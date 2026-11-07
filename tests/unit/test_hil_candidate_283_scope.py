"""The HIL candidate workflow's scope check for ZKT 2.8.3, run as the workflow runs it.

2.8.3 keeps the exact nationwide prefix of every 2.7 platform image, or
reaches an exact ordered prefix of the FSDMAIN ESPs (at most three), pinned by
hardware ID and terminal serial in deploy/add/hil-pins-2.8.3-fsdmain.json.
Connector IDs here are synthetic."""
import json
from pathlib import Path

import pytest

from test_hil_candidate_282_scope import CONNECTORS, NATIONWIDE, run

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/firmware-hil-candidate.yml"
PINS = json.loads((ROOT / "deploy/add/hil-pins-2.8.3-fsdmain.json").read_text())
LHRCENTRAL_PINS = json.loads((ROOT / "deploy/add/hil-pins-2.8.2-lhrcentral.json").read_text())
FSDMAIN = [{"connector_id": connector, "mac": pin["mac"], "terminal_serial": pin["terminal_serial"]}
           for connector, pin in zip(CONNECTORS, PINS)]
LHRCENTRAL = [{"connector_id": connector, "mac": pin["mac"], "terminal_serial": pin["terminal_serial"]}
              for connector, pin in zip(CONNECTORS, LHRCENTRAL_PINS)]


def test_pins_are_the_reviewed_fsdmain_esps():
    assert PINS == [
        {"name": "FSDMAIN-01", "mac": "30:30:f9:17:87:2c", "terminal_serial": "CKPG231060360"},
        {"name": "FSDMAIN-02", "mac": "8c:bf:ea:17:95:d0", "terminal_serial": "AF4C204260255"},
        {"name": "FSDMAIN-03", "mac": "30:30:f9:1f:90:10", "terminal_serial": "AF4C225060506"},
    ]
    # None is one of the nationwide 17 or the LHRCENTRAL ESPs.
    assert not {pin["mac"] for pin in PINS} & {row["mac"] for row in NATIONWIDE + LHRCENTRAL}


@pytest.mark.parametrize("targets", [FSDMAIN[:1], FSDMAIN, NATIONWIDE[:1], NATIONWIDE])
def test_2_8_3_publishes_to_an_exact_fsdmain_or_nationwide_prefix(tmp_path, targets):
    result, output = run(tmp_path, "2.8.3", targets)
    assert result.returncode == 0, result.stderr
    assert "targets=" + json.dumps(targets, separators=(",", ":")) + "\n" in output


@pytest.mark.parametrize("targets", [
    [FSDMAIN[1], FSDMAIN[0]],
    [*FSDMAIN, FSDMAIN[0]],
    [*FSDMAIN, {"connector_id": CONNECTORS[2], "mac": "00:11:22:33:44:55", "terminal_serial": "OTHER"}],
    FSDMAIN[1:],
    [{**FSDMAIN[0], "terminal_serial": PINS[1]["terminal_serial"]}],
    [FSDMAIN[0], {**FSDMAIN[1], "mac": "00:11:22:33:44:55"}],
    [{**FSDMAIN[0], "connector_id": CONNECTORS[0].upper()}],
    [{**FSDMAIN[0], "connector_id": "FSDMAIN-01"}],
    [FSDMAIN[0], {**FSDMAIN[1], "connector_id": CONNECTORS[0]}],
    [{**FSDMAIN[0], "zone_id": "LF-ZONE-FSDMAIN-01"}],
    [NATIONWIDE[0], FSDMAIN[0]],
    LHRCENTRAL[:1],
    [],
])
def test_2_8_3_refuses_any_other_scope(tmp_path, targets):
    result, output = run(tmp_path, "2.8.3", targets)
    assert result.returncode != 0 and "targets=" not in output


def test_2_8_3_refuses_a_legacy_mac(tmp_path):
    result, _output = run(tmp_path, "2.8.3", mac=PINS[0]["mac"])
    assert result.returncode != 0


@pytest.mark.parametrize("version", ["2.7.8", "2.8.0", "2.8.1", "2.8.2"])
def test_no_other_platform_image_reaches_fsdmain(tmp_path, version):
    result, _output = run(tmp_path, version, FSDMAIN)
    assert result.returncode != 0 and "exact ordered nationwide prefix" in result.stderr


def test_2_8_3_builds_as_the_direct_role():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"$EXPECTED_VERSION" == "2.8.2" || "$EXPECTED_VERSION" == "2.8.3" ) ]]; then direct_mode=ON; fi' in text
