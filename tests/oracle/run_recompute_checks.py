"""Prove the per-employee-day SLIC_ZKT_RECOMPUTE_DAILY_FLAGS in an isolated Oracle test container.

Never accepts a DSN, production connection, credential or source-data export.
The container must carry our test label and sit alone on a labelled internal-only network.
Schemas RECOMP_OLD (exact production procedure, no extra index), RECOMP_NEW (the real
migration file applied), RECOMP_GUARD, RECOMP_ODD, RECOMP_BLOCKED (a test DDL trigger refuses
the replacement), RECOMP_BUSY (a second session holds a long call in a caller), RECOMP_RESTORE
(a test DDL trigger breaks a caller just before the replacement) and RECOMP_LAB are recreated
and filled only with deterministic synthetic rows.
"""
from pathlib import Path
import argparse
import json
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "deploy/add/oracle"
TESTS = ROOT / "tests/oracle"
MIGRATION = ORACLE / "20261113_recompute_daily_flags_by_day.sql"
ROLLBACK = ORACLE / "20261113_recompute_daily_flags_by_day_rollback.sql"
# The RECOMP_BUSY session stays in a caller this long; the migration's guard
# triggers above 60 s and recompute_busy_wait.sql starts it after 62 s.
HOLD_SECONDS = 100
INSTALLER = ORACLE / "slic_zkt_truth_api.sql"
PRODUCTION = TESTS / "recompute_production_procedure.sql"
# DBMS_OUTPUT is enabled per container, so switch to the PDB before enabling it.
HEADER = (
    "set echo off\nset feedback off\nset linesize 32767\nset trimout on\n"
    "whenever sqlerror exit failure rollback\nalter session set container = FREEPDB1;\n"
    "set serveroutput on size unlimited format wrapped\n"
)
FINAL_MARKERS = (
    "ORACLE_RECOMPUTE_BLOCKED_DDL_PASSED",
    "ORACLE_RECOMPUTE_BUSY_GUARD_PASSED",
    "ORACLE_RECOMPUTE_RESTORE_PASSED",
    "ORACLE_RECOMPUTE_MIGRATION_PASSED",
    "ORACLE_RECOMPUTE_EQUIVALENCE_PASSED",
    "ORACLE_RECOMPUTE_TIMING_PASSED",
    "ORACLE_RECOMPUTE_PLAN_PASSED",
    "ORACLE_RECOMPUTE_ROLLBACK_PASSED",
    "ORACLE_RECOMPUTE_REAPPLY_PASSED",
)


RESTORE_DIAGNOSTICS = """
declare
    l_next number;
begin
    select last_number into l_next from dba_sequences
     where sequence_owner = 'RECOMP_RESTORE' and sequence_name = 'RECOMPUTE_DDL_ATTEMPTS';
    dbms_output.put_line('restore_diag attempts=' || (l_next - 1));
    for o in (select object_name, object_type, status from dba_objects
               where owner = 'RECOMP_RESTORE' order by 1, 2) loop
        dbms_output.put_line('restore_diag ' || o.object_name || ' ' || o.object_type || '='
            || o.status);
    end loop;
    for c in (select column_name from dba_tab_columns
               where owner = 'RECOMP_RESTORE' and table_name = 'RECOMPUTE_RESTORE_PROBE') loop
        dbms_output.put_line('restore_diag probe column ' || c.column_name);
    end loop;
    for e in (select name, type, line, text from dba_errors
               where owner = 'RECOMP_RESTORE' order by name, type, sequence) loop
        dbms_output.put_line('restore_diag error ' || e.name || ' ' || e.type || ' line '
            || e.line || ' ' || e.text);
    end loop;
end;
/
exit success commit
"""


