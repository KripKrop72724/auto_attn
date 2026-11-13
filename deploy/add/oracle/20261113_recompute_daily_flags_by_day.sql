set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  SLIC_ZKT_RECOMPUTE_DAILY_FLAGS: recompute each affected employee-day through
  an index instead of scanning the whole raw-capture table for every punch.

  Why (production SLIC_HRM, Oracle 19c EE 19.26, 32 CPUs, measured read-only
  on 13 Nov 2026):
  - Every inserted punch calls this procedure inside its own transaction
    (SLIC_ZKT_TRUTH_API body lines 443 and 912, SLIC_ZKT_IDENTITY_REPAIR_API
    body line 757).
  - V$SQL per execution: MERGE 14.9 s (14.8 s CPU, 7,533 buffer gets),
    UPDATE 12.5 s (12.5 s CPU), lock SELECT ... FOR UPDATE 1.7 s (mostly lock
    wait) and the punch INSERT 3.8 s (lock wait behind other recomputes).
  - The MERGE plan is TABLE ACCESS FULL HR_RAW_ATTN_CAPTURE_EVENTS +
    FILTER(JSONTABLE EVALUATION): the request JSON is parsed again for every
    one of the 932,366 rows (7,623 blocks). The table has only PK_RAW and
    UK_HR_RAW_ATTN_EVENT_UID; an employee-day holds 2 rows on average (max 31).

  What this script does:
  1. Creates index HR_RAW_ATTN_CNIC_DATE_I on (CNIC, ATTENDANCE_DATE) ONLINE,
     so punches keep flowing while it builds. Skipped when a usable index
     already starts with exactly (CNIC, ATTENDANCE_DATE).
  2. Replaces SLIC_ZKT_RECOMPUTE_DAILY_FLAGS: same signature, AUTHID DEFINER,
     same results and the same lock order. It still locks every affected
     employee-day first (sorted by CNIC, then date), then runs the same
     UPDATE and then the same MERGE, but once per affected (CNIC,
     ATTENDANCE_DATE), so each statement reads about 2 rows through the index.
     CI (tests/oracle/run_recompute_checks.py) runs this exact file and
     proves identical rows against the exact production procedure, and at
     least 50x less time per call on a table of about 800,000 rows.
  3. Recompiles anything left INVALID (the SLIC_ZKT_TRUTH_API and
     SLIC_ZKT_IDENTITY_REPAIR_API bodies call the procedure) and reports it.

  No attendance row is inserted, updated, deleted or merged by this script.

  Guards: nothing changes unless the installed procedure is the 13 Nov 2026
  production version (or already this version) and the table and dependent
  packages look as expected. If the new procedure or a dependent does not
  compile, the exact previous procedure is restored automatically. Running
  the script again is safe: finished steps are reported and skipped.

  How to apply (off-hours, when few punches arrive):
  - APEX > SQL Workshop > SQL Scripts: paste or upload this whole file, Run.
    Or SQL Commands: paste only the block from DECLARE to the final END;
    and Run. Lines starting with recompute_ report each step.
  - Expect new punches to wait for up to about a minute: the ONLINE index
    build briefly waits for in-flight punch transactions, and replacing the
    procedure waits for running recompute calls (about 30 s each today).
    Replacing the procedure may mark the two dependent package bodies
    INVALID; this script recompiles them within seconds (Oracle would also do
    it on their next call).
  - Undo: 20261113_recompute_daily_flags_by_day_rollback.sql restores the
    exact production procedure and drops HR_RAW_ATTN_CNIC_DATE_I.
