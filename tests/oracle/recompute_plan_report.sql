-- V$SQL for the statements each procedure ran during the timing phase (the
-- shared pool is flushed just before it), with their plans. NEW must reach
-- HR_RAW_ATTN_CAPTURE_EVENTS only through indexes, never a full scan.
declare
    l_plan varchar2(4000);
    l_new_full_scans number := 0;
    l_new_index_statements number := 0;
    l_new_statements number := 0;
begin
    -- Attribute cursors by PARSING_SCHEMA_NAME (per child): OLD and NEW issue the
    -- identical lock SELECT text, so they share one parent cursor and PROGRAM_ID.
    for stmt in (
        select parsing_schema_name owner, sql_id, child_number, executions, elapsed_time,
               cpu_time, buffer_gets,
               regexp_replace(substr(sql_text, 1, 60), '[[:space:]]+', ' ') text,
               case when instr(sql_text, 'HR_RAW_ATTN_CAPTURE_EVENTS') > 0 then 1 else 0 end
                   reads_table
          from v$sql
         where parsing_schema_name in ('RECOMP_OLD', 'RECOMP_NEW')
           and executions > 0
           and (instr(sql_text, 'HR_RAW_ATTN_CAPTURE_EVENTS') > 0
                or sql_text like 'SELECT DISTINCT TRIM(J.CNIC)%')
         order by parsing_schema_name desc, sql_text, child_number
    ) loop
        l_plan := null;
        for step in (
            select operation, options, object_name
              from v$sql_plan
             where sql_id = stmt.sql_id
               and child_number = stmt.child_number
             order by id
        ) loop
            if step.object_name is not null then
                l_plan := substr(
                    l_plan || ' | ' || step.operation || ' ' || step.options || ' '
                    || step.object_name,
                    1,
                    3000
                );
            end if;
            if stmt.owner = 'RECOMP_NEW'
               and step.operation = 'TABLE ACCESS'
               and step.options like '%FULL%'
               and step.object_name = 'HR_RAW_ATTN_CAPTURE_EVENTS' then
                l_new_full_scans := l_new_full_scans + 1;
            end if;
        end loop;
        if stmt.owner = 'RECOMP_NEW' and stmt.reads_table = 1 then
            l_new_statements := l_new_statements + 1;
            if instr(l_plan, 'HR_RAW_ATTN_CNIC_DATE_I') > 0 then
                l_new_index_statements := l_new_index_statements + 1;
            end if;
        end if;
        dbms_output.put_line(
            'recompute_sql ' || substr(stmt.owner, 8) || ' "' || stmt.text || '" execs='
            || stmt.executions
            || ' avg_ms=' || round(stmt.elapsed_time / stmt.executions / 1000, 3)
            || ' avg_cpu_ms=' || round(stmt.cpu_time / stmt.executions / 1000, 3)
            || ' avg_gets=' || round(stmt.buffer_gets / stmt.executions, 1)
        );
        dbms_output.put_line('recompute_plan ' || substr(stmt.owner, 8) || l_plan);
    end loop;

    if l_new_full_scans > 0 then
        raise_application_error(
            -20120,
            'ORACLE_RECOMPUTE_PLAN_FAILED NEW full-scanned HR_RAW_ATTN_CAPTURE_EVENTS '
            || l_new_full_scans || ' time(s)'
        );
    end if;
    -- Lock SELECT, UPDATE and MERGE all read the table.
    if l_new_statements < 3 or l_new_index_statements < 3 then
        raise_application_error(
            -20121,
            'ORACLE_RECOMPUTE_PLAN_FAILED only ' || l_new_index_statements || ' of '
            || l_new_statements || ' NEW table statements used HR_RAW_ATTN_CNIC_DATE_I'
        );
    end if;
    dbms_output.put_line(
        'ORACLE_RECOMPUTE_PLAN_PASSED new_table_statements=' || l_new_statements
        || ' using_hr_raw_attn_cnic_date_i=' || l_new_index_statements
        || ' new_full_scans=0'
    );
end;
/
