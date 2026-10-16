"""Coverage and failure propagation for the isolated CI test shards."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_ci_tests.py"
spec = importlib.util.spec_from_file_location("ci_test_partition", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def items(counts):
    return [{"file": file, "nodeid": f"{file}::test_case[{index}]"}
            for file, count in counts.items() for index in range(count)]


def test_balanced_partition_keeps_files_whole_and_covers_every_collected_item():
    collected = items({f"tests/unit/test_{name}.py": count
                       for name, count in (("a", 7), ("b", 4), ("c", 3), ("d", 2))})
    shards = runner.partition(collected, 2)
    assert shards == [["tests/unit/test_a.py", "tests/unit/test_d.py"],
                      ["tests/unit/test_b.py", "tests/unit/test_c.py"]]
    assert runner.partition(list(reversed(collected)), 2) == shards
    assert all(sum(item["file"] in shard for shard in shards) == 1 for item in collected)
    runner.verify_plans([runner.make_plan(collected, index, 2) for index in (0, 1)], 2)


@pytest.mark.parametrize("collected", [
    [], None, [{"file": "tests/unit/test_a.py", "nodeid": "unrelated::test_a"}],
    items({"outside/test_a.py": 1}), items({"tests/unit/../test_a.py": 1}),
    items({"tests/unit/test_a.py": 1}) * 2,
])
def test_empty_invalid_and_duplicate_collection_fails(collected):
    with pytest.raises(ValueError):
        runner.partition(collected, 2)


@pytest.mark.parametrize("count", [0, -1, True, 3])
def test_empty_or_invalid_shard_is_rejected(count):
    with pytest.raises(ValueError):
        runner.partition(items({"tests/unit/test_a.py": 1, "tests/unit/test_b.py": 1}), count)


def test_join_rejects_missing_duplicate_different_or_tampered_shard_plans():
    collected = items({"tests/unit/test_a.py": 2, "tests/firmware/test_b.py": 1})
    plans = [runner.make_plan(collected, index, 2) for index in (0, 1)]
    for invalid in ([plans[0]], [plans[0], plans[0]]):
        with pytest.raises(ValueError):
            runner.verify_plans(invalid, 2)
    different = runner.make_plan(collected + items({"tests/companion/test_c.py": 1}), 1, 2)
    with pytest.raises(ValueError, match="collections disagree"):
        runner.verify_plans([plans[0], different], 2)
    altered = deepcopy(plans)
    altered[0]["files"] += altered[1]["files"]
    with pytest.raises(ValueError, match="deterministic allocation"):
        runner.verify_plans(altered, 2)


def run(tmp_path, *args):
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


def synthetic_suite(tmp_path):
    for scope in runner.SCOPES:
        (tmp_path / scope).mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/scope_probe.py").write_text("COLLECTION_ROOT = True\n")
    paths = ["tests/unit/test_a.py", "tests/unit/test_b.py",
             "tests/firmware/test_c.py", "tests/companion/test_d.py"]
    for file in paths:
        (tmp_path / file).write_text(
            "from pathlib import Path\n"
            "from scripts.scope_probe import COLLECTION_ROOT\n"
            "def test_runs():\n"
            "    assert COLLECTION_ROOT\n"
            f"    with Path('executed').open('a') as output: output.write({file!r} + '\\n')\n")
    return paths


def test_actual_subprocess_shards_run_all_files_once_and_join(tmp_path):
    paths = synthetic_suite(tmp_path)
    for index in (0, 1):
        result = run(tmp_path, "--shard-index", str(index), "--plan-path", f"plans/test-plan-{index}.json")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "slowest" in result.stdout
    assert sorted((tmp_path / "executed").read_text().splitlines()) == sorted(paths)
    result = run(tmp_path, "--verify-plans", "plans")
    assert result.returncode == 0, result.stdout + result.stderr


def test_collection_error_is_fatal_and_does_not_write_a_plan_or_execute_tests(tmp_path):
    synthetic_suite(tmp_path)
    (tmp_path / "tests/unit/test_a.py").write_text("def broken(\n")
    result = run(tmp_path, "--shard-index", "0", "--plan-path", "plan.json")
    assert result.returncode != 0
    assert not (tmp_path / "plan.json").exists()
    assert not (tmp_path / "executed").exists()


def test_test_failure_propagates_even_with_a_valid_coverage_plan(tmp_path):
    synthetic_suite(tmp_path)
    (tmp_path / "tests/unit/test_a.py").write_text("def test_fails():\n    assert False\n")
    result = run(tmp_path, "--shard-index", "0", "--shard-count", "1", "--plan-path", "plan.json")
    assert result.returncode == 1
    runner.validate_plan(json.loads((tmp_path / "plan.json").read_text()))


def test_test_execution_refuses_collection_drift_before_running_any_test(tmp_path):
    paths = synthetic_suite(tmp_path)
    collected = [{"file": file, "nodeid": file + "::test_runs"} for file in paths]
    (tmp_path / "plan.json").write_text(json.dumps(runner.make_plan(collected, 0, 1)))
    (tmp_path / paths[0]).write_text("def test_replaced():\n    pass\n")
    result = run(tmp_path, "--execute-plan", "plan.json")
    assert result.returncode != 0
    assert "collection differs" in result.stderr
    assert not (tmp_path / "executed").exists()


def test_duration_weights_balance_whole_files_without_changing_coverage():
    collected = items({"tests/unit/test_slow.py": 2, "tests/unit/test_a.py": 9,
                       "tests/unit/test_b.py": 8, "tests/firmware/test_new.py": 1})
    weights = {"tests/unit/test_slow.py": 600_000, "tests/unit/test_a.py": 4_000,
               "tests/unit/test_b.py": 3_000, "tests/unit/test_removed.py": 9_000}
    shards = runner.partition(collected, 2, weights)
    # The measured slow file is isolated; unmeasured files fall back to a count estimate.
    assert shards == [["tests/unit/test_slow.py"],
                      ["tests/firmware/test_new.py", "tests/unit/test_a.py", "tests/unit/test_b.py"]]
    plans = [runner.make_plan(collected, index, 2, weights) for index in (0, 1)]
    runner.verify_plans(plans, 2, weights)
    assert sorted(file for plan in plans for file in plan["files"]) == sorted(
        {item["file"] for item in collected})


@pytest.mark.parametrize("weights", [{"tests/unit/test_a.py": 0, "tests/unit/test_b.py": 0},
                                     {"tests/unit/test_a.py": 10**9}])
def test_zero_or_extreme_weights_never_produce_an_empty_shard(weights):
    collected = items({"tests/unit/test_a.py": 1, "tests/unit/test_b.py": 1})
    assert all(runner.partition(collected, 2, weights))


@pytest.mark.parametrize("weights", [[], {"tests/unit/test_a.py": -1},
                                     {"tests/unit/test_a.py": 1.5}, {"tests/unit/test_a.py": True}])
def test_invalid_weights_are_rejected(weights):
    with pytest.raises(ValueError):
        runner.partition(items({"tests/unit/test_a.py": 1}), 1, weights)


def test_join_rejects_plans_balanced_with_different_or_uncommitted_weights():
    collected = items({"tests/unit/test_a.py": 2, "tests/unit/test_b.py": 1, "tests/unit/test_c.py": 1})
    committed = {"tests/unit/test_a.py": 10, "tests/unit/test_b.py": 20}
    other = {"tests/unit/test_a.py": 30, "tests/unit/test_b.py": 20}
    good = [runner.make_plan(collected, index, 2, committed) for index in (0, 1)]
    runner.verify_plans(good, 2, committed)
    with pytest.raises(ValueError, match="committed weights"):
        runner.verify_plans(good, 2, other)
    mixed = [good[0], runner.make_plan(collected, 1, 2, other)]
    with pytest.raises(ValueError):
        runner.verify_plans(mixed, 2)
    tampered = deepcopy(good)
    tampered[0]["weights"] = other
    with pytest.raises(ValueError):
        runner.verify_plans(tampered, 2)


def test_actual_subprocess_shards_with_committed_weights_join(tmp_path):
    paths = synthetic_suite(tmp_path)
    (tmp_path / "weights.json").write_text(json.dumps({paths[0]: 90_000, paths[1]: 1}))
    for index in range(3):
        result = run(tmp_path, "--shard-index", str(index), "--shard-count", "3",
                     "--weights", "weights.json", "--plan-path", f"plans/test-plan-{index}.json")
        assert result.returncode == 0, result.stdout + result.stderr
    assert sorted((tmp_path / "executed").read_text().splitlines()) == sorted(paths)
    result = run(tmp_path, "--verify-plans", "plans", "--shard-count", "3", "--weights", "weights.json")
    assert result.returncode == 0, result.stdout + result.stderr
    (tmp_path / "weights.json").write_text(json.dumps({paths[0]: 1}))
    result = run(tmp_path, "--verify-plans", "plans", "--shard-count", "3", "--weights", "weights.json")
    assert result.returncode != 0
