#!/usr/bin/env python3
"""Refresh scripts/ci_test_durations.json from backend-test-plan-* JUnit reports.

Weights only balance whole-file shards; run_ci_tests.py proves complete,
disjoint coverage independently of them, so stale weights cost time, never scope.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="JUnit XML files from one CI run")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("ci_test_durations.json"))
    args = parser.parse_args(argv)
    seconds: dict[str, float] = defaultdict(float)
    for report in args.reports:
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
                raise SystemExit(f"Cannot map JUnit class {module!r} to a test file")
    weights = {file: max(1, round(value * 1000)) for file, value in sorted(seconds.items())}
    args.output.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {len(weights)} file weights totalling {sum(weights.values()) / 1000:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
