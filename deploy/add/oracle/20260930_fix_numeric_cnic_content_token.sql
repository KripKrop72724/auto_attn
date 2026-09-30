set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  HR_RAW_ATTN_CAPTURE_EVENTS.CNIC is NUMBER in production. NVL(cnic, chr(0))
  converts the string sentinel to NUMBER and raises ORA-01722, preventing the
  read-only Oracle content check from verifying a saved attendance approval.

  Convert CNIC to deterministic text before applying the null sentinel. This
  guarded replacement changes only one expression in the installed package
  body. It preserves the installed credential, performs no attendance DML,
  and restores the exact original body if compilation or validation fails.
*/

declare
    l_previous_body clob;
    l_candidate_body clob;
    l_change_attempted boolean := false;
    l_status varchar2(30);
    l_errors number;
    l_old_expression constant varchar2(4000) := q'~nvl(d.cnic, chr(0))~';
    l_new_expression constant varchar2(4000) :=
        q'~nvl(to_char(d.cnic, 'TM9', 'NLS_NUMERIC_CHARACTERS=''.,'''), chr(0))~';

    function occurrence_count(p_source in clob, p_marker in varchar2)
        return pls_integer is
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
        select status into l_status
          from user_objects
         where object_name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and object_type = 'PACKAGE BODY';
        select count(*) into l_errors
          from user_errors
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY';
        if l_status <> 'VALID' or l_errors <> 0 then
            raise_application_error(-20891, 'Identity-repair package compilation failed.');
        end if;
    end validate_body;
begin
    validate_body;
    dbms_lob.createtemporary(l_previous_body, true);
    dbms_lob.writeappend(l_previous_body, length('create or replace '), 'create or replace ');
    for source_line in (
        select text from user_source
         where name = 'SLIC_ZKT_IDENTITY_REPAIR_API'
           and type = 'PACKAGE BODY'
         order by line
    ) loop
        dbms_lob.writeappend(l_previous_body, length(source_line.text), source_line.text);
    end loop;

    if occurrence_count(l_previous_body, l_new_expression) = 1
       and occurrence_count(l_previous_body, l_old_expression) = 0 then
        dbms_output.put_line('numeric_cnic_content_token=already_installed');
        dbms_output.put_line('attendance_rows_changed=0');
        return;
    end if;
    if occurrence_count(l_previous_body, l_old_expression) <> 1
       or occurrence_count(l_previous_body, l_new_expression) <> 0 then
        raise_application_error(-20890, 'Installed content-token expression does not match guarded patch.');
    end if;
    l_candidate_body := replace(l_previous_body, l_old_expression, l_new_expression);
    if occurrence_count(l_candidate_body, l_new_expression) <> 1
       or occurrence_count(l_candidate_body, l_old_expression) <> 0 then
        raise_application_error(-20890, 'Content-token replacement failed validation.');
    end if;
    l_change_attempted := true;
    execute immediate l_candidate_body;
    validate_body;
    dbms_output.put_line('numeric_cnic_content_token=installed');
    dbms_output.put_line('attendance_rows_changed=0');
exception
    when others then
        if l_change_attempted then
            begin
                execute immediate l_previous_body;
                validate_body;
            exception
                when others then
                    raise_application_error(-20892, 'Content-token install and automatic restoration both failed.');
            end;
        end if;
        raise;
end;
/
