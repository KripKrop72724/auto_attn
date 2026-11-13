-- RECOMP_BUSY holds the exact production procedure. Its
-- SLIC_ZKT_IDENTITY_REPAIR_API also gets a test-only HOLD_FOR_TEST, so a
-- second session (run_recompute_checks.py) can stay inside that package like
-- a long ADD identity-repair batch while the migration runs. The migration
-- must then stop with ORA-20970 before it changes anything.
create or replace package recomp_busy.slic_zkt_identity_repair_api authid definer as
    procedure recompute_probe(p_body in clob);
    procedure hold_for_test(p_seconds in number);
end slic_zkt_identity_repair_api;
/
create or replace package body recomp_busy.slic_zkt_identity_repair_api as
    procedure recompute_probe(p_body in clob) is
    begin
        slic_zkt_recompute_daily_flags(p_body);
    end recompute_probe;

    procedure hold_for_test(p_seconds in number) is
    begin
        dbms_session.sleep(p_seconds);
    end hold_for_test;
end slic_zkt_identity_repair_api;
/
