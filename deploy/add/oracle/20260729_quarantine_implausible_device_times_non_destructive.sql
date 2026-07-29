set define off
set serveroutput on

/*
  Preserve impossible terminal-time records for audit while preventing them
  from becoming attendance. The migration:

  - never deletes HR_RAW_ATTN_CAPTURE_EVENTS rows;
  - marks the two proven ZONE-SLICTOWER-3FL zero-timestamp sentinels as suspect,
    clears only their derived daily flags, and resets DATASYNC for correction;
  - makes the package classify future implausible timestamps as
    SUSPECT_DEVICE_TIME; and
  - excludes suspect rows from daily check-in/check-out ranking.

  Package credentials are preserved by patching the deployed source in place.
  Every source edit is exact-shape guarded and automatically restored if any
  affected Oracle object fails to compile.
*/

declare
    l_matches number;
    l_updated number;
begin
    select count(*)
      into l_matches
      from hr_raw_attn_capture_events
     where event_uid in (
               'cb38c86dc7c4d7778c9f79b8141511d2329aa4cdb9477c0b182dc2f67e257514',
               'dff1455669191f906988cff77516939fb5b4df1c6c53560fb7df95e8851a4cc5'
           )
       and zone_id = 'ZONE-SLICTOWER-3FL'
       and device_serial = 'ADZV211860253'
       and event_timestamp < to_timestamp_tz(
               '2010-01-01 00:00:00 Asia/Karachi',
               'YYYY-MM-DD HH24:MI:SS TZR'
           );

    if l_matches <> 2 then
        raise_application_error(
            -20520,
            'Expected exactly two proven sentinel rows; found ' || l_matches
        );
    end if;

    update hr_raw_attn_capture_events
       set trust_status = 'SUSPECT_DEVICE_TIME',
           check_in = 'F',
           check_out = 'F',
           datasync = 0
     where event_uid in (
               'cb38c86dc7c4d7778c9f79b8141511d2329aa4cdb9477c0b182dc2f67e257514',
               'dff1455669191f906988cff77516939fb5b4df1c6c53560fb7df95e8851a4cc5'
           )
       and zone_id = 'ZONE-SLICTOWER-3FL'
       and device_serial = 'ADZV211860253'
       and event_timestamp < to_timestamp_tz(
               '2010-01-01 00:00:00 Asia/Karachi',
               'YYYY-MM-DD HH24:MI:SS TZR'
           );
    l_updated := sql%rowcount;

    if l_updated <> 2 then
        rollback;
        raise_application_error(
            -20521,
            'Sentinel correction updated ' || l_updated || ' rows; rolled back'
        );
    end if;

    commit;
    dbms_output.put_line(
        'Corrected and preserved exactly two sentinel attendance rows.'
    );
end;
/

