from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "zone_lite"
RUNTIME = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
COMPONENT = (FIRMWARE / "main" / "CMakeLists.txt").read_text(encoding="utf-8")
WORKFLOW = (
    ROOT / ".github" / "workflows" / "firmware-quetta-diagnostic.yml"
).read_text(encoding="utf-8")
NORMAL_HIL = (
    ROOT / ".github" / "workflows" / "firmware-hil-candidate.yml"
).read_text(encoding="utf-8")
PROMOTER = (ROOT / "deploy" / "add" / "promote-firmware.ps1").read_text(
    encoding="utf-8"
)
COMM_KEYS = (ROOT / "apps" / "add_backend" / "zk_add" / "comm_keys.py").read_text(
    encoding="utf-8"
)


def test_diagnostic_is_compile_time_off_for_nationwide_firmware() -> None:
    assert "project(zone_lite VERSION 2.5.2)" in (FIRMWARE / "CMakeLists.txt").read_text()
    assert "#define ZONE_LITE_QUETTA_DIAGNOSTIC 0" in RUNTIME
    assert "if(ZONE_LITE_QUETTA_DIAGNOSTIC)" in COMPONENT
    assert "ZONE_LITE_QUETTA_DIAGNOSTIC=1" not in NORMAL_HIL
    assert "ZONE_LITE_QUETTA_DIAGNOSTIC=1" in WORKFLOW


def test_diagnostic_build_is_exact_mac_hil_only_and_non_promotable() -> None:
    assert "DIAGNOSTIC_VERSION: 2.5.3" in WORKFLOW
    assert "Type QUETTA READ ONLY" in WORKFLOW
    assert "Exact Quetta Zone Lite ESP MAC" in WORKFLOW
    assert "-PublicationMode HIL_ONLY" in WORKFLOW
    assert "-HilTargetMac '${{ needs.provenance.outputs.mac }}'" in WORKFLOW
    assert "profile = 'QUETTA_ZKT_PROTOCOL_READ_ONLY'" in WORKFLOW
    assert "configuration_commit_allowed = $false" in WORKFLOW
    assert ".diagnostic-only.json" in WORKFLOW
    assert ".diagnostic-only.json" in PROMOTER
    assert "is a non-promotable diagnostic release" in PROMOTER


def test_diagnostic_reports_each_protocol_stage_without_secret_or_address() -> None:
    for stage in (
        "TCP_4370_UNREACHABLE",
        "CONNECT_NO_RESPONSE",
        "CONNECT_REJECTED",
        "AUTH_NO_RESPONSE",
        "AUTH_REJECTED",
        "SERIAL_READ_FAILED",
        "SERIAL_EMPTY",
        "SERIAL_MISMATCH",
        "AUTH_NOT_REQUIRED",
        "VERIFIED",
    ):
        assert stage in RUNTIME
    formatter = RUNTIME[
        RUNTIME.index("static void format_comm_probe_result") :
        RUNTIME.index("static bool process_pending_comm_key_command")
    ]
    assert '"configuration_committed\\\":false' in formatter
    assert '"local_scan_prefix\\\":24' in formatter
    assert "comm_key" not in formatter.lower()
    assert "ip_address" not in formatter
    assert "verified_serial" not in formatter


def test_diagnostic_path_never_commits_and_finishes_the_one_shot_command() -> None:
    manager = RUNTIME[RUNTIME.index("static bool process_pending_comm_key_command") :]
    diagnostic = manager[
        manager.index("#if ZONE_LITE_QUETTA_DIAGNOSTIC") :
        manager.index("#endif", manager.index("#if ZONE_LITE_QUETTA_DIAGNOSTIC")) + 6
    ]
    assert 'command.command_id, "FAILED"' in manager
    assert "diagnostic_result" in manager
    assert "add_connector_command_complete(command.command_id)" in manager
    assert "COMM_KEY_DIAGNOSTIC_VERIFIED_NO_COMMIT" in RUNTIME
    assert "zone_config_save_zkt_comm_key" not in diagnostic


def test_add_sanitizes_and_preserves_only_secret_free_probe_evidence() -> None:
    for field in (
        '"diagnostic_only"',
        '"configuration_committed"',
        '"probe_stage"',
        '"hosts_attempted"',
        '"tcp_reachable"',
        '"auth_response_code"',
        '"serial_match"',
    ):
        assert field in COMM_KEYS
    assert '"comm_key"' not in COMM_KEYS[
        COMM_KEYS.index("def sanitize_comm_key_result") :
        COMM_KEYS.index("def reveal_comm_key")
    ]
