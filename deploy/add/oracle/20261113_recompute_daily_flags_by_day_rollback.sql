set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Undo 20261113_recompute_daily_flags_by_day.sql.

  1. Restores the exact production SLIC_ZKT_RECOMPUTE_DAILY_FLAGS as read from
     USER_SOURCE on 13 Nov 2026 (full-table UPDATE and MERGE, about 30 s per
     punch at 932,366 rows). Skipped when that version is already installed.
  2. Drops index HR_RAW_ATTN_CNIC_DATE_I ONLINE, only when an index with
     exactly that name exists on HR_RAW_ATTN_CAPTURE_EVENTS. An index created
     by anyone else under another name is left alone.
  3. Recompiles anything left INVALID (the SLIC_ZKT_TRUTH_API and
     SLIC_ZKT_IDENTITY_REPAIR_API bodies) and reports it.

  No attendance row is inserted, updated, deleted or merged. Apply it like the
  migration: off-hours, APEX SQL Scripts (whole file) or SQL Commands (the
  block from DECLARE to the final END;).
*/
declare
    c_table constant varchar2(128) := 'HR_RAW_ATTN_CAPTURE_EVENTS';
    c_index constant varchar2(128) := 'HR_RAW_ATTN_CNIC_DATE_I';
    c_procedure constant varchar2(128) := 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS';
    -- The schema being changed: the APEX parsing schema (SLIC_HRM).
    l_owner constant varchar2(128) := sys_context('userenv', 'current_schema');

    -- Exact production source read from USER_SOURCE on 13 Nov 2026.
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

    l_count number;

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
              from all_source
             where owner = l_owner
               and name = c_procedure
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
          from all_objects
         where owner = l_owner
           and object_name = p_name
           and object_type = p_type;
        return l_status;
    exception
        when no_data_found then
            return 'ABSENT';
    end object_status;

    procedure compile_object(p_name in varchar2, p_type in varchar2) is
        l_name varchar2(300) :=
            dbms_assert.enquote_name(l_owner, false) || '.'
            || dbms_assert.enquote_name(p_name, false);
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
              from all_dependencies d
             where d.owner = l_owner
               and d.referenced_owner = l_owner
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
              from all_dependencies d
             where d.owner = l_owner
               and d.referenced_owner = l_owner
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
                || ' required object(s) are not VALID; see ALL_ERRORS.'
            );
        end if;
    end validate_and_report;
begin
    dbms_output.put_line('recompute_schema=' || l_owner);

    if same_source(installed_source, c_production_ddl) then
        dbms_output.put_line(
            'recompute_daily_flags=already the 13 Nov 2026 production version; not replaced'
        );
    else
        execute immediate c_production_ddl;
        dbms_output.put_line(
            'recompute_daily_flags=restored the 13 Nov 2026 production version'
        );
    end if;
    validate_and_report('rollback');

    select count(*)
      into l_count
      from all_indexes
     where owner = l_owner
       and index_name = c_index
       and table_owner = l_owner
       and table_name = c_table;
    if l_count = 1 then
        execute immediate
            'drop index ' || dbms_assert.enquote_name(l_owner, false)
            || '.hr_raw_attn_cnic_date_i online';
        dbms_output.put_line('recompute_index=hr_raw_attn_cnic_date_i dropped');
    else
        dbms_output.put_line('recompute_index=hr_raw_attn_cnic_date_i not present; nothing dropped');
    end if;
    dbms_output.put_line('attendance_rows_changed=0');
end;
/