def require_isolated(container):
    inspected = subprocess.run(["docker", "inspect", container], check=True,
                               capture_output=True, text=True)
    info = json.loads(inspected.stdout)[0]
    networks = info["NetworkSettings"]["Networks"]
    if len(networks) != 1 or info["HostConfig"].get("PortBindings"):
        raise SystemExit("Refusing a test database with shared networking or published ports")
    network = json.loads(subprocess.check_output(
        ["docker", "network", "inspect", next(iter(networks))]))[0]
    if (info["Config"].get("Labels", {}).get("com.zkt.purpose") != "synthetic-qualification"
            or not network["Internal"]
            or network.get("Labels", {}).get("com.zkt.purpose") != "synthetic-qualification"
            or info["State"].get("Health", {}).get("Status") != "healthy"):
        raise SystemExit(
            "Refusing an unlabelled, externally networked or unhealthy Oracle test container")


def normalized(source):
    source = re.sub(r"--[^\n]*", "", source)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"\s", "", source).lower()


def literal(text, name):
    found = re.findall(name + r" constant varchar2\(32767\) := q'~(.*?)~';", text, flags=re.S)
    if len(found) != 1:
        raise SystemExit(f"Expected exactly one {name} literal")
    return found[0]


def between(text, start, end):
    first = text.index(start)
    return text[first:text.index(end, first) + len(end)]


def check_sources():
    """The embedded copies must be the exact production text and the exact new text."""
    production = PRODUCTION.read_text()
    production = production[production.index("create or replace procedure"):]
    production = production[:production.rindex("\n/")]
    migration = MIGRATION.read_text()
    rollback = ROLLBACK.read_text()
    installer = INSTALLER.read_text()
    if literal(migration, "c_production_ddl") != production:
        raise SystemExit("Migration c_production_ddl is not the exact production procedure")
    if literal(rollback, "c_production_ddl") != production:
        raise SystemExit("Rollback c_production_ddl is not the exact production procedure")
    per_day = literal(migration, "c_per_day_ddl")
    embedded = between(installer, "create or replace procedure slic_zkt_recompute_daily_flags(",
                       "end slic_zkt_recompute_daily_flags;")
    if embedded != per_day:
        raise SystemExit("slic_zkt_truth_api.sql must embed exactly the migrated procedure")
    if normalized(per_day) == normalized(production):
        raise SystemExit("The migrated procedure must differ from production")
    body = (TESTS / "recompute_truth_api_body.sql").read_text()
    installer_body = installer[installer.index("create or replace package body"):]
    for name in ("parse_event_timestamp", "attendance_timestamp_is_plausible",
                 "attendance_date_for"):
        ours = between(body, f"function {name}(", f"end {name};")
        theirs = between(installer_body, f"function {name}(", f"end {name};")
        if normalized(ours) != normalized(theirs):
            raise SystemExit(f"Test helper {name} differs from the installer")
    for script in (migration, rollback):
        outside = re.sub(r"q'~.*?~'", "''", script, flags=re.S).lower()
        for dml in ("insert into hr_raw", "update hr_raw", "delete from hr_raw", "merge into hr_raw"):
            if dml in outside:
                raise SystemExit(f"Migration scripts must not run attendance DML ({dml})")
        # The owner pastes these into APEX SQL Commands, which prompts for
        # :name bind variables (and substitutes &name), even inside literals.
        apex_syntax = re.findall(r":[A-Za-z_]\w*|&\w+", script)
        if apex_syntax:
            raise SystemExit(f"Migration scripts must not contain APEX bind or substitution "
                             f"syntax: {apex_syntax}")
    return production


