set define off
set serveroutput on
whenever sqlerror exit failure rollback

/*
  Revalidate the ZKT downstream-repair package after the production schema and
  its dependent packages have been changed.

  Oracle can retain an INVALID package body after a dependency change even
  when the checked-in source is correct.  Recompiling the body changes only
  Oracle object metadata; this migration invokes no repair procedure and does
  no attendance-table DML.
*/
alter package slic_zkt_downstream_repair compile body;

declare
    l_status varchar2(30);
    l_errors number;
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
            -20990,
            'SLIC_ZKT_DOWNSTREAM_REPAIR package body did not compile cleanly.'
        );
    end if;

    dbms_output.put_line('slic_zkt_downstream_repair=VALID');
    dbms_output.put_line('attendance_rows_changed=0');
end;
/
