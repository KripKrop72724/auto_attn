import pytest

from zk_add.schemas import HeartbeatPayload, OnboardRequest
from zk_add.terminal_families import (
    HIKVISION_RELEASE_GATES, HIKVISION_POLL_RELEASE_GATES, firmware_family, release_family,
    require_family_match, require_production_qualification,
)


def test_legacy_protocol_is_explicitly_zkt():
    assert firmware_family() == release_family({}) == "zkt"
    assert HeartbeatPayload().firmware_family == "zkt"
    assert OnboardRequest(hardware_id="00:11:22:33:44:55", zone_id="z", zone_name="z",
                          device_id="d", firmware_version="2.5.2").firmware_family == "zkt"


@pytest.mark.parametrize("value", ["", "HIKVISION", "unknown", {}, 1])
def test_unknown_families_do_not_become_zkt(value):
    with pytest.raises(ValueError):
        firmware_family(value)


def test_cross_family_and_legacy_release_rejected():
    hik = {"firmware_family": "hikvision", "project_name": "zone_lite_hikvision"}
    require_family_match("hikvision", hik)
    require_family_match("zkt", {})
    for family, manifest in (("zkt", hik), ("hikvision", {}),
                             ("hikvision", {"firmware_family": "hikvision"})):
        with pytest.raises(ValueError):
            require_family_match(family, manifest)


def qualified_manifest():
    return {
        "firmware_family": "hikvision", "project_name": "zone_lite_hikvision",
        "hardware_qualification": {
            "model": "DS-K1T342EFWX", "terminal_firmware": "test-fixture-only",
            "profile_id": "synthetic", "evidence_sha256": "a" * 64,
            "soak_seconds": 259200,
            "gates": {gate: "PASS" for gate in HIKVISION_RELEASE_GATES},
        },
    }


@pytest.mark.parametrize("gate", sorted(HIKVISION_RELEASE_GATES))
def test_each_missing_hardware_gate_blocks_production(gate):
    manifest = qualified_manifest()
    del manifest["hardware_qualification"]["gates"][gate]
    with pytest.raises(ValueError, match="QUALIFICATION_INCOMPLETE"):
        require_production_qualification(manifest)


@pytest.mark.parametrize("seconds", [259199, True, "259200", None])
def test_soak_cannot_be_skipped_or_coerced(seconds):
    manifest = qualified_manifest()
    manifest["hardware_qualification"]["soak_seconds"] = seconds
    with pytest.raises(ValueError, match="QUALIFICATION_INCOMPLETE"):
        require_production_qualification(manifest)


def test_qualification_required_for_hikvision_only():
    require_production_qualification({})
    require_production_qualification(qualified_manifest())
    with pytest.raises(ValueError, match="QUALIFICATION_REQUIRED"):
        require_production_qualification({
            "firmware_family": "hikvision", "project_name": "zone_lite_hikvision",
        })


def test_explicit_five_second_poll_mode_replaces_only_stream_gates():
    manifest = qualified_manifest()
    proof = manifest["hardware_qualification"]
    proof.update(capture_mode="poll", poll_interval_seconds=5,
                 gates={gate: "PASS" for gate in HIKVISION_POLL_RELEASE_GATES})
    require_production_qualification(manifest)
    for field, value in (("capture_mode", "unknown"), ("poll_interval_seconds", 15),
                         ("poll_interval_seconds", True), ("soak_seconds", 0)):
        original = proof[field]
        proof[field] = value
        with pytest.raises(ValueError, match="QUALIFICATION_INCOMPLETE"):
            require_production_qualification(manifest)
        proof[field] = original
    del proof["gates"]["poll_during_history_and_crud"]
    with pytest.raises(ValueError, match="QUALIFICATION_INCOMPLETE"):
        require_production_qualification(manifest)
