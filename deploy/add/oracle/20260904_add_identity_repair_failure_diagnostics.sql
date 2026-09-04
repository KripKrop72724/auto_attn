set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Add PII-free unexpected-failure diagnostics to the installed identity-repair
  package without reading, printing, or replacing its protected credential.

  This migration changes only SLIC_ZKT_IDENTITY_REPAIR_API's package body.  It
  performs no table DML and returns only SQLCODE plus the package line number;
  SQLERRM and the backtrace are never returned.  The exact previous body is
  restored automatically if compilation or validation fails.
*/

declare
    l_previous_body clob;
    l_normalized_body clob;
    l_candidate_body clob;
    l_status varchar2(30);
    l_errors number;
    l_old_declaration constant varchar2(4000) := q'~        l_result json_object_t;
        l_json clob;
    begin
        require_add_auth;
        if not downstream_ready then~';
    l_new_declaration constant varchar2(4000) := q'~        l_result json_object_t;
        l_json clob;
        l_error_code number;
        l_error_line number;
    begin
        require_add_auth;
        if not downstream_ready then~';
    l_old_handler constant varchar2(4000) := q'~    exception
        when e_response_sent then rollback;
        when others then
            rollback;
            send_json(500, '{"success":false,"error_code":"REPAIR_TRANSACTION_FAILED"}');
    end post_repair;~';
    l_new_handler constant varchar2(4000) := q'~    exception
        when e_response_sent then rollback;
        when others then
            l_error_code := sqlcode;
            l_error_line := to_number(
                regexp_substr(
                    dbms_utility.format_error_backtrace,
                    'line ([0-9]+)',
                    1,
                    1,
                    'i',
                    1
                )
            );
            rollback;
            select json_object(
                       'success' value 'false' format json,
                       'error_code' value 'REPAIR_TRANSACTION_FAILED',
                       'oracle_sqlcode' value l_error_code,
                       'failure_line' value l_error_line
                       returning clob
                   )
              into l_json
              from dual;
            send_json(500, l_json);
    end post_repair;~';

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
         where object_name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and object_type = 'PACKAGE BODY';
        select count(*)
          into l_errors
          from user_errors
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY';
        if l_status <> 'VALID' or l_errors <> 0 then
            raise_application_error(-20881, 'Identity-repair package body validation failed.');
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
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
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

    if occurrence_count(l_normalized_body, l_new_declaration) = 1
       and occurrence_count(l_normalized_body, l_new_handler) = 1
       and occurrence_count(l_normalized_body, l_old_handler) = 0 then
        validate_body;
        dbms_output.put_line('identity_repair_failure_diagnostics=already_installed');
        return;
    end if;
    if occurrence_count(l_normalized_body, l_old_declaration) <> 1
       or occurrence_count(l_normalized_body, l_old_handler) <> 1
       or occurrence_count(l_normalized_body, l_new_declaration) <> 0
       or occurrence_count(l_normalized_body, l_new_handler) <> 0 then
        raise_application_error(
            -20880,
            'Installed identity-repair body does not match the guarded patch shape.'
        );
    end if;

    l_candidate_body := replace(
        replace(l_normalized_body, l_old_declaration, l_new_declaration),
        l_old_handler,
        l_new_handler
    );
    execute immediate l_candidate_body;
    validate_body;
    if occurrence_count(l_candidate_body, l_new_declaration) <> 1
       or occurrence_count(l_candidate_body, l_new_handler) <> 1 then
        raise_application_error(-20881, 'Identity-repair diagnostic markers are incomplete.');
    end if;
    dbms_output.put_line('identity_repair_failure_diagnostics=installed');
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
                        -20882,
                        'Identity-repair diagnostic install and automatic restoration both failed.'
                    );
            end;
        end if;
        raise;
end;
/
