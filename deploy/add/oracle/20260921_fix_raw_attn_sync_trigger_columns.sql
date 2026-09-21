set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Repair the production raw-capture insert trigger after the attendance table
  schema was found not to contain CREATED_IP or UPDATED_IP.

  This migration changes trigger source only. It performs no INSERT, UPDATE,
  DELETE, MERGE, or attendance backfill. It is guarded against an unexpected
  trigger body and restores the exact previous trigger automatically if the
  replacement or validation fails.
*/
declare
    l_previous_ddl clob;
    l_source clob;
    l_candidate clob;
    l_status varchar2(30);
    l_errors number;
    l_has_created_ip number;
    l_has_updated_ip number;
    l_old_update constant varchar2(4000) :=
        'updated_by = nvl(v(''APP_USER''), user),' || chr(10) ||
        'updated_ip = sys_context(''USERENV'', ''IP_ADDRESS'')';
    l_new_update constant varchar2(4000) :=
        'updated_by = nvl(v(''APP_USER''), user)';
    l_old_created constant varchar2(4000) :=
        'created_ip,' || chr(10) ||
        'marked_by,';
    l_new_created constant varchar2(4000) :=
        'marked_by,';
    l_old_created_value constant varchar2(4000) :=
        'sys_context(''USERENV'', ''IP_ADDRESS''),' || chr(10) ||
        '''BIOMETRIC'',';
    l_new_created_value constant varchar2(4000) :=
        '''BIOMETRIC'',';

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

    procedure validate_trigger is
    begin
        select status
          into l_status
          from user_objects
         where object_name = 'TRG_RAW_ATTN_SYNC_BI'
           and object_type = 'TRIGGER';
        select count(*)
          into l_errors
          from user_errors
         where name = 'TRG_RAW_ATTN_SYNC_BI'
           and type = 'TRIGGER';
        if l_status <> 'VALID' or l_errors <> 0 then
            raise_application_error(-20981, 'Raw attendance sync trigger validation failed.');
        end if;
    end validate_trigger;
begin
    select count(*)
      into l_has_created_ip
      from user_tab_columns
     where table_name = 'HR_EMPLOYEE_ATTENDANCE'
       and column_name = 'CREATED_IP';
    select count(*)
      into l_has_updated_ip
      from user_tab_columns
     where table_name = 'HR_EMPLOYEE_ATTENDANCE'
       and column_name = 'UPDATED_IP';
    if l_has_created_ip <> 0 or l_has_updated_ip <> 0 then
        raise_application_error(-20980, 'Attendance table schema differs; trigger repair was not applied.');
    end if;

    dbms_lob.createtemporary(l_previous_ddl, true);
    dbms_lob.createtemporary(l_source, true);
    dbms_lob.writeappend(l_previous_ddl, length('create or replace '), 'create or replace ');
    dbms_lob.writeappend(l_source, length('create or replace '), 'create or replace ');
    for source_line in (
        select text
          from user_source
         where name = 'TRG_RAW_ATTN_SYNC_BI'
           and type = 'TRIGGER'
         order by line
    ) loop
        dbms_lob.writeappend(l_previous_ddl, length(source_line.text), source_line.text);
        dbms_lob.writeappend(l_source, length(trim(source_line.text) || chr(10)), trim(source_line.text) || chr(10));
    end loop;

    if occurrence_count(l_source, l_new_update) = 1
       and occurrence_count(l_source, l_old_update) = 0
       and occurrence_count(l_source, l_old_created) = 0
       and occurrence_count(l_source, l_old_created_value) = 0 then
        validate_trigger;
        dbms_output.put_line('raw_attn_sync_trigger=already_fixed');
        dbms_output.put_line('attendance_rows_changed=0');
        return;
    end if;

    if occurrence_count(l_source, l_old_update) <> 1
       or occurrence_count(l_source, l_old_created) <> 1
       or occurrence_count(l_source, l_old_created_value) <> 1
       or occurrence_count(l_source, l_new_update) <> 0 then
        raise_application_error(-20980, 'Installed trigger body did not match the guarded repair shape.');
    end if;

    l_candidate := replace(l_source, l_old_update, l_new_update);
    l_candidate := replace(l_candidate, l_old_created, l_new_created);
    l_candidate := replace(l_candidate, l_old_created_value, l_new_created_value);
    execute immediate l_candidate;
    validate_trigger;
    dbms_output.put_line('raw_attn_sync_trigger=fixed');
    dbms_output.put_line('attendance_rows_changed=0');
exception
    when others then
        if l_previous_ddl is not null then
            begin
                execute immediate l_previous_ddl;
                validate_trigger;
            exception
                when others then
                    raise_application_error(-20982, 'Trigger repair and automatic restoration both failed.');
            end;
        end if;
        raise;
end;
/
