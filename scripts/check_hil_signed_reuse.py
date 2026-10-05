"""Validate GitHub provenance before reusing an existing signed HIL package.

This permits retrying publication, never rebuilding or resigning a version.
The publication job independently verifies signed manifest identity, signature,
image bytes, deployed backend support and exact quarantine scope.
"""
import argparse
import json
from pathlib import Path
import re


def validate(run_id, version, repository, run, jobs, artifacts):
    if (not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or run.get("id") != int(run_id)
            or run.get("name") != "Zone Lite quarantined HIL candidate"
            or run.get("path") != ".github/workflows/firmware-hil-candidate.yml"
            or run.get("event") != "workflow_dispatch"
            or run.get("status") != "completed"
            or run.get("conclusion") not in {"success", "failure"}
            or run.get("head_repository", {}).get("full_name") != repository
            or not re.fullmatch(r"[a-f0-9]{40}", run.get("head_sha", ""))):
        raise ValueError("SIGNED_CANDIDATE_RUN_UNVERIFIED")
    rows = jobs.get("jobs", [])
    if jobs.get("total_count") != len(rows):
        raise ValueError("SIGNED_CANDIDATE_JOBS_INCOMPLETE")
    for name in ("provenance", "build", "sign"):
        matching = [row for row in rows if row.get("name") == name]
        if (len(matching) != 1 or matching[0].get("conclusion") != "success"
                or matching[0].get("status") != "completed"
                or matching[0].get("run_id") != int(run_id)):
            raise ValueError("SIGNED_CANDIDATE_STAGE_UNVERIFIED")
    rows = artifacts.get("artifacts", [])
    matching = [row for row in rows if row.get("name") == f"zone-lite-{version}-hil-only"]
    if (artifacts.get("total_count") != len(rows) or len(matching) != 1
            or matching[0].get("expired") is not False
            or type(matching[0].get("size_in_bytes")) is not int
            or not 0 < matching[0]["size_in_bytes"] <= 16 * 1024 * 1024):
        raise ValueError("SIGNED_CANDIDATE_ARTIFACT_UNAVAILABLE")
    return "reuse_run=" + run_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run_id", "version", "repository", "run", "jobs", "artifacts"):
        parser.add_argument(name)
    args = parser.parse_args()
    print(validate(args.run_id, args.version, args.repository,
                   *(json.loads(Path(path).read_text()) for path in (args.run, args.jobs, args.artifacts))))


if __name__ == "__main__":
    main()