def sqlplus(container, script, title):
    """Run one sqlplus session, streaming its output, and return (exit code, output)."""
    print(f"===== {title} =====", flush=True)
    started = time.monotonic()
    process = subprocess.Popen(
        ["docker", "exec", "-i", container, "sqlplus", "-s", "/ as sysdba"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def feed():
        process.stdin.write(script)
        process.stdin.close()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    lines = []
    for line in process.stdout:
        lines.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    writer.join()
    code = process.wait()
    print(f"===== {title}: exit {code} after {time.monotonic() - started:.0f} s =====",
          flush=True)
    return code, "".join(lines)


def start_background(container, script):
    """Start one sqlplus session that runs alongside the next ones; finish() collects it."""
    process = subprocess.Popen(
        ["docker", "exec", "-i", container, "sqlplus", "-s", "/ as sysdba"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    process.stdin.write(script)
    process.stdin.close()
    # communicate() must not flush the closed pipe (ValueError before Python 3.10).
    process.stdin = None
    return process


def finish(process, title, timeout):
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
    print(f"===== {title} =====\n{output}===== {title}: exit {process.returncode} =====",
          flush=True)
    return process.returncode, output


def in_schema(schema, script):
    return f"alter session set current_schema = {schema};\n{script}\nalter session set current_schema = sys;\n"


def block(statement):
    return f"begin\n    {statement}\nend;\n/\n"


def build_scripts(args):
    """Return the setup, refusal, blocked and main sqlplus scripts."""
    template = (TESTS / "recompute_schema.sql").read_text()
    truth_body = (TESTS / "recompute_truth_api_body.sql").read_text()
    production_file = PRODUCTION.read_text()
    unexpected = production_file.replace("order by cnic, attendance_date",
                                         "order by attendance_date, cnic")
    if unexpected.count("order by attendance_date, cnic") != 1:
        raise SystemExit("Could not build the unexpected-procedure variant")

    def schema(name, procedure):
        return (template.replace("__SCHEMA__", name)
                .replace("-- __PRODUCTION_PROCEDURE__", procedure)
                .replace("-- __TRUTH_API_BODY__", truth_body))

    setup = HEADER
    setup += schema("recomp_old", production_file)
    setup += schema("recomp_new", production_file)
    setup += schema("recomp_guard", production_file)
    setup += ("create index recomp_guard.hr_raw_attn_day_idx on "
              "recomp_guard.hr_raw_attn_capture_events (cnic, attendance_date, event_timestamp);\n")
    setup += schema("recomp_odd", unexpected)
    setup += schema("recomp_blocked", production_file)
    setup += (TESTS / "recompute_blocked_setup.sql").read_text()
    setup += schema("recomp_busy", production_file)
    setup += (TESTS / "recompute_busy_setup.sql").read_text()
    setup += schema("recomp_restore", production_file)
    setup += (TESTS / "recompute_restore_setup.sql").read_text()
    setup += (TESTS / "recompute_lab.sql").read_text()
    # SQL*Plus drops DBMS_OUTPUT of a failing block, so list errors first, then fail.
    setup += """
begin
    for broken in (
        select owner, object_name, object_type, status from dba_objects
         where owner like 'RECOMP\\_%' escape '\\' and status <> 'VALID'
    ) loop
        dbms_output.put_line('INVALID ' || broken.owner || '.' || broken.object_name
            || ' ' || broken.object_type);
    end loop;
    for err in (select owner, name, type object_type, line, text from dba_errors
                 where owner like 'RECOMP\\_%' escape '\\' order by owner, name, sequence) loop
        dbms_output.put_line(err.owner || '.' || err.name || ' ' || err.object_type || ' line '
            || err.line || ': ' || err.text);
    end loop;
end;
/
declare
    l_bad number;
begin
    select count(*) into l_bad from dba_objects
     where owner like 'RECOMP\\_%' escape '\\' and status <> 'VALID';
    if l_bad > 0 then
        raise_application_error(-20140, 'Recompute test schemas did not compile');
    end if;
end;
/
"""
    setup += block("recomp_lab.recompute_lab.generate_seed; recomp_lab.recompute_lab.load_seed;")
    setup += "exit success commit\n"

    migration = MIGRATION.read_text()
    rollback = ROLLBACK.read_text()
    refusal = HEADER + in_schema("recomp_odd", migration) + "exit success commit\n"
    blocked = HEADER + in_schema("recomp_blocked", migration) + "exit success commit\n"
    holder = HEADER + block(
        f"recomp_busy.slic_zkt_identity_repair_api.hold_for_test({HOLD_SECONDS});\n"
        "    dbms_output.put_line('recompute_busy_holder=released');") + "exit success commit\n"
    busy = (HEADER + (TESTS / "recompute_busy_wait.sql").read_text()
            + in_schema("recomp_busy", migration) + "exit success commit\n")
    restore = HEADER + in_schema("recomp_restore", migration) + "exit success commit\n"

    main_sql = HEADER
    main_sql += (TESTS / "recompute_verify_blocked.sql").read_text()
    main_sql += (TESTS / "recompute_verify_guards.sql").read_text()
    main_sql += in_schema("recomp_new", migration)
    main_sql += in_schema("recomp_new", migration)
    main_sql += in_schema("recomp_guard", migration)
    main_sql += (TESTS / "recompute_verify_migrated.sql").read_text()
    for first in range(1, args.calls + 1, args.batch):
        last = min(first + args.batch - 1, args.calls)
        main_sql += block(f"recomp_lab.recompute_lab.run_equivalence({first}, {last});")
    main_sql += block("recomp_lab.recompute_lab.run_fixed_cases;")
    main_sql += block(f"recomp_lab.recompute_lab.report_equivalence({args.calls});")
    main_sql += "drop trigger recomp_old.hr_raw_attn_count_writes;\n"
    main_sql += "drop trigger recomp_new.hr_raw_attn_count_writes;\n"
    main_sql += block(f"recomp_lab.recompute_lab.grow_tables({args.grow_rows});")
    main_sql += block(
        "dbms_stats.gather_table_stats('RECOMP_OLD', 'HR_RAW_ATTN_CAPTURE_EVENTS', "
        "cascade => true, no_invalidate => false);\n"
        "    dbms_stats.gather_table_stats('RECOMP_NEW', 'HR_RAW_ATTN_CAPTURE_EVENTS', "
        "cascade => true, no_invalidate => false);")
    main_sql += "alter system flush shared_pool;\n"
    main_sql += block(
        f"recomp_lab.recompute_lab.run_timing({args.old_timing_calls}, "
        f"{args.new_timing_calls}, {args.min_speedup});")
    main_sql += (TESTS / "recompute_plan_report.sql").read_text()
    main_sql += in_schema("recomp_new", rollback)
    main_sql += in_schema("recomp_guard", rollback)
    main_sql += (TESTS / "recompute_verify_rollback.sql").read_text()
    main_sql += in_schema("recomp_new", migration)
    main_sql += """
declare
    l_count number;
begin
    select count(*) into l_count from dba_indexes
     where owner = 'RECOMP_NEW' and index_name = 'HR_RAW_ATTN_CNIC_DATE_I' and status = 'VALID';
    if l_count <> 1 then
        raise_application_error(-20150, 'Re-applied migration did not recreate the index');
    end if;
    select count(*) into l_count from dba_source
     where owner = 'RECOMP_NEW' and name = 'SLIC_ZKT_RECOMPUTE_DAILY_FLAGS'
       and instr(text, 'l_cnics(i)') > 0;
    if l_count = 0 then
        raise_application_error(-20151, 'Re-applied migration did not replace the procedure');
    end if;
    select count(*) into l_count from dba_objects
     where owner = 'RECOMP_NEW' and status <> 'VALID';
    if l_count <> 0 then
        raise_application_error(-20152, 'Invalid objects after re-applying the migration');
    end if;
    dbms_output.put_line('ORACLE_RECOMPUTE_REAPPLY_PASSED');
end;
/
exit success commit
"""
    return setup, refusal, blocked, holder, busy, restore, main_sql


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container")
    parser.add_argument("--write-sql", metavar="DIR",
                        help="only write the four sqlplus scripts to DIR for review")
    parser.add_argument("--calls", type=int, default=600)
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--grow-rows", type=int, default=780000)
    parser.add_argument("--old-timing-calls", type=int, default=3)
    parser.add_argument("--new-timing-calls", type=int, default=60)
    parser.add_argument("--min-speedup", type=float, default=50)
    args = parser.parse_args()

    check_sources()
    print("Source checks passed: migration and rollback embed the exact production procedure; "
          "the installer embeds exactly the migrated procedure.", flush=True)
    setup, refusal, blocked, holder, busy, restore, main_sql = build_scripts(args)
    if args.write_sql:
        folder = Path(args.write_sql)
        folder.mkdir(parents=True, exist_ok=True)
        for name, script in (("setup", setup), ("refusal", refusal), ("blocked", blocked),
                             ("holder", holder), ("busy", busy), ("restore", restore),
                             ("main", main_sql)):
            (folder / f"recompute_{name}.sql").write_text(script)
        return
    if not args.container:
        raise SystemExit("--container is required")
    require_isolated(args.container)

    code, _ = sqlplus(args.container, setup, "setup: OLD/NEW/GUARD/ODD/BLOCKED schemas and seed data")
    if code:
        raise SystemExit("Recompute test setup failed")
    code, refused = sqlplus(args.container, refusal,
                            "migration must refuse an unexpected installed procedure")
    if code == 0 or "ORA-20974" not in refused:
        raise SystemExit("Migration did not refuse an unexpected procedure with ORA-20974")
    code, stopped = sqlplus(args.container, blocked,
                            "migration must not retry when the replacement itself fails")
    if (code == 0 or "ORA-20978" not in stopped or "ORA-20990" not in stopped
            or "ORA-20975" in stopped):
        raise SystemExit("Migration did not stop with ORA-20978 (cause ORA-20990, no ORA-20975)"
                         " when the procedure could not be replaced")
    holding = start_background(args.container, holder)
    code, refused_busy = sqlplus(args.container, busy,
                                 "migration must refuse while a long call runs in a caller")
    held_code, held = finish(holding, "long call held in RECOMP_BUSY", HOLD_SECONDS + 120)
    if held_code or "recompute_busy_holder=released" not in held:
        raise SystemExit("The session holding a long call in RECOMP_BUSY did not finish cleanly")
    if (code == 0 or "recompute_busy_holder=seen" not in refused_busy
            or "ORA-20970" not in refused_busy
            or "recompute_index=" in refused_busy):
        raise SystemExit("Migration did not refuse with ORA-20970 before step 2 while a long"
                         " call was running")
    code, restored = sqlplus(args.container, restore,
                             "migration must restore the previous procedure when validation"
                             " fails after the replacement")
    if (code == 0 or "ORA-20979" not in restored or "ORA-20975" in restored
            or "Compile errors: slic_zkt_identity_repair_api package body line" not in restored
            or "recompute_daily_flags=previous version restored after" not in restored):
        sqlplus(args.container, HEADER + RESTORE_DIAGNOSTICS, "RECOMP_RESTORE diagnostics")
        raise SystemExit("Migration did not restore with ORA-20979 (with the compile errors,"
                         " no ORA-20975) when validation failed after the replacement")
    code, output = sqlplus(args.container, main_sql,
                           "migration, equivalence, timing, rollback and re-apply")

    expected_counts = {
        "recompute_installed_procedure=production": 3,
        "recompute_installed_procedure=per_employee_day": 1,
        "recompute_daily_flags=replaced with the per-employee-day version": 3,
        "recompute_daily_flags=already the per-employee-day version; not replaced": 1,
        "recompute_index=hr_raw_attn_cnic_date_i created online": 2,
        "recompute_index=hr_raw_attn_day_idx already covers (cnic, attendance_date)": 1,
        "recompute_daily_flags=restored the 13 Nov 2026 production version": 2,
        "recompute_index=hr_raw_attn_cnic_date_i dropped": 1,
        "recompute_index=hr_raw_attn_cnic_date_i not present; nothing dropped": 1,
        "recompute_long_calls=none over 60 s": 4,
    }
    problems = [f"{text!r}: expected {count}, saw {output.count(text)}"
                for text, count in expected_counts.items() if output.count(text) != count]
    missing = [marker for marker in FINAL_MARKERS if marker not in output]
    if code or problems or missing:
        for problem in problems:
            print("Unexpected migration output count:", problem)
        raise SystemExit(f"Oracle recompute checks failed (exit {code}; missing {missing})")
    print("Oracle recompute checks passed: " + ", ".join(FINAL_MARKERS), flush=True)


if __name__ == "__main__":
    main()
