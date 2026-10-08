"""Run isolated, file-preserving CI shards and verify their complete coverage."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCOPES = ("tests/unit", "tests/firmware", "tests/companion")


def validate_items(items: object) -> list[dict[str, str]]:
    if not isinstance(items, list) or not items:
        raise ValueError("The collected test scope must not be empty")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"file", "nodeid"}:
            raise ValueError("Invalid collected test entry")
        file, nodeid = item["file"], item["nodeid"]
        if (not isinstance(file, str) or not isinstance(nodeid, str)
                or Path(file).is_absolute() or ".." in Path(file).parts
                or not file.endswith(".py") or "\\" in file
                or not any(file.startswith(scope + "/") for scope in SCOPES)
                or not nodeid.startswith(file + "::") or nodeid in seen):
            raise ValueError("Invalid, duplicate, or out-of-scope collected test")
        seen.add(nodeid)
    return sorted(items, key=lambda item: item["nodeid"])


def partition(items: object, count: int) -> list[list[str]]:
    items = validate_items(items)
    counts = Counter(item["file"] for item in items)
    if type(count) is not int or not 1 <= count <= len(counts):
        raise ValueError("Every shard must contain at least one collected test file")
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0] * count
    for file in sorted(counts, key=lambda file: (-counts[file], file)):
        index = min(range(count), key=lambda index: (loads[index], index))
        shards[index].append(file)
        loads[index] += counts[file]
    return [sorted(files) for files in shards]


def make_plan(items: object, index: int, count: int) -> dict:
    items = validate_items(items)
    shards = partition(items, count)
    if type(index) is not int or not 0 <= index < count:
        raise ValueError("Invalid shard index")
    return {"schema_version": 1, "shard_index": index, "shard_count": count,
            "items": items, "files": shards[index]}


def validate_plan(plan: object) -> dict:
    if (not isinstance(plan, dict) or set(plan) != {
            "schema_version", "shard_index", "shard_count", "items", "files"}
            or type(plan["schema_version"]) is not int or plan["schema_version"] != 1):
        raise ValueError("Invalid shard plan")
    expected = make_plan(plan["items"], plan["shard_index"], plan["shard_count"])
    if plan != expected:
        raise ValueError("Shard plan differs from its deterministic allocation")
    return expected


def verify_plans(plans: list[dict], count: int) -> None:
    if type(count) is not int or count < 1 or len(plans) != count:
        raise ValueError("A successful plan from every shard is required")
    checked = [validate_plan(plan) for plan in plans]
    if ({plan["shard_index"] for plan in checked} != set(range(count))
            or any(plan["shard_count"] != count or plan["items"] != checked[0]["items"]
                   for plan in checked)):
        raise ValueError("Shard collections disagree or a shard is missing/duplicated")
    files = [file for plan in checked for file in plan["files"]]
    expected = {item["file"] for item in checked[0]["items"]}
    if len(files) != len(set(files)) or set(files) != expected:
        raise ValueError("Test files were dropped or duplicated")


class Collection:
    def __init__(self, root: Path, expected: list[dict] | None = None):
        self.root, self.expected = root, expected
        self.items: list[dict[str, str]] = []
        self.deselected = False

    def pytest_deselected(self, items):
        self.deselected = self.deselected or bool(items)

    def pytest_collection_finish(self, session):
        import pytest

        try:
            self.items = validate_items([
                {"file": Path(item.path).resolve().relative_to(self.root).as_posix(),
                 "nodeid": item.nodeid} for item in session.items])
            if self.deselected or (self.expected is not None and self.items != self.expected):
                raise ValueError("Executed test collection differs from the assigned scope")
        except ValueError as error:
            raise pytest.UsageError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--collect-manifest", type=Path, help=argparse.SUPPRESS)
    mode.add_argument("--execute-plan", type=Path, help=argparse.SUPPRESS)
    mode.add_argument("--verify-plans", type=Path)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=2)
    parser.add_argument("--plan-path", type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("PYTEST_ADDOPTS", "").strip():
        parser.error("PYTEST_ADDOPTS must be empty; CI cannot silently change test scope")
    try:
        if args.verify_plans:
            plans = [json.loads(path.read_text()) for path in sorted(args.verify_plans.glob("test-plan-*.json"))]
            verify_plans(plans, args.shard_count)
            print(f"Verified {len(plans)} shards: complete, identical collection and disjoint file coverage")
            return 0
        root = Path.cwd().resolve()
        if any(not (root / scope).is_dir() for scope in SCOPES):
            raise ValueError("Run from the repository root with all three required test scopes")
        if args.collect_manifest or args.execute_plan:
            # Match `python -m pytest`: repository-local script imports are
            # available during collection as well as during test execution.
            sys.path.insert(0, str(root))
            import pytest

            plan = validate_plan(json.loads(args.execute_plan.read_text())) if args.execute_plan else None
            expected = [item for item in plan["items"] if item["file"] in plan["files"]] if plan else None
            collector = Collection(root, expected)
            options = ["--rootdir", str(root), "-q", "--durations=25"]
            options += plan["files"] if plan else ["--collect-only", *SCOPES]
            result = int(pytest.main(options, plugins=[collector]))
            if result == 0 and args.collect_manifest:
                args.collect_manifest.write_text(json.dumps(collector.items))
            return result
        if args.shard_index is None or args.plan_path is None:
            raise ValueError("--shard-index and --plan-path are required")
        script = str(Path(__file__).resolve())
        with tempfile.TemporaryDirectory(prefix="add-ci-collection-") as temporary:
            manifest = Path(temporary) / "collection.json"
            result = subprocess.run([sys.executable, script, "--collect-manifest", str(manifest)], check=False)
            if result.returncode:
                return result.returncode
            plan = make_plan(json.loads(manifest.read_text()), args.shard_index, args.shard_count)
        args.plan_path.parent.mkdir(parents=True, exist_ok=True)
        args.plan_path.write_text(json.dumps(plan, sort_keys=True))
        selected = sum(item["file"] in plan["files"] for item in plan["items"])
        print(f"Shard {args.shard_index + 1}/{args.shard_count}: {selected}/{len(plan['items'])} tests, "
              f"{len(plan['files'])} whole files", flush=True)
        return subprocess.run([sys.executable, script, "--execute-plan", str(args.plan_path.resolve())], check=False).returncode
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
