"""Derive exact HIL prefixes and require unchanged green main. Never changes ADD."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "KripKrop72724/auto_attn"
CHECKS = (
    "repository-contract",
    "windows-publication",
    "firmware",
    "backend",
    "frontend",
    "oracle-projection",
    "containers",
)


def require(value, code):
    if not value:
        raise ValueError(code)


def plan(version, source, application, artifact, old_count, new_count, mode, confirmation):
    require(version in {"2.6.23", "2.6.22", "2.7.0"}, "VERSION_REFUSED")
    require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source), "SOURCE_REFUSED")
    for value in (application, artifact):
        require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "DIGEST_REFUSED")
    require(
        mode in {"preview", "apply"}
        and confirmation == f"{mode.upper()}-{version}-HIL-{old_count}-TO-{new_count}",
        "CONFIRMATION_REFUSED",
    )
    inventory = json.loads((ROOT / "deploy/add/hil-targets-zkt-270.json").read_text())
    require(isinstance(inventory, list) and len(inventory) == 17, "INVENTORY_REFUSED")
    if version == "2.6.22":
        sys.path.insert(0, str(ROOT / "apps/add_backend"))
        from zk_add.zkt_factory_contract import factory_trial_targets

        targets = factory_trial_targets()
        require(
            len(targets) == 3 and all(target in inventory for target in targets),
            "FACTORY_INVENTORY_REFUSED",
        )
    else:
        targets = inventory
    require(
        type(old_count) is int
        and type(new_count) is int
        and 1 <= old_count < new_count <= len(targets),
        "PREFIX_COUNTS_REFUSED",
    )
    return {
        "schema_version": 1,
        "version": version,
        "source_sha": source,
        "application_sha256": application,
        "artifact_sha256": artifact,
        "old_count": old_count,
        "new_count": new_count,
        "mode": mode,
        "signed_nationwide_denominator": 17,
        "old_targets_json": json.dumps(targets[:old_count], separators=(",", ":")),
        "new_targets_json": json.dumps(targets[:new_count], separators=(",", ":")),
        "eligibility": "NOT_GRANTED; EXISTING_ADD_SCHEDULER_REQUIRED",
    }


def require_green_checks(value, sha):
    require(
        isinstance(value, dict)
        and type(value.get("total_count")) is int
        and isinstance(value.get("check_runs"), list)
        and 0 < value["total_count"] <= 100
        and len(value["check_runs"]) == value["total_count"],
        "CI_LIST_INCOMPLETE",
    )
    rows = value["check_runs"]
    require(
        all(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0 for row in rows)
        and len({row["id"] for row in rows}) == len(rows),
        "CI_LIST_INVALID",
    )
    chosen = {}
    for name in CHECKS:
        matching = [
            row
            for row in rows
            if row.get("name") == name
            and isinstance(row.get("app"), dict)
            and row["app"].get("slug") == "github-actions"
        ]
        require(matching, "MAIN_CI_NOT_GREEN")
        latest = max(matching, key=lambda row: row["id"])
        require(
            latest.get("head_sha") == sha
            and latest.get("status") == "completed"
            and latest.get("conclusion") == "success",
            "MAIN_CI_NOT_GREEN",
        )
        chosen[name] = latest["id"]
    return chosen


def verify_main(expected, source, get):
    require(
        isinstance(expected, str)
        and re.fullmatch(r"[0-9a-f]{40}", expected)
        and isinstance(source, str)
        and re.fullmatch(r"[0-9a-f]{40}", source),
        "MAIN_SHA_REFUSED",
    )
    path = f"repos/{REPOSITORY}/git/ref/heads/main"

    def current():
        value = get(path)
        require(
            isinstance(value, dict)
            and value.get("ref") == "refs/heads/main"
            and isinstance(value.get("object"), dict)
            and value["object"].get("type") == "commit"
            and value["object"].get("sha") == expected,
            "MAIN_CHANGED",
        )

    current()
    checks = require_green_checks(
        get(f"repos/{REPOSITORY}/commits/{expected}/check-runs?per_page=100"), expected
    )
    if source != expected:
        comparison = get(f"repos/{REPOSITORY}/compare/{source}...{expected}")
        require(
            isinstance(comparison, dict)
            and comparison.get("status") in {"ahead", "identical"}
            and comparison.get("base_commit", {}).get("sha") == source
            and comparison.get("merge_base_commit", {}).get("sha") == source,
            "SIGNED_SOURCE_NOT_MAIN_ANCESTOR",
        )
    current()
    return {"schema_version": 1, "main_sha": expected, "source_sha": source, "check_ids": checks}


def github_get(path):
    token = os.environ.get("GH_TOKEN")
    require(token and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "GITHUB_CONTEXT_REFUSED")
    request = Request(
        "https://api.github.com/" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ADD-HIL-scope-extension",
        },
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, "GITHUB_RESPONSE_TOO_LARGE")
    return json.loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-main")
    parser.add_argument("--version", choices=["2.6.23", "2.6.22", "2.7.0"])
    parser.add_argument("--source", required=True)
    parser.add_argument("--application")
    parser.add_argument("--artifact")
    parser.add_argument("--old-count", type=int)
    parser.add_argument("--new-count", type=int)
    parser.add_argument("--mode", choices=["preview", "apply"])
    parser.add_argument("--confirmation")
    args = parser.parse_args(argv)
    try:
        value = (
            verify_main(args.verify_main, args.source, github_get)
            if args.verify_main
            else plan(
                args.version,
                args.source,
                args.application,
                args.artifact,
                args.old_count,
                args.new_count,
                args.mode,
                args.confirmation,
            )
        )
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("HIL_SCOPE_EXTENSION_REFUSED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