*/
declare
    c_table constant varchar2(128) := 'HR_RAW_ATTN_CAPTURE_EVENTS';
    c_index constant varchar2(128) := 'HR_RAW_ATTN_CNIC_DATE_I';
    c_procedure constant varchar2(128) := 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS';

    -- The production procedure this replaces, exactly as read from
    -- USER_SOURCE on 13 Nov 2026 (identical to the rollback script).
    c_production_ddl constant varchar2(32767) := q'~create or replace procedure slic_zkt_recompute_daily_flags(
  p_body in clob
) authid definer
as
begin
  for affected_day in (
    select distinct
      trim(j.cnic) cnic,
      slic_zkt_truth_api.attendance_date_for(
        slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)
      ) attendance_date
    from json_table(
      p_body,
      '$.events[*]'
      columns
        cnic varchar2(13) path '$.cnic' null on error,
        event_timestamp varchar2(80) path '$.timestamp' null on error
    ) j
    where trim(j.cnic) is not null
      and slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) is not null
    order by cnic, attendance_date
  ) loop
    for locked_event in (
      select d.event_uid
      from hr_raw_attn_capture_events d
      where d.raw_punch = 'F'
        and slic_zkt_truth_api.attendance_timestamp_is_plausible(
          d.event_timestamp
        ) = 1
        and d.cnic = affected_day.cnic
        and d.attendance_date = affected_day.attendance_date
      order by d.event_timestamp, d.event_uid
      for update
    ) loop
      null;
    end loop;
  end loop;

  update hr_raw_attn_capture_events d
  set d.check_in = 'F',
      d.check_out = 'F',
      d.datasync = 0
  where d.raw_punch = 'F'
    and slic_zkt_truth_api.attendance_timestamp_is_plausible(
      d.event_timestamp
    ) = 1
    and (d.check_in <> 'F' or d.check_out <> 'F')
    and exists (
      select 1
      from json_table(
        p_body,
        '$.events[*]'
        columns
          cnic varchar2(13) path '$.cnic' null on error,
          event_timestamp varchar2(80) path '$.timestamp' null on error
      ) j
      where trim(j.cnic) = d.cnic
        and slic_zkt_truth_api.attendance_date_for(
          slic_zkt_truth_api.parse_event_timestamp(
            j.event_timestamp
          )
        ) = d.attendance_date
    );

  merge into hr_raw_attn_capture_events d
  using (
    select ranked.event_uid,
      case when ranked.rn_in = 1 then 'T' else 'F' end check_in,
      case
        when ranked.normal_count > 1 and ranked.rn_out = 1
        then 'T'
        else 'F'
      end check_out
    from (
      select stored.event_uid,
        row_number() over (
          partition by stored.cnic, stored.attendance_date
          order by stored.event_timestamp, stored.event_uid
        ) rn_in,
        row_number() over (
          partition by stored.cnic, stored.attendance_date
          order by stored.event_timestamp desc, stored.event_uid desc
        ) rn_out,
        count(*) over (
          partition by stored.cnic, stored.attendance_date
        ) normal_count
      from hr_raw_attn_capture_events stored
      where stored.raw_punch = 'F'
        and slic_zkt_truth_api.attendance_timestamp_is_plausible(
          stored.event_timestamp
        ) = 1
        and exists (
          select 1
          from json_table(
            p_body,
            '$.events[*]'
            columns
              cnic varchar2(13) path '$.cnic' null on error,
              event_timestamp varchar2(80) path '$.timestamp' null on error
          ) j
          where trim(j.cnic) = stored.cnic
            and slic_zkt_truth_api.attendance_date_for(
              slic_zkt_truth_api.parse_event_timestamp(
                j.event_timestamp
              )
            ) = stored.attendance_date
        )
    ) ranked
  ) desired
  on (d.event_uid = desired.event_uid)
  when matched then update set
    d.check_in = desired.check_in,
    d.check_out = desired.check_out,
    d.datasync = 0
  where d.check_in <> desired.check_in
     or d.check_out <> desired.check_out;
end slic_zkt_recompute_daily_flags;~';

    -- The per-employee-day procedure (identical to the copy embedded in
    -- deploy/add/oracle/slic_zkt_truth_api.sql).
    c_per_day_ddl constant varchar2(32767) := q'~create or replace procedure slic_zkt_recompute_daily_flags(
    p_body in clob
) authid definer
as
    -- Each distinct (CNIC text, attendance date) named by the request, in
    -- the same order and with the same filters as the original cursor.
    type t_cnic_list is table of varchar2(13);
    type t_date_list is table of date;
    l_cnics t_cnic_list;
    l_dates t_date_list;
