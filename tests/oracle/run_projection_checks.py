"""Compile/run synthetic Oracle checks in an explicitly isolated Docker database.

Never accepts a DSN, production connection, credential or source-data export.
The container must have our test label and a labelled internal-only network. The disposable schema
is recreated; every row comes from the checked-in synthetic fixture.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    inspected = subprocess.run(["docker", "inspect", args.container], check=True, capture_output=True, text=True)
    info = json.loads(inspected.stdout)[0]
    networks = info["NetworkSettings"]["Networks"]
    if len(networks) != 1 or info["HostConfig"].get("PortBindings"):
        raise SystemExit("Refusing a test database with shared networking or published ports")
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", next(iter(networks))]))[0]
    if (info["Config"].get("Labels", {}).get("com.zkt.purpose") != "synthetic-qualification"
            or not network["Internal"]
            or network.get("Labels", {}).get("com.zkt.purpose") != "synthetic-qualification"
            or info["State"].get("Health", {}).get("Status") != "healthy"):
        raise SystemExit("Refusing an unlabelled, externally networked or unhealthy Oracle test container")
    # Docker exec never receives a password. OS authentication remains inside
    # this network-isolated, labelled local test container.
    setup = (ROOT / "tests/oracle/projection_schema.sql").read_text()
    contract = (ROOT / "deploy/add/oracle/zkt_delivery_projection_v2.sql").read_text()
    # These credentials exist only inside this disposable schema. The
    # checked-in deployment source retains unconfigured placeholders.
    contract = contract.replace("REPLACE_WITH_ADD_API_USERNAME", "synthetic-add")
    contract = contract.replace("REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX",
                                hashlib.sha256(b"synthetic-test-password").hexdigest())
    checks = (ROOT / "tests/oracle/projection_checks.sql").read_text()
    sql = "set echo off\nset feedback off\nwhenever sqlerror exit failure rollback\n" + setup + contract + checks + "\nexit success rollback\n"
    result = subprocess.run(["docker", "exec", "-i", args.container, "sqlplus", "-s", "/ as sysdba"],
                            input=sql, text=True, capture_output=True)
    print(result.stdout)
    if result.returncode or not all(marker in result.stdout for marker in
            ("ORACLE_PROJECTION_CHECKS_PASSED", "ORACLE_AUTH_CHECKS_PASSED")):
        raise SystemExit("Oracle projection checks did not complete successfully")


if __name__ == "__main__":
    main()
