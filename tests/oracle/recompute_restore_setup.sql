-- RECOMP_RESTORE holds the exact production procedure. A test-only DDL
-- trigger lets the migration's CREATE OR REPLACE succeed, but just before it
-- recompiles SLIC_ZKT_IDENTITY_REPAIR_API with a conditional-compilation flag
-- that breaks it. Validation after the replacement then fails, and the
-- migration must restore the exact previous procedure and say so with
-- ORA-20979 and the compile errors. When the restore re-creates the
-- procedure, the trigger recompiles the package without the flag, so
-- everything must end VALID with the production source.
create or replace package recomp_restore.slic_zkt_identity_repair_api authid definer as
    procedure recompute_probe(p_body in clob);
    $if $$recompute_fault $then
    this_declaration_does_not_compile;
    $end
end slic_zkt_identity_repair_api;
/
alter package recomp_restore.slic_zkt_identity_repair_api compile body;

create sequence recomp_restore.recompute_ddl_attempts start with 1 increment by 1 nocache;
grant administer database trigger to recomp_restore;

create or replace trigger recomp_restore.fault_after_replacement
before create on database
declare
    pragma autonomous_transaction;
    l_attempt number;
begin
    if ora_dict_obj_owner = 'RECOMP_RESTORE'
       and ora_dict_obj_type = 'PROCEDURE'
       and ora_dict_obj_name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS' then
        l_attempt := recomp_restore.recompute_ddl_attempts.nextval;
        begin
            execute immediate
                'alter package recomp_restore.slic_zkt_identity_repair_api'
                || ' compile specification plsql_ccflags = ''recompute_fault:'
                || case when l_attempt = 1 then 'true' else 'false' end || '''';
        exception
            when others then
                -- ORA-24344: compiled with errors, which is the point on attempt 1.
                if sqlcode <> -24344 then
                    raise;
                end if;
        end;
    end if;
end;
/
