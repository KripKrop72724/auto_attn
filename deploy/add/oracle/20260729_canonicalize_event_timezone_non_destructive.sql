set define off
set serveroutput on

/*
  Canonicalize offset-bearing event timestamps at the Oracle ingestion
  boundary. Incoming UTC timestamps already describe the correct instant; the
  package now returns that instant represented in Asia/Karachi so callers do
  not see a misleading five-hour difference.

  This migration:
  - changes only SLIC_ZKT_TRUTH_API package-body source;
  - performs no attendance table DML;
  - requires the exact pre-patch parser shape;
  - preserves the non-destructive attendance gates; and
  - restores the exact original package body on any compile/verification
    failure.
*/

declare
    l_body                    clob;
    l_patched_body            clob;
    l_original_ddl            clob;
    l_patched_ddl             clob;
    l_body_status             varchar2(30);
    l_compile_errors          number;
    l_ddl_attempted           boolean := false;
    l_parser_start            integer;
    l_parser_end              integer;
    l_return_start            integer;
    l_canonical_start         integer;
    l_old_return              varchar2(4000) :=
        'return to_timestamp_tz(v_value, '
        || chr(39)
        || 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM'
        || chr(39)
        || ');';
    l_new_return              varchar2(4000) :=
        'return to_timestamp_tz(v_value, '
        || chr(39)
        || 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM'
        || chr(39)
        || ') at time zone c_attendance_timezone;';

    procedure restore_original is
    begin
        execute immediate l_original_ddl;
        l_ddl_attempted := false;
    end restore_original;
begin
    dbms_lob.createtemporary(l_body, true);
    for source_line in (
        select text
          from user_source
         where name = 'SLIC_ZKT_TRUTH_API'
           and type = 'PACKAGE BODY'
         order by line
    ) loop
        dbms_lob.writeappend(l_body, length(source_line.text), source_line.text);
    end loop;

    l_parser_start := dbms_lob.instr(
        lower(l_body),
        'function parse_event_timestamp',
        1
    );
    l_parser_end := dbms_lob.instr(
        lower(l_body),
        'end parse_event_timestamp;',
        l_parser_start
    );
    l_return_start := dbms_lob.instr(
        lower(l_body),
        'return to_timestamp_tz',
        l_parser_start
    );
    l_canonical_start := dbms_lob.instr(
        lower(l_body),
        'at time zone c_attendance_timezone;',
        l_return_start
    );

    if l_parser_start > 0
       and l_parser_end > l_parser_start
       and l_return_start > l_parser_start
       and l_canonical_start > l_return_start
       and l_canonical_start < l_parser_end then
        dbms_output.put_line(
            'SLIC_ZKT_TRUTH_API already canonicalizes event timestamps to Asia/Karachi.'
        );
        return;
    end if;

    if dbms_lob.getlength(l_body) = 0
       or l_parser_start = 0
       or l_parser_end = 0
       or dbms_lob.instr(l_body, l_old_return, l_parser_start) = 0
       or dbms_lob.instr(
              l_body,
              l_old_return,
              dbms_lob.instr(l_body, l_old_return, l_parser_start) + 1
          ) <> 0
       or regexp_count(
           l_body,
           'delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0',
           1,
           'i'
       ) <> 2 then
        raise_application_error(
            -20501,
            'Deployed package was not the expected non-destructive pre-canonicalization shape.'
        );
    end if;

    l_patched_body := replace(
        l_body,
        l_old_return,
        l_new_return
    );

    if dbms_lob.instr(l_patched_body, l_old_return, l_parser_start) <> 0
       or dbms_lob.instr(
              lower(l_patched_body),
              'at time zone c_attendance_timezone;',
              l_return_start
          ) = 0
       or regexp_count(
           l_patched_body,
           'delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0',
           1,
           'i'
       ) <> 2 then
        raise_application_error(
            -20502,
            'Patched package failed source invariants before compilation.'
        );
    end if;

    l_original_ddl := to_clob('create or replace ') || l_body;
    l_patched_ddl := to_clob('create or replace ') || l_patched_body;
    l_ddl_attempted := true;
    execute immediate l_patched_ddl;

    select status
      into l_body_status
      from user_objects
     where object_name = 'SLIC_ZKT_TRUTH_API'
       and object_type = 'PACKAGE BODY';

    select count(*)
      into l_compile_errors
      from user_errors
     where name = 'SLIC_ZKT_TRUTH_API'
       and type = 'PACKAGE BODY';

    if l_body_status <> 'VALID' or l_compile_errors <> 0 then
        restore_original;
        raise_application_error(
            -20503,
            'Canonical timestamp package failed compilation and was restored.'
        );
    end if;

    l_ddl_attempted := false;
    dbms_output.put_line(
        'SLIC_ZKT_TRUTH_API now preserves event instants and represents them in Asia/Karachi.'
    );
exception
    when others then
        if l_ddl_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20504,
                        'Timestamp package migration failed and automatic restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
