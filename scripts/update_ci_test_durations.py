#!/usr/bin/env python3
"""Refresh scripts/ci_test_durations.json from backend-test-plan-* JUnit reports.

Weights only balance whole-file shards; run_ci_tests.py proves complete,
disjoint coverage independently of them, so stale weights cost time, never scope.
With --check, compare one run's reports with the committed weights and print
GitHub warnings instead of writing; that mode is a diagnostic and always exits 0.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import xml.etree.ElementTree as ET

# A file is reported as stale only when it is both relatively and absolutely
# slower than its weight, so ordinary runner noise on small files stays quiet.
DRIFT_RATIO = 1.5
DRIFT_MIN_SECONDS = 30


def measure(reports: list[Path]) -> dict[str, float]:
    seconds: dict[str, float] = defaultdict(float)
    for report in reports:
        for case in ET.parse(report).iter("testcase"):
            module = case.get("classname", "").split("::")[0]
            # pytest renders tests/unit/test_x.py as classname "tests.unit.test_x[.Class]".
            parts = module.split(".")
            for end in range(len(parts), 0, -1):
                candidate = "/".join(parts[:end]) + ".py"
                if Path(candidate).is_file():
                    seconds[candidate] += float(case.get("time", 0))
                    break
            else:
                raise ValueError(f"Cannot map JUnit class {module!r} to a test file")
    return dict(seconds)


def stale_weight_warnings(measured: dict[str, float], weights: dict[str, int]) -> list[str]:
    warnings = []
    for file, seconds in sorted(measured.items()):
        if file not in weights:
            warnings.append(f"{file} has no committed duration weight, so its shard "
                            f"was balanced on an estimate ({seconds:.0f}s measured)")
        elif (seconds > DRIFT_RATIO * weights[file] / 1000
              and seconds - weights[file] / 1000 > DRIFT_MIN_SECONDS):
            warnings.append(f"{file} took {seconds:.0f}s against a committed "
                            f"weight of {weights[file] / 1000:.0f}s")
    return warnings


def check(reports: list[Path], weights_path: Path) -> int:
    try:
        weights = json.loads(weights_path.read_text())
        per_report = {report.name: measure([report]) for report in sorted(reports)}
        measured: dict[str, float] = defaultdict(float)
        for files in per_report.values():
            for file, seconds in files.items():
                measured[file] += seconds
        warnings = stale_weight_warnings(dict(measured), weights)
    except Exception as error:  # A diagnostic must never become a gate.
        print(f"::warning title=CI shard weights::Could not compare shard durations "
              f"with the committed weights ({type(error).__name__})")
        return 0
    for warning in warnings:
        print(f"::warning title=Stale CI shard weights::{warning}; refresh "
              f"scripts/ci_test_durations.json with scripts/update_ci_test_durations.py")
    print("Measured test seconds per shard report: " + " ".join(
        f"{name}={sum(files.values()):.0f}" for name, files in per_report.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="JUnit XML files from one CI run")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("ci_test_durations.json"))
    parser.add_argument("--check", type=Path, metavar="WEIGHTS",
                        help="warn about stale committed weights instead of writing new ones")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.reports, args.check)
    try:
        seconds = measure(args.reports)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    weights = {file: max(1, round(value * 1000)) for file, value in sorted(seconds.items())}
    args.output.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {len(weights)} file weights totalling {sum(weights.values()) / 1000:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