begin
    select distinct
           trim(j.cnic) cnic,
           slic_zkt_truth_api.attendance_date_for(
               slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)
           ) attendance_date
      bulk collect into l_cnics, l_dates
      from json_table(
               p_body,
               '$.events[*]'
               columns
                   cnic varchar2(13) path '$.cnic' null on error,
                   event_timestamp varchar2(80) path '$.timestamp' null on error
           ) j
     where trim(j.cnic) is not null
       and slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp) is not null
     order by cnic, attendance_date;

    -- Lock affected normal-punch rows in a deterministic order: every
    -- employee-day first, sorted by CNIC then date, before any row changes.
    -- The caller owns the transaction; any later failure rolls back both
    -- insertion and flag correction together.
    for i in 1 .. l_cnics.count loop
        for locked_event in (
            select d.event_uid
              from hr_raw_attn_capture_events d
             where d.raw_punch = 'F'
               and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                       d.event_timestamp
                   ) = 1
               and d.cnic = l_cnics(i)
               and d.attendance_date = l_dates(i)
             order by d.event_timestamp, d.event_uid
             for update
        ) loop
            null;
        end loop;
    end loop;

    -- The UPDATE and MERGE below are the original statements restricted to
    -- one employee-day at a time, so each reads only that day's rows through
    -- the (CNIC, ATTENDANCE_DATE) index instead of testing every table row
    -- against the request JSON. Employee-days are independent partitions,
    -- so the resulting rows are identical.
    for i in 1 .. l_cnics.count loop
        update hr_raw_attn_capture_events d
           set d.check_in = 'F',
               d.check_out = 'F',
               d.datasync = 0
         where d.raw_punch = 'F'
           and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                   d.event_timestamp
               ) = 1
           and (d.check_in <> 'F' or d.check_out <> 'F')
           and d.cnic = l_cnics(i)
           and d.attendance_date = l_dates(i);
    end loop;

    for i in 1 .. l_cnics.count loop
        merge into hr_raw_attn_capture_events d
        using (
            select ranked.event_uid,
                   case when ranked.rn_in = 1 then 'T' else 'F' end check_in,
                   case
                       when ranked.normal_count > 1 and ranked.rn_out = 1
                       then 'T'
                       else 'F'
                   end check_out
              from (
                  select stored.event_uid,
                         row_number() over (
                             partition by stored.cnic, stored.attendance_date
                             order by stored.event_timestamp, stored.event_uid
                         ) rn_in,
                         row_number() over (
                             partition by stored.cnic, stored.attendance_date
                             order by stored.event_timestamp desc, stored.event_uid desc
                         ) rn_out,
                         count(*) over (
                             partition by stored.cnic, stored.attendance_date
                         ) normal_count
                   from hr_raw_attn_capture_events stored
                   where stored.raw_punch = 'F'
                     and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                             stored.event_timestamp
                         ) = 1
                     and stored.cnic = l_cnics(i)
                     and stored.attendance_date = l_dates(i)
              ) ranked
        ) desired
           -- EVENT_UID is unique, so a matched row is the ranked row itself;
           -- the day predicates are always true for it and only let Oracle
           -- reach the target through the same index.
           on (d.event_uid = desired.event_uid
               and d.cnic = l_cnics(i)
               and d.attendance_date = l_dates(i))
         when matched then update set
              d.check_in = desired.check_in,
              d.check_out = desired.check_out,
              d.datasync = 0
          where d.check_in <> desired.check_in
             or d.check_out <> desired.check_out;
    end loop;
