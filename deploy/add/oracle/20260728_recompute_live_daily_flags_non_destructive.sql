set define off
set serveroutput on

/*
  Patch package-backed live/bulk insertion to use the valid daily flag helper.

  Preconditions:
  - the helper exists and is VALID;
  - the package is the expected triple-verifier version;
  - both legacy DELETE paths remain gated; and
  - live insertion has not already been patched.

  The migration performs no attendance table DML and restores the exact
  original package body automatically on any failure.
*/

declare
    l_body                 clob;
    l_patched_body         clob;
    l_original_ddl         clob;
    l_patched_ddl          clob;
    l_body_status          varchar2(30);
    l_compile_errors       number;
    l_helper_status        varchar2(30);
    l_helper_errors        number;
    l_ddl_attempted        boolean := false;

    c_flag_pattern constant varchar2(4000) :=
        q'~case when i\.raw_punch = 'F' and n\.rn_in = 1 then 'T' else 'F' end check_in,[[:space:]]+case when i\.raw_punch = 'F' and n\.normal_count > 1 and n\.rn_out = 1 then 'T' else 'F' end check_out~';
    c_insert_marker_pattern constant varchar2(4000) :=
        q'~(v_inserted := sql%rowcount;)~';
    c_helper_call constant varchar2(4000) :=
        q'~slic_zkt_recompute_daily_flags\(p_body\);~';

    procedure restore_original is
    begin
        execute immediate l_original_ddl;
        l_ddl_attempted := false;
    end restore_original;
begin
    select status
      into l_helper_status
      from user_objects
     where object_name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
       and object_type = 'PROCEDURE';

    select count(*)
      into l_helper_errors
      from user_errors
     where name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
       and type = 'PROCEDURE';

    if l_helper_status <> 'VALID' or l_helper_errors <> 0 then
        raise_application_error(
            -20421,
            'Daily flag helper is missing or invalid.'
        );
    end if;

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

    if dbms_lob.getlength(l_body) = 0
       or regexp_count(l_body, c_flag_pattern, 1, 'i') <> 3
       or regexp_count(l_body, c_insert_marker_pattern, 1, 'i') <> 2
       or regexp_count(l_body, c_helper_call, 1, 'i') <> 0
       or regexp_count(
           l_body,
           'v_password_digest = c_add_api_password_sha256',
           1,
           'i'
       ) <> 1
       or regexp_count(
           l_body,
           q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
           1,
           'i'
       ) <> 2 then
        raise_application_error(
            -20422,
            'Deployed package was not the expected triple-verifier pre-helper shape.'
        );
    end if;

    l_patched_body := regexp_replace(
        l_body,
        c_flag_pattern,
        '''F'' check_in,'
            || chr(10)
            || '                   ''F'' check_out',
        1,
        1,
        'i'
    );
    l_patched_body := regexp_replace(
        l_patched_body,
        c_insert_marker_pattern,
        '\1'
            || chr(10)
            || chr(10)
            || '        if v_inserted > 0 then'
            || chr(10)
            || '            -- Non-destructive daily flag recomputation.'
            || chr(10)
            || '            slic_zkt_recompute_daily_flags(p_body);'
            || chr(10)
            || '        end if;',
        1,
        1,
        'i'
    );

    if regexp_count(l_patched_body, c_helper_call, 1, 'i') <> 1
       or regexp_count(
           l_patched_body,
           q'~'F' check_in,[[:space:]]+'F' check_out~',
           1,
           'c'
       ) <> 1
       or regexp_count(
           l_patched_body,
           q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
           1,
           'i'
       ) <> 2 then
        raise_application_error(
            -20423,
            'Patched package failed the helper/non-destructive source invariant'
                || ' (helper_calls='
                || regexp_count(l_patched_body, c_helper_call, 1, 'i')
                || ', false_flag_pairs='
                || regexp_count(
                    l_patched_body,
                    q'~'F' check_in,[[:space:]]+'F' check_out~',
                    1,
                    'c'
                )
                || ', delete_gates='
                || regexp_count(
                    l_patched_body,
                    q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
                    1,
                    'i'
                )
                || ').'
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
            -20424,
            'Patched package failed compilation and was restored.'
        );
    end if;

    l_ddl_attempted := false;
    dbms_output.put_line(
        'Live inserts now call the valid non-destructive daily flag helper; package body is VALID.'
    );
exception
    when others then
        if l_ddl_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20425,
                        'Migration failed and automatic package restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