declare
    l_spec clob;
    l_body clob;
    l_flags clob;
    l_original_spec_ddl clob;
    l_original_body_ddl clob;
    l_original_flags_ddl clob;
    l_new_spec_ddl clob;
    l_new_body_ddl clob;
    l_new_flags_ddl clob;
    l_new_spec clob;
    l_new_body clob;
    l_new_flags clob;
    l_marker varchar2(200) := 'function attendance_timestamp_is_plausible';
    l_old_trust varchar2(500) :=
        'coalesce(nullif(trim(j.trust_status), ''''), ''TRUSTED_LIVE'') trust_status,';
    l_new_trust varchar2(4000) := q'~
case
                       when slic_zkt_truth_api.attendance_timestamp_is_plausible(
                                slic_zkt_truth_api.parse_event_timestamp(j.event_timestamp)
                            ) = 0
                       then 'SUSPECT_DEVICE_TIME'
                       else coalesce(nullif(trim(j.trust_status), ''), 'TRUSTED_LIVE')
                   end trust_status,~';
    l_function_source varchar2(4000) := q'~

    function attendance_timestamp_is_plausible(
        p_event_ts in timestamp with time zone
    ) return number is
    begin
        if p_event_ts is null
           or p_event_ts < to_timestamp_tz(
                  '2010-01-01 00:00:00 Asia/Karachi',
                  'YYYY-MM-DD HH24:MI:SS TZR'
              )
           or p_event_ts > systimestamp + interval '1' day then
            return 0;
        end if;
        return 1;
    end attendance_timestamp_is_plausible;~';
    l_attempted boolean := false;
    l_package_guard_installed boolean;
    l_interim_trust_filter_installed boolean;
    l_invalid_objects number;
    l_compile_errors number;

    function load_source(
        p_name varchar2,
        p_type varchar2
    ) return clob is
        l_source clob;
    begin
        dbms_lob.createtemporary(l_source, true);
        for source_line in (
            select text
              from user_source
             where name = upper(p_name)
               and type = upper(p_type)
             order by line
        ) loop
            dbms_lob.writeappend(
                l_source,
                length(source_line.text),
                source_line.text
            );
        end loop;
        return l_source;
    end load_source;

    function count_literal(
        p_source clob,
        p_needle varchar2
    ) return number is
        l_count number := 0;
        l_position integer := 1;
        l_match integer;
    begin
        loop
            l_match := dbms_lob.instr(p_source, p_needle, l_position);
            exit when l_match = 0;
            l_count := l_count + 1;
            l_position := l_match + length(p_needle);
        end loop;
        return l_count;
    end count_literal;

    procedure restore_original is
    begin
        execute immediate l_original_spec_ddl;
        execute immediate l_original_body_ddl;
        execute immediate l_original_flags_ddl;
        l_attempted := false;
    end restore_original;
begin
    l_spec := load_source('SLIC_ZKT_TRUTH_API', 'PACKAGE');
    l_body := load_source('SLIC_ZKT_TRUTH_API', 'PACKAGE BODY');
    l_flags := load_source('SLIC_ZKT_RECOMPUTE_DAILY_FLAGS', 'PROCEDURE');
    l_package_guard_installed :=
        dbms_lob.instr(lower(l_spec), l_marker, 1) > 0
        and dbms_lob.instr(lower(l_body), l_marker, 1) > 0;
    l_interim_trust_filter_installed :=
        regexp_count(
            l_flags,
            'trust_status <> ''SUSPECT_DEVICE_TIME''',
            1,
            'i'
        ) = 3;

    if dbms_lob.getlength(l_spec) = 0
       or dbms_lob.getlength(l_body) = 0
       or dbms_lob.getlength(l_flags) = 0 then
        raise_application_error(
            -20522,
            'Expected Oracle attendance package objects were not found.'
        );
    end if;

    if l_package_guard_installed
       and regexp_count(
               l_flags,
               'attendance_timestamp_is_plausible',
               1,
               'i'
           ) = 3 then
        dbms_output.put_line(
            'Implausible timestamp package guard is already installed.'
        );
        return;
    end if;

    if (
           not l_package_guard_installed
           and (
               dbms_lob.instr(lower(l_spec), l_marker, 1) > 0
               or dbms_lob.instr(lower(l_body), l_marker, 1) > 0
               or count_literal(l_body, l_old_trust) <> 4
           )
       )
       or (
           l_interim_trust_filter_installed
           and not l_package_guard_installed
       )
       or (
           not l_interim_trust_filter_installed
           and regexp_count(
                   l_flags,
                   'attendance_timestamp_is_plausible',
                   1,
                   'i'
               ) <> 0
       )
       or regexp_count(
               l_flags,
               'where d.raw_punch = ''F''',
               1,
               'i'
           ) <> 2
       or regexp_count(
               l_flags,
               'where stored.raw_punch = ''F''',
               1,
               'i'
           ) <> 1 then
        raise_application_error(
            -20523,
            'Deployed attendance package was not the expected pre-guard shape.'
        );
    end if;

    if l_package_guard_installed then
        l_new_spec := l_spec;
        l_new_body := l_body;
    else
        l_new_spec := replace(
            l_spec,
            '    function attendance_date_for(p_event_ts in timestamp with time zone) return date;',
            q'~    function attendance_timestamp_is_plausible(
        p_event_ts in timestamp with time zone
    ) return number;
    function attendance_date_for(p_event_ts in timestamp with time zone) return date;~'
        );
        l_new_body := replace(
            l_body,
            '    end parse_event_timestamp;',
            '    end parse_event_timestamp;' || l_function_source
        );
        l_new_body := replace(l_new_body, l_old_trust, l_new_trust);
    end if;

    if l_interim_trust_filter_installed then
        l_new_flags := replace(
            l_flags,
            'and d.trust_status <> ''SUSPECT_DEVICE_TIME''',
            q'~and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                       d.event_timestamp
                   ) = 1~'
        );
        l_new_flags := replace(
            l_new_flags,
            'and stored.trust_status <> ''SUSPECT_DEVICE_TIME''',
            q'~and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                         stored.event_timestamp
                     ) = 1~'
        );
    else
        l_new_flags := replace(
            l_flags,
            'where d.raw_punch = ''F''',
            'where d.raw_punch = ''F''' || chr(10)
            || q'~               and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                       d.event_timestamp
                   ) = 1~'
        );
        l_new_flags := replace(
            l_new_flags,
            'where stored.raw_punch = ''F''',
            'where stored.raw_punch = ''F''' || chr(10)
            || q'~                 and slic_zkt_truth_api.attendance_timestamp_is_plausible(
                         stored.event_timestamp
                     ) = 1~'
        );
    end if;

    if dbms_lob.instr(lower(l_new_spec), l_marker, 1) = 0
       or dbms_lob.instr(lower(l_new_body), l_marker, 1) = 0
       or count_literal(l_new_body, l_old_trust) <> 0
       or regexp_count(
               l_new_flags,
               'attendance_timestamp_is_plausible',
               1,
               'i'
           ) <> 3
       or regexp_count(
               l_new_flags,
               'trust_status <> ''SUSPECT_DEVICE_TIME''',
               1,
               'i'
           ) <> 0 then
        raise_application_error(
            -20524,
            'Timestamp guard failed source invariants before compilation.'
        );
    end if;

    l_original_spec_ddl := to_clob('create or replace ') || l_spec;
    l_original_body_ddl := to_clob('create or replace ') || l_body;
    l_original_flags_ddl := to_clob('create or replace ') || l_flags;
    l_new_spec_ddl := to_clob('create or replace ') || l_new_spec;
    l_new_body_ddl := to_clob('create or replace ') || l_new_body;
    l_new_flags_ddl := to_clob('create or replace ') || l_new_flags;
    l_attempted := true;

    execute immediate l_new_spec_ddl;
    execute immediate l_new_body_ddl;
    execute immediate l_new_flags_ddl;

    select count(*)
      into l_invalid_objects
      from user_objects
     where object_name in (
               'SLIC_ZKT_TRUTH_API',
               'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
           )
       and status <> 'VALID';

    select count(*)
      into l_compile_errors
      from user_errors
     where name in (
               'SLIC_ZKT_TRUTH_API',
               'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
           );

    if l_invalid_objects <> 0 or l_compile_errors <> 0 then
        restore_original;
        raise_application_error(
            -20525,
            'Timestamp guard failed compilation and original objects were restored.'
        );
    end if;

    l_attempted := false;
    dbms_output.put_line(
        'Oracle now preserves implausible timestamps as suspect and excludes them from attendance flags.'
    );
exception
    when others then
        if l_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20526,
                        'Timestamp guard failed and automatic source restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
