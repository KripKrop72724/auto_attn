#!/usr/bin/env python3
"""Fail CI if a retired application or an ADD branding invariant regresses."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "apps/add_backend/zk_add/web.py",
    "apps/add_frontend/src/App.tsx",
    "firmware/zone_lite/main/zone_lite.c",
    "deploy/add/oracle/20260728_unify_raw_capture_auth.sql",
    ".github/workflows/add-ci.yml",
    ".github/workflows/add-deploy.yml",
    "docker-compose.add.yml",
}
FORBIDDEN_PREFIXES = (
    "apps/head_office/",
    "apps/hr_enrollment/",
    "apps/zone_agent/",
    "installer/",
    "packages/",
    "tools/zkt_",
)
FORBIDDEN_EXACT = {
    ".github/workflows/ci.yml",
    ".github/workflows/cd.yml",
    "railway.json",
    "railpack.json",
    "docs.md",
}
ALLOWED_UI_COLORS = {
    "#0094da",
    # State Life corporate navy/blue scale and accessible semantic states.
    "#066da3", "#071a2b", "#079bdc", "#087cb7", "#0b2238", "#0c5279",
    "#0c704d", "#12202d", "#12324f", "#164c70", "#526475", "#536675",
    "#55c6f2", "#62cff7", "#65cef5", "#7894a6", "#8bcdb4", "#8ca0ad",
    "#8da0ad", "#8ea8ba", "#962e38", "#96cfba", "#9a6500", "#9babb5",
    "#9bcbdf", "#9fb3c2", "#b43b44", "#b8c5ce", "#bfd2df", "#c9d7e1",
    "#d79a23", "#d9e9f4", "#dce4e9", "#dce9f0", "#dff3fc", "#e0a2a8",
    "#e1a2a7", "#e5bd69", "#e7f6f0", "#eaf0f3", "#eef3f6", "#f1faff",
    "#f2f5f7", "#f3f6f8", "#f5f8fa", "#f7fbfd", "#f8fafb", "#f8fbfc",
    "#fbfcfd", "#ffd987", "#fff0f1", "#fff4d6",
    "#111111",
    "#171717",
    "#1c1c1c",
    "#292929",
    "#333333",
    "#3d3d3d",
    "#424242",
    "#444444",
    "#525252",
    "#616161",
    "#737373",
    "#777777",
    "#a3a3a3",
    "#d6d6d6",
    "#e5e5e5",
    "#e8e8e8",
    "#eeeeee",
    "#f5f5f5",
    "#fafafa",
    "#ffffff",
}


def tracked_files() -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
    )
    # Deleted-but-not-yet-staged files can appear during local development.
    return {name for name in output.splitlines() if (ROOT / name).is_file()}


def main() -> int:
    files = tracked_files()
    problems: list[str] = []

    missing = sorted(REQUIRED - files)
    if missing:
        problems.append(f"required files are missing: {', '.join(missing)}")

    retired = sorted(
        name
        for name in files
        if name in FORBIDDEN_EXACT or any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
    )
    if retired:
        problems.append("retired product files returned:\n  " + "\n  ".join(retired))

    app_products = {
        name.split("/", 2)[1]
        for name in files
        if name.startswith("apps/") and len(name.split("/", 2)) >= 3
    }
    unexpected_apps = sorted(app_products - {"add_backend", "add_frontend"})
    if unexpected_apps:
        problems.append(f"unexpected applications: {', '.join(unexpected_apps)}")

    firmware_products = {
        name.split("/", 2)[1]
        for name in files
        if name.startswith("firmware/") and len(name.split("/", 2)) >= 3
    }
    if firmware_products != {"zone_lite"}:
        problems.append(f"firmware products must be exactly zone_lite, got {sorted(firmware_products)}")

    workflow_files = {name for name in files if name.startswith(".github/workflows/")}
    expected_workflows = {
        ".github/workflows/add-ci.yml",
        ".github/workflows/add-deploy.yml",
        ".github/workflows/firmware-canary-promote.yml",
        ".github/workflows/firmware-hil-candidate.yml",
        ".github/workflows/firmware-hil.yml",
        ".github/workflows/firmware-key-bootstrap.yml",
        ".github/workflows/firmware-nationwide-rollout.yml",
        ".github/workflows/firmware-release.yml",
        ".github/workflows/zone-lite-device-provisioning.yml",
    }
    if workflow_files != expected_workflows:
        problems.append(f"workflow set is not consolidated: {sorted(workflow_files)}")

    ui_sources = [
        ROOT / "apps/add_frontend/src/styles.css",
        ROOT / "apps/add_frontend/index.html",
    ]
    found_colors: set[str] = set()
    for source in ui_sources:
        found_colors.update(
            item.lower() for item in re.findall(r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])", source.read_text())
        )
    unexpected_colors = sorted(found_colors - ALLOWED_UI_COLORS)
    if unexpected_colors:
        problems.append(f"UI contains colors outside State Life blue/white/neutrals: {unexpected_colors}")
    if "#0094da" not in found_colors or "#ffffff" not in found_colors:
        problems.append("UI must use State Life blue #0094DA and white")

    frontend = (ROOT / "apps/add_frontend/src/App.tsx").read_text().lower()
    backend = (ROOT / "apps/add_backend/zk_add/web.py").read_text().lower()
    if "register connector" in frontend or "/connectors/register" in backend:
        problems.append("manual connector registration must not exist")

    oracle_truth_api = (ROOT / "deploy/add/oracle/slic_zkt_truth_api.sql").read_text()
    if re.search(r"\bc_api_password\s+constant\b", oracle_truth_api, re.IGNORECASE):
        problems.append("Oracle truth API must never embed a plaintext password constant")
    if "c_api_password_sha256" not in oracle_truth_api:
        problems.append("Oracle truth API must verify the password through a SHA-256 digest")
    if "or v_api_version <> 2" not in oracle_truth_api:
        problems.append("Oracle authoritative truth must require reconcile API v2")
    for attestation in (
        "v_terminal_event_count <> v_received",
        "v_identity_mapped_count <> v_received",
        "v_identity_map_complete <> 'true'",
    ):
        if attestation not in oracle_truth_api:
            problems.append(
                f"Oracle authoritative truth is missing completeness guard: {attestation}"
            )

    logo = ROOT / "apps/add_frontend/public/state-life-logo.png"
    if not logo.is_file() or logo.stat().st_size < 1_000:
        problems.append("the State Life production logo asset is missing or invalid")

    if problems:
        print("Repository contract check failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("Repository contract valid: ADD + Zone Lite only; branding invariants satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
