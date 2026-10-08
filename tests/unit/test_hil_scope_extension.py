"""Exact inventory, fresh main and workflow-bound extension; no production I/O."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "hil_scope_extension", ROOT / "scripts/hil_scope_extension.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
SOURCE, MAIN = "a" * 40, "b" * 40


def arguments(version="2.6.23", old=1, new=2, mode="preview"):
    return [
        version,
        SOURCE,
        "c" * 64,
        "d" * 64,
        old,
        new,
        mode,
        f"{mode.upper()}-{version}-HIL-{old}-TO-{new}",
    ]


@pytest.mark.parametrize("version,total", [("2.6.23", 17), ("2.6.22", 3), ("2.7.0", 17)])
def test_exact_tracked_prefixes_preserve_nationwide_denominator(version, total):
    full = json.loads((ROOT / "deploy/add/hil-targets-zkt-270.json").read_text())
    for old in range(1, total):
        result = helper.plan(*arguments(version, old, total))
        before, after = (
            json.loads(result[key]) for key in ("old_targets_json", "new_targets_json")
        )
        assert before == after[:old] and len(after) == total
        assert all(target in full for target in after)
        assert result["signed_nationwide_denominator"] == 17
        assert "NOT_GRANTED" in result["eligibility"]
        if version != "2.6.22":
            assert after == full
        else:
            from zk_add.zkt_factory_contract import factory_trial_targets

            assert after == factory_trial_targets() and full[0] not in after


@pytest.mark.parametrize(
    "index,bad",
    [
        (0, "2.6.21"),
        (1, "A" * 40),
        (1, "a" * 64),
        (2, "c" * 40),
        (3, "d" * 65),
        (4, 0),
        (4, True),
        (5, 18),
        (5, 1),
        (6, "force"),
        (7, "APPLY-2.6.23-HIL-1-TO-2"),
        (7, ""),
    ],
)
def test_invalid_identity_counts_or_mode_confirmation_refused(index, bad):
    args = arguments()
    args[index] = bad
    with pytest.raises(ValueError):
        helper.plan(*args)


def test_factory_cannot_extend_outside_three_and_apply_needs_exact_confirmation():
    with pytest.raises(ValueError):
        helper.plan(*arguments("2.6.22", 1, 4))
    assert helper.plan(*arguments(mode="apply"))["mode"] == "apply"


def checks():
    return {
        "total_count": len(helper.CHECKS),
        "check_runs": [
            {
                "id": index + 1,
                "name": name,
                "app": {"slug": "github-actions"},
                "head_sha": MAIN,
                "status": "completed",
                "conclusion": "success",
            }
            for index, name in enumerate(helper.CHECKS)
        ],
    }


def getter(changes=None):
    calls = []

    def get(path):
        calls.append(path)
        if path.endswith("git/ref/heads/main"):
            value = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN}}
        elif "check-runs" in path:
            value = checks()
        else:
            value = {
                "status": "ahead",
                "base_commit": {"sha": SOURCE},
                "merge_base_commit": {"sha": SOURCE},
            }
        return changes(path, deepcopy(value), calls) if changes else value

    return get, calls


def test_source_is_ancestor_and_current_main_checked_before_and_after():
    get, calls = getter()
    result = helper.verify_main(MAIN, SOURCE, get)
    assert result["check_ids"].keys() == set(helper.CHECKS)
    assert "oracle-projection" in result["check_ids"]
    assert len(calls) == 4 and calls[0] == calls[-1]


@pytest.mark.parametrize(
    "fault",
    [
        "old-main",
        "main-changed",
        "foreign-source",
        "unknown-check",
        "not-completed",
        "failed",
        "new-rerun",
        "wrong-app",
        "wrong-head",
        "truncated",
        "overflow",
        "duplicate",
        "missing-oracle",
    ],
)
def test_changed_main_failed_rerun_and_incomplete_evidence_refused(fault):
    def changes(path, value, calls):
        if path.endswith("git/ref/heads/main"):
            if fault == "old-main" or (fault == "main-changed" and len(calls) > 1):
                value["object"]["sha"] = "e" * 40
        elif "compare/" in path and fault == "foreign-source":
            value["merge_base_commit"]["sha"] = "f" * 40
        elif "check-runs" in path:
            row = value["check_runs"][0]
            if fault == "unknown-check":
                row["name"] = "invented"
            if fault == "not-completed":
                row["status"] = "in_progress"
            if fault == "failed":
                row["conclusion"] = "failure"
            if fault == "wrong-app":
                row["app"]["slug"] = "someone-else"
            if fault == "wrong-head":
                row["head_sha"] = SOURCE
            if fault == "new-rerun":
                value["check_runs"].append(
                    {**row, "id": 100, "status": "queued", "conclusion": None}
                )
                value["total_count"] += 1
            if fault == "truncated":
                value["total_count"] += 1
            if fault == "overflow":
                value["total_count"] = 101
            if fault == "duplicate":
                value["check_runs"][1]["id"] = row["id"]
            if fault == "missing-oracle":
                value["check_runs"] = [
                    r for r in value["check_runs"] if r["name"] != "oracle-projection"
                ]
                value["total_count"] -= 1
        return value

    get, _ = getter(changes)
    with pytest.raises(ValueError):
        helper.verify_main(MAIN, SOURCE, get)


def test_cli_redacts_arbitrary_exception(monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("sensitive configuration must not be printed")

    monkeypatch.setattr(helper, "github_get", fail)
    assert helper.main(["--verify-main", MAIN, "--source", SOURCE]) == 1
    assert capsys.readouterr() == ("", "HIL_SCOPE_EXTENSION_REFUSED\n")


def test_workflow_is_explicit_bounded_main_only_and_serialized_with_production():
    import yaml

    # BaseLoader keeps YAML's on key as a string and values intentionally textual.
    workflow = yaml.load(
        (ROOT / ".github/workflows/firmware-extend-journal-hil.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert len(workflow["on"]["workflow_dispatch"]["inputs"]) == 8
    assert workflow["concurrency"] == {"group": "add-production", "cancel-in-progress": "false"}
    job = workflow["jobs"]["extend"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "firmware-production"
    assert job["timeout-minutes"] == "10"
    checkout = job["steps"][0]
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": "false"}
    run = job["steps"][1]["run"]
    assert "${{ inputs." not in run and "-ExpectedMainSha $env:GITHUB_SHA" in run
    ci = (ROOT / ".github/workflows/add-ci.yml").read_text()
    assert ci.count("./tests/firmware/test_journal_hil_extension.ps1") == 2
    assert (
        "-RequirePublishedHilRelease"
        in (ROOT / "deploy/add/extend-journal-hil-scope.ps1").read_text()
    )
