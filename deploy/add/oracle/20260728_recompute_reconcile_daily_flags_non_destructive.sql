set define off
set serveroutput on

/*
  Patch package-backed authoritative reconciliation to use the same valid
  non-destructive whole-day flag helper as live/bulk ingestion.

  Preconditions:
  - the helper exists and is VALID;
  - live ingestion already uses the helper exactly once;
  - the reconcile insert and inline reconcile MERGE still have the expected
    pre-patch shape; and
  - both legacy DELETE paths remain gated.

  The migration performs no attendance table DML and restores the exact
  original package body automatically on any failure.
*/

declare
    l_body                    clob;
    l_patched_body            clob;
    l_original_ddl            clob;
    l_patched_ddl             clob;
    l_body_status             varchar2(30);
    l_compile_errors          number;
    l_helper_status           varchar2(30);
    l_helper_errors           number;
    l_ddl_attempted           boolean := false;
    l_reconcile_start         integer;
    l_insert_marker_start     integer;
    l_merge_start             integer;
    l_merge_end_start         integer;
    l_merge_end               integer;

    c_flag_pattern constant varchar2(4000) :=
        q'~case when i\.raw_punch = 'F' and n\.rn_in = 1 then 'T' else 'F' end check_in,[[:space:]]+case when i\.raw_punch = 'F' and n\.normal_count > 1 and n\.rn_out = 1 then 'T' else 'F' end check_out~';
    c_helper_call constant varchar2(4000) :=
        q'~slic_zkt_recompute_daily_flags\(p_body\);~';
    c_insert_marker constant varchar2(100) := 'v_inserted := sql%rowcount;';
    c_merge_marker constant varchar2(100) :=
        'merge into hr_raw_attn_capture_events d';
    c_merge_end_marker constant varchar2(100) :=
        'v_flag_corrected := sql%rowcount;';

    procedure restore_original is
    begin
        execute immediate l_original_ddl;
        l_ddl_attempted := false;
    end restore_original;

    function replace_clob_segment(
        p_source in clob,
        p_start in integer,
        p_end in integer,
        p_replacement in varchar2
    ) return clob
    is
        l_result clob;
        l_prefix_length integer := p_start - 1;
        l_tail_length integer := dbms_lob.getlength(p_source) - p_end;
    begin
        dbms_lob.createtemporary(l_result, true);
        if l_prefix_length > 0 then
            dbms_lob.copy(
                l_result,
                p_source,
                l_prefix_length,
                1,
                1
            );
        end if;
        dbms_lob.writeappend(
            l_result,
            length(p_replacement),
            p_replacement
        );
        if l_tail_length > 0 then
            dbms_lob.copy(
                l_result,
                p_source,
                l_tail_length,
                dbms_lob.getlength(l_result) + 1,
                p_end + 1
            );
        end if;
        return l_result;
    end replace_clob_segment;
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
            -20431,
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

    l_reconcile_start := dbms_lob.instr(
        lower(l_body),
        'procedure post_reconcile',
        1
    );
    l_insert_marker_start := dbms_lob.instr(
        lower(l_body),
        c_insert_marker,
        l_reconcile_start
    );
    l_merge_start := dbms_lob.instr(
        lower(l_body),
        c_merge_marker,
        l_insert_marker_start
    );
    l_merge_end_start := dbms_lob.instr(
        lower(l_body),
        c_merge_end_marker,
        l_merge_start
    );

    if dbms_lob.getlength(l_body) = 0
       or l_reconcile_start = 0
       or l_insert_marker_start = 0
       or l_merge_start = 0
       or l_merge_end_start = 0
       or regexp_count(l_body, c_flag_pattern, l_reconcile_start, 'i') <> 2
       or regexp_count(l_body, c_helper_call, 1, 'i') <> 1
       or regexp_count(
           l_body,
           q'~'F' check_in,[[:space:]]+'F' check_out~',
           1,
           'c'
       ) <> 1
       or regexp_count(
           l_body,
           q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
           1,
           'i'
       ) <> 2 then
        raise_application_error(
            -20432,
            'Deployed package was not the expected live-helper/reconcile-inline shape.'
        );
    end if;

    -- The first remaining pair is the reconcile INSERT. The second is inside
    -- the inline reconcile MERGE that is replaced below.
    l_patched_body := regexp_replace(
        l_body,
        c_flag_pattern,
        '''F'' check_in,'
            || chr(10)
            || '                   ''F'' check_out',
        l_reconcile_start,
        1,
        'i'
    );

    -- Recalculate offsets after replacing the flag expressions; CLOB offsets
    -- after that point may have changed.
    l_reconcile_start := dbms_lob.instr(
        lower(l_patched_body),
        'procedure post_reconcile',
        1
    );
    l_insert_marker_start := dbms_lob.instr(
        lower(l_patched_body),
        c_insert_marker,
        l_reconcile_start
    );
    l_merge_start := dbms_lob.instr(
        lower(l_patched_body),
        c_merge_marker,
        l_insert_marker_start
    );
    l_merge_end_start := dbms_lob.instr(
        l_patched_body,
        c_merge_end_marker,
        l_merge_start
    );
    l_merge_end := l_merge_end_start + length(c_merge_end_marker) - 1;

    l_patched_body := replace_clob_segment(
        l_patched_body,
        l_merge_start,
        l_merge_end,
        '-- Reconcile uses deterministic whole-day flags.'
            || chr(10)
            || '        slic_zkt_recompute_daily_flags(p_body);'
            || chr(10)
            || '        v_flag_corrected := 0;'
    );

    if regexp_count(l_patched_body, c_helper_call, 1, 'i') <> 2
       or regexp_count(l_patched_body, c_flag_pattern, 1, 'i') <> 0
       or regexp_count(
           l_patched_body,
           q'~'F' check_in,[[:space:]]+'F' check_out~',
           1,
           'c'
       ) <> 2
       or regexp_count(
           l_patched_body,
           q'~delete from hr_raw_attn_capture_events d[[:space:]]+where 1 = 0~',
           1,
           'i'
       ) <> 2 then
        raise_application_error(
            -20433,
            'Patched package failed helper/non-destructive source invariants.'
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
            -20434,
            'Patched package failed compilation and was restored.'
        );
    end if;

    l_ddl_attempted := false;
    dbms_output.put_line(
        'Reconcile now uses the valid non-destructive daily flag helper; package body is VALID.'
    );
exception
    when others then
        if l_ddl_attempted then
            begin
                restore_original;
            exception
                when others then
                    raise_application_error(
                        -20435,
                        'Migration failed and automatic package restoration also failed.'
                    );
            end;
        end if;
        raise;
end;
/
