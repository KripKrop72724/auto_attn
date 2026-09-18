#!/usr/bin/env python3
"""Compile pinned production components and retain an auditable host-soak result."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
SOURCES = [
    ROOT / "tests/firmware/soak/queue_soak.c",
    *[ROOT / "firmware/zone_lite/main" / name for name in (
        "durable_queue.c", "durable_queue.h", "storage_budget.c", "storage_budget.h",
        "delivery_scheduler.c", "delivery_scheduler.h",
    )],
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument("--backlog", type=int, default=100000)
    args = parser.parse_args()
    if not 2 <= args.seconds <= 86400 or not 1 <= args.backlog <= 100000:
        parser.error("Duration must be 2..86400 seconds; backlog must be 1..100000")
    paths = [str(path.relative_to(ROOT)) for path in SOURCES] + [str(Path(__file__).resolve().relative_to(ROOT))]
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", *paths], cwd=ROOT, text=True)
    if dirty:
        raise SystemExit("Commit the tested soak sources before recording qualification evidence")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = output / "queue-files"
    data.mkdir()
    executable = output / "queue-soak"
    compiler = shutil.which("cc")
    if not compiler:
        raise SystemExit("A C compiler is required")
    command = [compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-O1", "-g", "-Wall",
               "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
               "-I", str(ROOT / "firmware/zone_lite/main"),
               *[str(path) for path in SOURCES if path.suffix == ".c"], "-o", str(executable)]
    subprocess.run(command, cwd=ROOT, check=True)
    manifest = {
        "kind": "HOST_QUEUE_SCHEDULER_CAPACITY_SOAK",
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES},
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "compiler": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0],
        "requested_seconds": args.seconds, "initial_source_records": args.backlog,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "limitations": ["Simulated NVS and destination ports", "Simulated worker restarts",
                        "Not full-device integration", "Physical power cuts not performed",
                        "Hardware endurance not performed", "Does not authorize publication"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    started = time.monotonic()
    run = [str(executable), str(args.seconds), str(args.backlog)]
    # Keep an explicitly requested long test active across idle system sleep.
    caffeinate = shutil.which("caffeinate")
    if caffeinate:
        run = [caffeinate, "-i", *run]
    with (output / "soak.log").open("w") as log:
        try:
            result = subprocess.run(run, cwd=data, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=args.seconds + 180, check=False)
            return_code = result.returncode
        except subprocess.TimeoutExpired:
            return_code = 124
            log.write("SOAK_TIMEOUT\n")
    text = (output / "soak.log").read_text()
    passed = return_code == 0 and "PASS seconds=" in text
    receipt = {"outcome": "PASS" if passed else "FAILED", "return_code": return_code,
               "elapsed_seconds": time.monotonic() - started,
               "ended_at": datetime.now(timezone.utc).isoformat(),
               "log_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "thirty_minute_10_eps": "PASS" if "30_MINUTE_10_EPS_CAPTURE_PASSED" in text else "NOT_COMPLETED"}
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
