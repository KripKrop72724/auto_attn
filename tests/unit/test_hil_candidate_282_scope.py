"""The HIL candidate workflow's scope check for ZKT 2.8.2, run as the workflow runs it.

2.8.2 keeps the exact nationwide prefix of every 2.7 platform image, or
reaches an exact ordered prefix of the three LHRCENTRAL ESPs, pinned by
hardware ID and terminal serial in deploy/add/hil-pins-2.8.2-lhrcentral.json.
Connector IDs here are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/firmware-hil-candidate.yml"
CHECKS = ("repository-contract", "windows-publication", "firmware", "backend", "frontend", "oracle-projection",
          "containers")
PINS = json.loads((ROOT / "deploy/add/hil-pins-2.8.2-lhrcentral.json").read_text())
NATIONWIDE = json.loads((ROOT / "deploy/add/hil-targets-zkt-270.json").read_text())
CONNECTORS = ("0c1e0001-0000-4000-8000-000000000001", "0c1e0002-0000-4000-8000-000000000002",
              "0c1e0003-0000-4000-8000-000000000003")
LHRCENTRAL = [{"connector_id": connector, "mac": pin["mac"], "terminal_serial": pin["terminal_serial"]}
              for connector, pin in zip(CONNECTORS, PINS)]


def provenance_script(checks: Path) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text.split("python3 - <<'PYCODE'\n", 1)[1].split("\n          PYCODE\n", 1)[0]
    return textwrap.dedent(body).replace("/tmp/hil-checks.json", str(checks))


def run(tmp_path, version, targets=None, mac=""):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({"check_runs": [
        {"id": 1, "name": name, "conclusion": "success", "app": {"slug": "github-actions"}} for name in CHECKS]}))
    output = tmp_path / "output"
    output.write_text("")
    environment = {**os.environ, "INPUT_VERSION": version, "INPUT_MAC": mac, "INPUT_FAMILY": "zkt",
                   "INPUT_TARGETS": json.dumps(targets) if targets is not None else "",
                   "GITHUB_OUTPUT": str(output)}
    result = subprocess.run([sys.executable, "-c", provenance_script(checks)], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=60)
    return result, output.read_text()


def test_pins_are_the_three_reviewed_lhrcentral_esps():
    assert PINS == [
        {"name": "LHRCENTRAL-01", "mac": "ac:27:6e:a3:44:68", "terminal_serial": "COVP232060159"},
        {"name": "LHRCENTRAL-02", "mac": "14:c1:9f:46:97:d0", "terminal_serial": "COVP232060174"},
        {"name": "LHRCENTRAL-03", "mac": "a4:cb:8f:d4:55:28", "terminal_serial": "COVP232060177"},
    ]
    # None is one of the nationwide 17.
    assert not {pin["mac"] for pin in PINS} & {row["mac"] for row in NATIONWIDE}


@pytest.mark.parametrize("targets", [LHRCENTRAL[:1], LHRCENTRAL[:2], LHRCENTRAL, NATIONWIDE[:1], NATIONWIDE])
def test_2_8_2_publishes_to_an_exact_lhrcentral_or_nationwide_prefix(tmp_path, targets):
    result, output = run(tmp_path, "2.8.2", targets)
    assert result.returncode == 0, result.stderr
    assert "targets=" + json.dumps(targets, separators=(",", ":")) + "\n" in output


@pytest.mark.parametrize("targets", [
    [LHRCENTRAL[0], LHRCENTRAL[2]],
    [LHRCENTRAL[0], LHRCENTRAL[2], LHRCENTRAL[1]],
    [*LHRCENTRAL, LHRCENTRAL[0]],
    LHRCENTRAL[1:],
    [{**LHRCENTRAL[0], "terminal_serial": PINS[1]["terminal_serial"]}],
    [LHRCENTRAL[0], {**LHRCENTRAL[1], "mac": "00:11:22:33:44:55"}],
    [{**LHRCENTRAL[0], "connector_id": CONNECTORS[0].upper()}],
    [{**LHRCENTRAL[0], "connector_id": "LHRCENTRAL-01"}],
    [LHRCENTRAL[0], {**LHRCENTRAL[1], "connector_id": CONNECTORS[0]}],
    [{**LHRCENTRAL[0], "zone_id": "LF-ZONE-LHRCENTRAL-01"}],
    [NATIONWIDE[0], LHRCENTRAL[0]],
    [],
])
def test_2_8_2_refuses_any_other_scope(tmp_path, targets):
    result, output = run(tmp_path, "2.8.2", targets)
    assert result.returncode != 0 and "targets=" not in output


def test_2_8_2_refuses_a_legacy_mac(tmp_path):
    result, _output = run(tmp_path, "2.8.2", mac=PINS[0]["mac"])
    assert result.returncode != 0


@pytest.mark.parametrize("version", ["2.7.8", "2.8.0", "2.8.1"])
def test_no_other_platform_image_reaches_lhrcentral(tmp_path, version):
    result, _output = run(tmp_path, version, LHRCENTRAL)
    assert result.returncode != 0 and "exact ordered nationwide prefix" in result.stderr
    result, _output = run(tmp_path, version, NATIONWIDE[:2])
    assert result.returncode == 0, result.stderr


def test_2_8_2_builds_as_the_direct_role():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"$EXPECTED_VERSION" == "2.8.1" || "$EXPECTED_VERSION" == "2.8.2" ) ]]; then direct_mode=ON; fi' in text
