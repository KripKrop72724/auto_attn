set define off
set serveroutput on

/*
  Bound repeated authoritative reconcile CPU on the shared Oracle server.

  Production profiling showed that an already-present daily truth window still
  executed SLIC_ZKT_RECOMPUTE_DAILY_FLAGS and rewrote DATASYNC. Repeated device
  windows therefore consumed millions of buffer gets without changing truth.

  This migration changes only the deployed package body:
  - recompute whole-day flags only when reconciliation changed the stored event
    set; and
  - update DATASYNC only for matching rows whose value is not already zero.

  It performs no attendance-table DML, preserves both non-destructive DELETE
  gates, validates the exact source shape, and restores the original package
  body automatically if compilation or any invariant fails.
*/

declare
    o clob;
    p clob;
    s varchar2(30);
    e number;
    r integer;
    attempted boolean := false;
    old_flags varchar2(500) :=
        'slic_zkt_recompute_daily_flags\(p_body\);[[:space:]]+v_flag_corrected := 0;';
    new_flags varchar2(500) :=
        '        if v_inserted + v_corrected + v_deleted > 0 then' || chr(10) ||
        '            slic_zkt_recompute_daily_flags(p_body);' || chr(10) ||
        '        end if;' || chr(10) ||
        '        v_flag_corrected := 0;';
    old_sync varchar2(500) :=
        'set d\.datasync = 0[[:space:]]+where exists \(';
    new_sync varchar2(500) :=
        '           set d.datasync = 0' || chr(10) ||
        '         where nvl(d.datasync, 0) <> 0' || chr(10) ||
        '           and exists (';

    procedure restore_original is
    begin
        execute immediate o;
        attempted := false;
    end;
begin
    select dbms_metadata.get_ddl(
               'PACKAGE_BODY',
               'SLIC_ZKT_TRUTH_API',
               'SLIC_HRM'
           )
      into o
      from dual;

    r := dbms_lob.instr(lower(o), 'procedure post_reconcile');
    if r = 0
       or regexp_count(o, old_flags, r, 'i') <> 1
       or regexp_count(o, old_sync, r, 'i') <> 1
       or regexp_count(
              o,
              'delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0',
              1,
              'i'
          ) <> 2 then
        raise_application_error(-20501, 'Unexpected pre-patch package shape.');
    end if;

    p := regexp_replace(o, old_flags, new_flags, r, 1, 'i');
    p := regexp_replace(p, old_sync, new_sync, r, 1, 'i');
    if regexp_count(
           p,
           'if v_inserted \+ v_corrected \+ v_deleted > 0 then[[:space:]]+slic_zkt_recompute_daily_flags\(p_body\);[[:space:]]+end if;',
           r,
           'i'
       ) <> 1
       or regexp_count(
              p,
              'set d\.datasync = 0[[:space:]]+where nvl\(d\.datasync, 0\) <> 0[[:space:]]+and exists \(',
              r,
              'i'
          ) <> 1 then
        raise_application_error(-20502, 'Unexpected post-patch package shape.');
    end if;

    attempted := true;
    execute immediate p;
    select status
      into s
      from user_objects
     where object_name = 'SLIC_ZKT_TRUTH_API'
       and object_type = 'PACKAGE BODY';
    select count(*)
      into e
      from user_errors
     where name = 'SLIC_ZKT_TRUTH_API'
       and type = 'PACKAGE BODY';
    if s <> 'VALID' or e <> 0 then
        restore_original;
        raise_application_error(-20503, 'Invalid patch restored.');
    end if;
    attempted := false;
    dbms_output.put_line('No-op reconcile CPU bounded; package VALID.');
exception
    when others then
        if attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(-20504, 'Patch and restore failed.');
            end;
        end if;
        raise;
end;
/
