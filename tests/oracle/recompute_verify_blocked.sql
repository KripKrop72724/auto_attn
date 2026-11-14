-- After the migration ran in RECOMP_BLOCKED, where the test trigger refused
-- the CREATE OR REPLACE (run_recompute_checks.py already required ORA-20978
-- and no ORA-20975). RECOMP_OLD holds the exact production source.
declare
    l_count number;
    l_attempts number;

    procedure require(p_ok in boolean, p_message in varchar2) is
    begin
        if not nvl(p_ok, false) then
            raise_application_error(-20160, 'ORACLE_RECOMPUTE_BLOCKED_DDL_FAILED ' || p_message);
        end if;
    end require;
begin
    -- The trigger draws one value per attempt, starting at 1.
    select recomp_blocked.recompute_ddl_attempts.nextval - 1 into l_attempts from dual;
    require(
        l_attempts = 1,
        'expected exactly one CREATE OR REPLACE attempt (no restoring retry), saw ' || l_attempts
    );

    with lines as (
        select owner,
               row_number() over (partition by owner order by line) line_no,
               rtrim(text, ' ' || chr(10) || chr(13)) text
          from dba_source
         where owner in ('RECOMP_BLOCKED', 'RECOMP_OLD')
           and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
           and type = 'PROCEDURE'
           and rtrim(text, ' ' || chr(10) || chr(13)) is not null
    )
    select count(*) into l_count from (
        (select line_no, text from lines where owner = 'RECOMP_BLOCKED'
         minus
         select line_no, text from lines where owner = 'RECOMP_OLD')
        union all
        (select line_no, text from lines where owner = 'RECOMP_OLD'
         minus
         select line_no, text from lines where owner = 'RECOMP_BLOCKED')
    );
    require(l_count = 0, 'the production procedure source was changed');
    select count(*) into l_count from dba_source
     where owner = 'RECOMP_BLOCKED' and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS';
    require(l_count > 100, 'the production procedure is missing');

    -- Step 2 ran before the replacement failed; a later run reuses the index.
    select count(*) into l_count from dba_indexes
     where owner = 'RECOMP_BLOCKED' and index_name = 'HR_RAW_ATTN_CNIC_DATE_I'
       and status = 'VALID';
    require(l_count = 1, 'the index from step 2 is missing');

    for broken in (
        select object_name, object_type, status from dba_objects
         where owner = 'RECOMP_BLOCKED' and status <> 'VALID'
    ) loop
        require(false, broken.object_name || ' ' || broken.object_type || ' is ' || broken.status);
    end loop;
    dbms_output.put_line(
        'ORACLE_RECOMPUTE_BLOCKED_DDL_PASSED replacement_attempts=' || l_attempts
        || ' production_source_kept=1 restore_skipped=1 index_from_step_2_kept=1'
        || ' all_objects_valid=1'
    );
end;
/
drop trigger recomp_blocked.block_recompute_replacement;
