-- After the migration ran twice in RECOMP_NEW, once in RECOMP_GUARD (which
-- already had an index starting with CNIC, ATTENDANCE_DATE) and was refused in
-- RECOMP_ODD (unexpected procedure source).
declare
    l_count number;

    procedure require(p_ok in boolean, p_message in varchar2) is
    begin
        if not nvl(p_ok, false) then
            raise_application_error(-20110, 'ORACLE_RECOMPUTE_MIGRATION_FAILED ' || p_message);
        end if;
    end require;

    function index_columns(p_owner in varchar2, p_index in varchar2) return varchar2 is
        l_columns varchar2(400);
    begin
        select listagg(column_name, ',') within group (order by column_position)
          into l_columns
          from dba_ind_columns
         where index_owner = p_owner
           and index_name = p_index
           and table_name = 'HR_RAW_ATTN_CAPTURE_EVENTS';
        return l_columns;
    end index_columns;

    function index_count(p_owner in varchar2) return number is
        l_total number;
    begin
        select count(*) into l_total from dba_indexes
         where table_owner = p_owner and table_name = 'HR_RAW_ATTN_CAPTURE_EVENTS';
        return l_total;
    end index_count;

    function source_has(p_owner in varchar2, p_fragment in varchar2) return boolean is
        l_hits number;
    begin
        select count(*) into l_hits from dba_source
         where owner = p_owner
           and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
           and type = 'PROCEDURE'
           and instr(text, p_fragment) > 0;
        return l_hits > 0;
    end source_has;
begin
    require(
        index_columns('RECOMP_NEW', 'HR_RAW_ATTN_CNIC_DATE_I') = 'CNIC,ATTENDANCE_DATE',
        'NEW lacks HR_RAW_ATTN_CNIC_DATE_I on (CNIC, ATTENDANCE_DATE)'
    );
    select count(*) into l_count from dba_indexes
     where owner = 'RECOMP_NEW' and index_name = 'HR_RAW_ATTN_CNIC_DATE_I'
       and status = 'VALID' and visibility = 'VISIBLE';
    require(l_count = 1, 'NEW index is not VALID and VISIBLE');
    require(index_count('RECOMP_NEW') = 3, 'NEW must have exactly PK, UK and the new index');
    require(index_count('RECOMP_OLD') = 2, 'OLD must keep only PK and UK');

    require(index_count('RECOMP_GUARD') = 3, 'GUARD must keep PK, UK and its own index only');
    -- A TIMESTAMP WITH TIME ZONE key is stored as a hidden SYS_NC expression column.
    require(
        index_columns('RECOMP_GUARD', 'HR_RAW_ATTN_DAY_IDX') like 'CNIC,ATTENDANCE_DATE,%',
        'GUARD lost its existing index'
    );
    require(
        index_columns('RECOMP_GUARD', 'HR_RAW_ATTN_CNIC_DATE_I') is null,
        'GUARD received a duplicate index'
    );

    require(index_count('RECOMP_ODD') = 2, 'ODD (refused) must not receive an index');
    require(
        source_has('RECOMP_ODD', 'order by attendance_date, cnic'),
        'ODD procedure was changed although the migration refused'
    );

    require(source_has('RECOMP_NEW', 'l_cnics(i)'), 'NEW procedure was not replaced');
    require(source_has('RECOMP_GUARD', 'l_cnics(i)'), 'GUARD procedure was not replaced');
    require(not source_has('RECOMP_OLD', 'l_cnics(i)'), 'OLD procedure must stay production');

    for broken in (
        select owner, object_name, object_type, status
          from dba_objects
         where owner in ('RECOMP_OLD', 'RECOMP_NEW', 'RECOMP_GUARD', 'RECOMP_ODD', 'RECOMP_LAB')
           and status <> 'VALID'
    ) loop
        require(
            false,
            broken.owner || '.' || broken.object_name || ' ' || broken.object_type
            || ' is ' || broken.status
        );
    end loop;

    for dependent in (
        select owner, name, type object_type
          from dba_dependencies
         where referenced_owner = 'RECOMP_NEW'
           and referenced_name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
         order by owner, name, object_type
    ) loop
        dbms_output.put_line(
            'recompute_dependent ' || dependent.owner || '.' || dependent.name || ' '
            || dependent.object_type
        );
    end loop;

    dbms_output.put_line(
        'ORACLE_RECOMPUTE_MIGRATION_PASSED new_index=HR_RAW_ATTN_CNIC_DATE_I(CNIC,ATTENDANCE_DATE)'
        || ' guard_kept=HR_RAW_ATTN_DAY_IDX odd_refused_unchanged=1 all_objects_valid=1'
    );
end;
/
