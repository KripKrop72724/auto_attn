-- RECOMP_BLOCKED holds the exact production procedure, but a test-only DDL
-- trigger refuses every CREATE of SLIC_ZKT_RECOMPUTE_DAILY_FLAGS there and
-- counts each attempt. It stands in for a replacement that never happens in
-- production (ORA-04021 or ORA-04020 while running calls keep the procedure
-- in use): the migration must report ORA-20978, leave the production version
-- in place and must not try a second CREATE OR REPLACE to "restore" it.
create sequence recomp_blocked.recompute_ddl_attempts start with 1 increment by 1 nocache;
grant administer database trigger to recomp_blocked;

create or replace trigger recomp_blocked.block_recompute_replacement
before create on database
declare
    l_attempt number;
begin
    if ora_dict_obj_owner = 'RECOMP_BLOCKED' and ora_dict_obj_type = 'PROCEDURE' then
        if ora_dict_obj_name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS' then
            l_attempt := recomp_blocked.recompute_ddl_attempts.nextval;
            raise_application_error(
                -20990,
                'Test trigger: replacing the procedure is blocked (attempt ' || l_attempt || ').'
            );
        elsif ora_dict_obj_name = 'RECOMPUTE_DDL_PROBE' then
            raise_application_error(-20991, 'Test trigger: probe blocked.');
        end if;
    end if;
end;
/

-- The migration runs as SYS with CURRENT_SCHEMA switched, so first prove the
-- trigger fires for exactly that kind of session.
declare
    l_fired boolean := false;
begin
    begin
        execute immediate
            'create or replace procedure recomp_blocked.recompute_ddl_probe as begin null; end;';
    exception
        when others then
            if instr(dbms_utility.format_error_stack, 'ORA-20991') > 0 then
                l_fired := true;
            else
                raise;
            end if;
    end;
    if not l_fired then
        raise_application_error(
            -20142,
            'The test DDL trigger did not fire for this session; cannot test a blocked replacement.'
        );
    end if;
    dbms_output.put_line('recompute_blocked_trigger=fires');
end;
/