end slic_zkt_recompute_daily_flags;~';

    l_installed clob;
    l_previous_ddl clob;
    l_state varchar2(30);
    l_usable_index varchar2(128);
    l_unusable_index varchar2(400);
    l_count number;
    l_changed boolean := false;
    l_error varchar2(4000);

    function normalized(p_source in clob) return clob is
        l_text clob := p_source;
    begin
        -- Compare logic, not layout: drop comments, whitespace and case.
        l_text := regexp_replace(l_text, '--[^' || chr(10) || ']*', '');
        l_text := regexp_replace(l_text, '/\*.*?\*/', '', 1, 0, 'n');
        l_text := lower(regexp_replace(l_text, '[[:space:]]+', ''));
        return regexp_replace(
            l_text,
            '^createorreplace(editionable|noneditionable)?',
            ''
        );
    end normalized;

    function same_source(p_left in clob, p_right in clob) return boolean is
    begin
        return nvl(dbms_lob.compare(normalized(p_left), normalized(p_right)), 1) = 0;
    end same_source;

    function installed_source return clob is
        l_source clob;
    begin
        dbms_lob.createtemporary(l_source, true);
        for source_line in (
            select text
              from user_source
             where name = c_procedure
               and type = 'PROCEDURE'
             order by line
        ) loop
            if source_line.text is not null then
                dbms_lob.writeappend(
                    l_source,
                    length(source_line.text),
                    source_line.text
                );
            end if;
        end loop;
        return l_source;
    end installed_source;

    function object_status(p_name in varchar2, p_type in varchar2)
        return varchar2
    is
        l_status varchar2(30);
    begin
        select status
          into l_status
          from user_objects
         where object_name = p_name
           and object_type = p_type;
        return l_status;
    exception
        when no_data_found then
            return 'ABSENT';
    end object_status;

    procedure compile_object(p_name in varchar2, p_type in varchar2) is
        l_name varchar2(200) := dbms_assert.enquote_name(p_name, false);
    begin
        if p_type = 'PACKAGE BODY' then
            execute immediate 'alter package ' || l_name || ' compile body reuse settings';
        elsif p_type = 'PACKAGE' then
            execute immediate 'alter package ' || l_name || ' compile specification reuse settings';
        elsif p_type = 'TYPE BODY' then
            execute immediate 'alter type ' || l_name || ' compile body reuse settings';
        elsif p_type in ('PROCEDURE', 'FUNCTION', 'TRIGGER') then
            execute immediate 'alter ' || lower(p_type) || ' ' || l_name || ' compile reuse settings';
        end if;
    exception
        when others then
            -- ORA-24344 = compiled with errors; the status check reports it.
            if sqlcode <> -24344 then
                raise;
            end if;
    end compile_object;

    -- Recompile anything Oracle left INVALID after the procedure changed, then
    -- require the procedure, SLIC_ZKT_TRUTH_API and (when present)
    -- SLIC_ZKT_IDENTITY_REPAIR_API plus every other dependent to be VALID.
    procedure validate_and_report(p_stage in varchar2) is
        l_status varchar2(30);
        l_problems pls_integer := 0;
    begin
        if object_status(c_procedure, 'PROCEDURE') <> 'VALID' then
            compile_object(c_procedure, 'PROCEDURE');
        end if;
        for dependent in (
            select distinct d.name object_name, d.type object_type
              from user_dependencies d
             where d.referenced_owner = sys_context('userenv', 'current_schema')
               and d.referenced_name = c_procedure
               and d.referenced_type = 'PROCEDURE'
             order by 1, 2
        ) loop
            if object_status(dependent.object_name, dependent.object_type) <> 'VALID' then
                compile_object(dependent.object_name, dependent.object_type);
            end if;
        end loop;

        for required in (
            select c_procedure object_name, 'PROCEDURE' object_type, 1 mandatory from dual
            union all
            select 'SLIC_ZKT_TRUTH_API', 'PACKAGE', 1 from dual
            union all
            select 'SLIC_ZKT_TRUTH_API', 'PACKAGE BODY', 1 from dual
            union all
            select 'SLIC_ZKT_IDENTITY_REPAIR_API', 'PACKAGE', 0 from dual
            union all
            select 'SLIC_ZKT_IDENTITY_REPAIR_API', 'PACKAGE BODY', 0 from dual
        ) loop
            l_status := object_status(required.object_name, required.object_type);
            if l_status = 'INVALID' then
                compile_object(required.object_name, required.object_type);
                l_status := object_status(required.object_name, required.object_type);
            end if;
            dbms_output.put_line(
                'recompute_' || p_stage || ': ' || lower(required.object_name)
                || ' ' || lower(required.object_type) || '=' || l_status
            );
            if l_status <> 'VALID'
               and (required.mandatory = 1 or l_status <> 'ABSENT') then
                l_problems := l_problems + 1;
            end if;
        end loop;

        for dependent in (
            select distinct d.name object_name, d.type object_type
              from user_dependencies d
             where d.referenced_owner = sys_context('userenv', 'current_schema')
               and d.referenced_name = c_procedure
               and d.referenced_type = 'PROCEDURE'
               and d.name not in ('SLIC_ZKT_TRUTH_API', 'SLIC_ZKT_IDENTITY_REPAIR_API')
             order by 1, 2
        ) loop
            l_status := object_status(dependent.object_name, dependent.object_type);
            dbms_output.put_line(
                'recompute_' || p_stage || ': dependent ' || lower(dependent.object_name)
                || ' ' || lower(dependent.object_type) || '=' || l_status
            );
            if l_status <> 'VALID' then
                l_problems := l_problems + 1;
            end if;
        end loop;

        if l_problems > 0 then
            raise_application_error(
                -20971,
                'recompute ' || p_stage || ': ' || l_problems
                || ' required object(s) are not VALID; see USER_ERRORS.'
            );
        end if;
    end validate_and_report;
