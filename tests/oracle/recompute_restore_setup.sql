-- RECOMP_RESTORE holds the exact production procedure. Its
-- SLIC_ZKT_IDENTITY_REPAIR_API body also reads a test-only table column. A
-- test-only DDL trigger lets the migration's CREATE OR REPLACE succeed, but
-- just before it renames that column (a table operation, which system
-- triggers may run), so the body no longer compiles. Validation after the
-- replacement then fails, and the migration must restore the exact previous
-- procedure and say so with ORA-20979 and the compile errors. When the
-- restore re-creates the procedure, the trigger renames the column back, so
-- everything must end VALID with the production source.
create table recomp_restore.recompute_restore_probe (probe_value number);

create or replace package body recomp_restore.slic_zkt_identity_repair_api as
    procedure recompute_probe(p_body in clob) is
        l_value number;
    begin
        select max(probe_value) into l_value from recompute_restore_probe;
        slic_zkt_recompute_daily_flags(p_body);
    end recompute_probe;
end slic_zkt_identity_repair_api;
/

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
        if l_attempt = 1 then
            execute immediate 'alter table recomp_restore.recompute_restore_probe'
                || ' rename column probe_value to probe_value_gone';
        else
            execute immediate 'alter table recomp_restore.recompute_restore_probe'
                || ' rename column probe_value_gone to probe_value';
        end if;
        -- DDL in a system trigger does not commit by itself; without this the
        -- autonomous transaction is rolled back with ORA-06519 (run 43513234008).
        commit;
    end if;
end;
/
