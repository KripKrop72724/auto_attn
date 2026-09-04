set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Fix the downstream projection's Oracle SQL timezone expression.

  AT TIME ZONE accepts the literal below in static SQL, while binding the
  package constant at runtime raises ORA-00905.  This migration changes only
  SLIC_ZKT_DOWNSTREAM_REPAIR's package body, performs no table DML, and restores
  the exact previous body automatically if compilation or validation fails.
*/

declare
    l_previous_body clob;
    l_normalized_body clob;
    l_candidate_body clob;
    l_status varchar2(30);
    l_errors number;
    l_old_expression constant varchar2(200) :=
        'cast(event_timestamp at time zone c_attendance_timezone as date)';
    l_new_expression constant varchar2(200) :=
        'cast(event_timestamp at time zone ''Asia/Karachi'' as date)';

    function occurrence_count(p_source in clob, p_marker in varchar2)
        return pls_integer
    is
        l_count pls_integer := 0;
        l_offset pls_integer := 1;
        l_found pls_integer;
    begin
        loop
            l_found := dbms_lob.instr(p_source, p_marker, l_offset);
            exit when l_found = 0;
            l_count := l_count + 1;
            l_offset := l_found + length(p_marker);
        end loop;
        return l_count;
    end occurrence_count;

    procedure validate_body is
    begin
        select status
          into l_status
          from user_objects
         where object_name = 'SLIC_ZKT_DOWNSTREAM_REPAIR'
           and object_type = 'PACKAGE BODY';
        select count(*)
          into l_errors
          from user_errors
         where name = 'SLIC_ZKT_DOWNSTREAM_REPAIR'
           and type = 'PACKAGE BODY';
        if l_status <> 'VALID' or l_errors <> 0 then
            raise_application_error(
                -20891,
                'Downstream-repair package body validation failed.'
            );
        end if;
    end validate_body;
begin
    dbms_lob.createtemporary(l_previous_body, true);
    dbms_lob.createtemporary(l_normalized_body, true);
    dbms_lob.writeappend(
        l_previous_body,
        length('create or replace '),
        'create or replace '
    );
    dbms_lob.writeappend(
        l_normalized_body,
        length('create or replace '),
        'create or replace '
    );
    for source_line in (
        select text
          from user_source
         where name = 'SLIC_ZKT_DOWNSTREAM_REPAIR'
           and type = 'PACKAGE BODY'
         order by line
    ) loop
        dbms_lob.writeappend(
            l_previous_body,
            length(source_line.text),
            source_line.text
        );
        dbms_lob.writeappend(
            l_normalized_body,
            length(rtrim(source_line.text, ' ' || chr(9) || chr(13) || chr(10)) || chr(10)),
            rtrim(source_line.text, ' ' || chr(9) || chr(13) || chr(10)) || chr(10)
        );
    end loop;

    if occurrence_count(l_normalized_body, l_new_expression) = 2
       and occurrence_count(l_normalized_body, l_old_expression) = 0 then
        validate_body;
        dbms_output.put_line('downstream_timezone_expression=already_fixed');
        return;
    end if;
    if occurrence_count(l_normalized_body, l_old_expression) <> 2
       or occurrence_count(l_normalized_body, l_new_expression) <> 0 then
        raise_application_error(
            -20890,
            'Installed downstream-repair body does not match the guarded patch shape.'
        );
    end if;

    l_candidate_body := replace(
        l_normalized_body,
        l_old_expression,
        l_new_expression
    );
    execute immediate l_candidate_body;
    validate_body;
    if occurrence_count(l_candidate_body, l_new_expression) <> 2
       or occurrence_count(l_candidate_body, l_old_expression) <> 0 then
        raise_application_error(
            -20891,
            'Downstream-repair timezone markers are incomplete.'
        );
    end if;
    dbms_output.put_line('downstream_timezone_expression=fixed');
    dbms_output.put_line('attendance_rows_changed=0');
exception
    when others then
        if l_previous_body is not null then
            begin
                execute immediate l_previous_body;
                validate_body;
            exception
                when others then
                    raise_application_error(
                        -20892,
                        'Downstream timezone fix and automatic restoration both failed.'
                    );
            end;
        end if;
        raise;
end;
/