begin
    dbms_output.put_line(
        'recompute_schema=' || sys_context('userenv', 'current_schema')
    );

    -- 1. Preflight. Nothing is changed unless all of this holds.
    select count(*)
      into l_count
      from user_tab_columns
     where table_name = c_table
       and (
               (column_name = 'CNIC' and data_type = 'NUMBER')
               or (column_name = 'ATTENDANCE_DATE' and data_type = 'DATE')
           );
    if l_count <> 2 then
        raise_application_error(
            -20972,
            c_table || ' lacks NUMBER CNIC or DATE ATTENDANCE_DATE; nothing was changed.'
        );
    end if;

    l_installed := installed_source;
    if nvl(dbms_lob.getlength(l_installed), 0) = 0 then
        raise_application_error(
            -20973,
            c_procedure || ' is not installed; nothing was changed.'
        );
    end if;
    l_previous_ddl := 'create or replace ' || l_installed;

    if same_source(l_installed, c_per_day_ddl) then
        l_state := 'per_employee_day';
    elsif same_source(l_installed, c_production_ddl) then
        l_state := 'production';
    else
        raise_application_error(
            -20974,
            c_procedure || ' differs from both the 13 Nov 2026 production version'
            || ' and the per-employee-day version; nothing was changed.'
        );
    end if;
    dbms_output.put_line('recompute_installed_procedure=' || l_state);

    select count(*)
      into l_count
      from user_indexes
     where index_name = c_index
       and table_name <> c_table;
    if l_count <> 0 then
        raise_application_error(
            -20976,
            c_index || ' already names an index on another table; nothing was changed.'
        );
    end if;

    for candidate in (
        select i.index_name, i.status, i.visibility
          from user_indexes i
         where i.table_name = c_table
           and (
                   select c.column_name
                     from user_ind_columns c
                    where c.index_name = i.index_name
                      and c.table_name = i.table_name
                      and c.column_position = 1
               ) = 'CNIC'
           and (
                   select c.column_name
                     from user_ind_columns c
                    where c.index_name = i.index_name
                      and c.table_name = i.table_name
                      and c.column_position = 2
               ) = 'ATTENDANCE_DATE'
         order by case when i.index_name = c_index then 0 else 1 end,
                  i.index_name
    ) loop
        if candidate.status in ('VALID', 'N/A')
           and candidate.visibility = 'VISIBLE' then
            l_usable_index := candidate.index_name;
            exit;
        end if;
        l_unusable_index := substr(
            l_unusable_index || ' ' || candidate.index_name || ' ('
            || candidate.status || ', ' || candidate.visibility || ')',
            1,
            400
        );
    end loop;
    if l_usable_index is null and l_unusable_index is not null then
        raise_application_error(
            -20977,
            'Index on (CNIC, ATTENDANCE_DATE) exists but is not usable:'
            || l_unusable_index || '. Rebuild it or make it VISIBLE, then re-run;'
            || ' nothing was changed.'
        );
    end if;

    for trigger_row in (
        select trigger_name, trigger_type
          from user_triggers
         where table_name = c_table
           and triggering_event like '%UPDATE%'
         order by trigger_name
    ) loop
        -- Row-level effects are identical. A statement-level UPDATE trigger
        -- would fire once per affected employee-day instead of once per call.
        dbms_output.put_line(
            'recompute_info: update trigger ' || lower(trigger_row.trigger_name)
            || ' (' || lower(trigger_row.trigger_type) || ')'
        );
    end loop;

    -- Dependents must already compile before anything changes.
    validate_and_report('before');

    -- 2. Index (ONLINE: inserts and updates continue while it builds).
    if l_usable_index is null then
        execute immediate
            'create index hr_raw_attn_cnic_date_i'
            || ' on hr_raw_attn_capture_events (cnic, attendance_date) online';
        dbms_output.put_line('recompute_index=hr_raw_attn_cnic_date_i created online');
    else
        dbms_output.put_line(
            'recompute_index=' || lower(l_usable_index)
            || ' already covers (cnic, attendance_date); no index created'
        );
    end if;

    -- 3. Procedure.
    if l_state = 'production' then
        l_changed := true;
        execute immediate c_per_day_ddl;
        validate_and_report('after');
        l_changed := false;
        dbms_output.put_line(
            'recompute_daily_flags=replaced with the per-employee-day version'
        );
    else
        validate_and_report('after');
        dbms_output.put_line(
            'recompute_daily_flags=already the per-employee-day version; not replaced'
        );
    end if;
    dbms_output.put_line('attendance_rows_changed=0');
exception
    when others then
        l_error := sqlerrm;
        if l_changed then
            begin
                execute immediate l_previous_ddl;
                validate_and_report('restored');
            exception
                when others then
                    raise_application_error(
                        -20975,
                        'Per-employee-day recompute failed and automatic restoration'
                        || ' also failed: ' || l_error
                    );
            end;
            dbms_output.put_line(
                'recompute_daily_flags=previous version restored after: ' || l_error
            );
        end if;
        raise;
end;
/
