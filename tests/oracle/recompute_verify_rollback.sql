-- After the rollback script ran in RECOMP_NEW and RECOMP_GUARD. RECOMP_OLD was
-- created from the exact production copy, so its USER_SOURCE is the reference.
declare
    l_count number;

    procedure require(p_ok in boolean, p_message in varchar2) is
    begin
        if not nvl(p_ok, false) then
            raise_application_error(-20130, 'ORACLE_RECOMPUTE_ROLLBACK_FAILED ' || p_message);
        end if;
    end require;

    -- Every non-blank line identical and in the same order (indentation kept;
    -- SQL*Plus may drop blank lines when it creates the reference copy).
    function source_diff(p_owner in varchar2) return number is
        l_diff number;
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
        select count(*) into l_diff from (
            (select line_no, text from lines where owner = p_owner
             minus
             select line_no, text from lines where owner = 'RECOMP_OLD')
            union all
            (select line_no, text from lines where owner = 'RECOMP_OLD'
             minus
             select line_no, text from lines where owner = p_owner)
        );
        return l_diff;
    end source_diff;
begin
    select count(*) into l_count from dba_source
     where owner = 'RECOMP_OLD' and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS' and type = 'PROCEDURE';
    require(l_count > 100, 'reference production source missing');
    require(source_diff('RECOMP_NEW') = 0, 'NEW source is not the exact production procedure');
    require(source_diff('RECOMP_GUARD') = 0, 'GUARD source is not the exact production procedure');

    select count(*) into l_count from dba_indexes
     where owner = 'RECOMP_NEW' and index_name = 'HR_RAW_ATTN_CNIC_DATE_I';
    require(l_count = 0, 'NEW still has HR_RAW_ATTN_CNIC_DATE_I');
    select count(*) into l_count from dba_indexes
     where owner = 'RECOMP_GUARD' and index_name = 'HR_RAW_ATTN_DAY_IDX';
    require(l_count = 1, 'rollback dropped an index it did not create');

    for broken in (
        select owner, object_name, object_type, status
          from dba_objects
         where owner in ('RECOMP_OLD', 'RECOMP_NEW', 'RECOMP_GUARD')
           and status <> 'VALID'
    ) loop
        require(
            false,
            broken.owner || '.' || broken.object_name || ' ' || broken.object_type
            || ' is ' || broken.status
        );
    end loop;
    dbms_output.put_line(
        'ORACLE_RECOMPUTE_ROLLBACK_PASSED exact_production_source=1 index_dropped=1'
        || ' foreign_index_kept=1 all_objects_valid=1'
    );
end;
/
