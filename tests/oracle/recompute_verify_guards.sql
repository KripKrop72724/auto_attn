-- After the migration was refused in RECOMP_BUSY (a long call was running in
-- SLIC_ZKT_IDENTITY_REPAIR_API) and failed validation after the replacement
-- in RECOMP_RESTORE (run_recompute_checks.py already required ORA-20970 and
-- ORA-20979 without ORA-20975). RECOMP_OLD holds the exact production source.
declare
    l_count number;
    l_attempts number;

    procedure require(p_ok in boolean, p_message in varchar2) is
    begin
        if not nvl(p_ok, false) then
            raise_application_error(-20170, 'ORACLE_RECOMPUTE_GUARDS_FAILED ' || p_message);
        end if;
    end require;

    -- Lines of the procedure that differ from RECOMP_OLD, either way.
    function source_differences(p_owner in varchar2) return number is
        l_differences number;
    begin
        with lines as (
            select owner,
                   row_number() over (partition by owner order by line) line_no,
                   rtrim(text, ' ' || chr(10) || chr(13)) text
              from dba_source
             where owner in (p_owner, 'RECOMP_OLD')
               and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
               and type = 'PROCEDURE'
               and rtrim(text, ' ' || chr(10) || chr(13)) is not null
        )
        select count(*) into l_differences from (
            (select line_no, text from lines where owner = p_owner
             minus
             select line_no, text from lines where owner = 'RECOMP_OLD')
            union all
            (select line_no, text from lines where owner = 'RECOMP_OLD'
             minus
             select line_no, text from lines where owner = p_owner)
        );
        return l_differences;
    end source_differences;

    procedure require_all_valid(p_owner in varchar2) is
    begin
        for broken in (
            select object_name, object_type, status from dba_objects
             where owner = p_owner and status <> 'VALID'
        ) loop
            require(false, p_owner || '.' || broken.object_name || ' ' || broken.object_type
                || ' is ' || broken.status);
        end loop;
    end require_all_valid;
begin
    -- Busy: the guard runs before step 2, so neither the procedure nor the
    -- indexes changed.
    require(source_differences('RECOMP_BUSY') = 0, 'BUSY procedure source was changed');
    select count(*) into l_count from dba_source
     where owner = 'RECOMP_BUSY' and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS';
    require(l_count > 100, 'BUSY procedure is missing');
    select count(*) into l_count from dba_indexes
     where table_owner = 'RECOMP_BUSY' and table_name = 'HR_RAW_ATTN_CAPTURE_EVENTS';
    require(l_count = 2, 'BUSY must keep only PK and UK (no index created), saw ' || l_count);
    require_all_valid('RECOMP_BUSY');
    dbms_output.put_line(
        'ORACLE_RECOMPUTE_BUSY_GUARD_PASSED production_source_kept=1 index_not_created=1'
        || ' all_objects_valid=1'
    );

    -- Restore: the replacement happened, validation failed, and the exact
    -- production procedure came back through one more CREATE OR REPLACE.
    select recomp_restore.recompute_ddl_attempts.nextval - 1 into l_attempts from dual;
    require(
        l_attempts = 2,
        'expected the replacement plus one restoring CREATE OR REPLACE, saw ' || l_attempts
    );
    require(source_differences('RECOMP_RESTORE') = 0, 'RESTORE production source not restored');
    select count(*) into l_count from dba_source
     where owner = 'RECOMP_RESTORE' and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS';
    require(l_count > 100, 'RESTORE procedure is missing');
    select count(*) into l_count from dba_source
     where owner = 'RECOMP_RESTORE' and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
       and instr(text, 'l_cnics(i)') > 0;
    require(l_count = 0, 'RESTORE still has the per-employee-day procedure');
    select count(*) into l_count from dba_indexes
     where owner = 'RECOMP_RESTORE' and index_name = 'HR_RAW_ATTN_CNIC_DATE_I'
       and status = 'VALID';
    require(l_count = 1, 'RESTORE lost the index from step 2');
    select count(*) into l_count from dba_tab_columns
     where owner = 'RECOMP_RESTORE' and table_name = 'RECOMPUTE_RESTORE_PROBE'
       and column_name = 'PROBE_VALUE';
    require(l_count = 1, 'RESTORE test column was not renamed back by the restoring CREATE');
    select count(*) into l_count from dba_errors where owner = 'RECOMP_RESTORE';
    require(l_count = 0, 'RESTORE objects still have compile errors');
    require_all_valid('RECOMP_RESTORE');
    dbms_output.put_line(
        'ORACLE_RECOMPUTE_RESTORE_PASSED replacement_attempts=' || l_attempts
        || ' production_source_restored=1 index_from_step_2_kept=1 all_objects_valid=1'
    );
end;
/
drop trigger recomp_restore.fault_after_replacement;
