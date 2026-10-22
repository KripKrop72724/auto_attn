"""ZKT 2.7.7: the confirm-first storage exception's firmware contracts (read
from the sources; storage_exception_host.c and the SPIFFS harness execute the
decision). docs/zone-lite-2.7.7-confirm-first.md, section 9.3."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
OTA = (MAIN / "ota_manager.c").read_text()
REPAIR = (MAIN / "storage_repair.c").read_text()
GUARD = (MAIN / "zkt_ota_guard.c").read_text()
CONNECTOR = (MAIN / "add_connector.c").read_text()
APP = (MAIN / "zone_lite.c").read_text()


def code(source: str) -> str:
    """The source without its comments."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))


def block(source: str, start: str, end: str) -> str:
    first = source.index(start)
    return source[first:source.index(end, first)]


EXCEPTION = block(OTA, "/* ---- ZKT 2.7.7: the confirm-first storage exception", "static bool confirm_or_report_rollback(void)")
CONFIRM = block(OTA, "static bool confirm_or_report_rollback(void)", "#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE)")


def test_the_exception_marks_valid_only_after_record_evidence_receipt_and_recheck() -> None:
    step = block(EXCEPTION, "static sx_step_t sx_confirm_step(", "/* Before the ordinary path marks the image valid")
    order = [step.index(marker) for marker in (
        "sx_window_step(&s_sx.window, verdict",
        "SX_BUDGET_MARGIN_SECONDS",
        "sx_store_record(SX_STATE_CONFIRMING)",
        'add_connector_log_acknowledged("WARN", "storage", "STORAGE_REPAIR_EXCEPTION_EVIDENCE"',
        'report_progress("BOOTED_PENDING", "STORAGE_REPAIR_EXCEPTION"',
        "ota_progress_receipt_exception(receipt",
        "!sx_window_same(&s_sx.window, &store)",
        "progress->active_seconds + spent >= BOOT_CONFIRM_ACTIVE_SECONDS",
        "esp_ota_mark_app_valid_cancel_rollback()",
        "sx_store_record(SX_STATE_CONFIRMED)",
        'strlcpy(s_journal.state, "RECONCILING"',
    )]
    assert order == sorted(order)
    assert "sx_store_record(SX_STATE_ABANDONED)" in step  # a failed mark-valid rolls back as 2.7.6
    # Only three paths ever mark an image valid: the writer's local proof, the
    # ordinary acceptance and this one.
    assert len(re.findall(r"esp_ota_mark_app_valid_cancel_rollback\(\) [!=]= ESP_OK", OTA)) == 3
    accept = block(CONFIRM, "if (verdict == BOOT_CONFIRM_ACCEPT) {", "ESP_LOGE(TAG, \"New image not confirmed")
    assert accept.index("sx_ordinary_confirmation()") < accept.index("esp_ota_mark_app_valid_cancel_rollback()")


def test_a_valid_image_is_never_invalidated() -> None:
    # Critique 13, M4: READY_TO_BOOT on a valid image reconciles before the
    # loop, and the loop's rollback is guarded as well.
    valid_rule = CONFIRM.index("if (running_state_is(ESP_OTA_IMG_VALID)) {")
    loop = CONFIRM.index("for (;;) {")
    assert valid_rule < loop
    calls = [match.start() for match in re.finditer(r"esp_ota_mark_app_invalid_rollback_and_reboot\(\)", CONFIRM)]
    assert len(calls) == 1
    guard = CONFIRM.rindex("\n", 0, calls[0])
    assert "if (!running_state_is(ESP_OTA_IMG_VALID)) (void)esp_ota_mark_app_invalid_rollback_and_reboot();" in \
        CONFIRM[guard:calls[0] + 60]
    # A CONFIRMING record is abandoned before any rollback (critique 14).
    timeout = CONFIRM[CONFIRM.index("New image not confirmed"):]
    assert timeout.index("sx_rollback_note(") < timeout.index("esp_ota_mark_app_invalid_rollback_and_reboot")
    assert "sx_store_record(SX_STATE_ABANDONED)" in block(EXCEPTION, "static void sx_rollback_note(", "\n}\n")


def test_gate_reasons_never_reach_the_ota_last_error() -> None:
    # M2: ADD turns an active deployment's ota.last_error into FAILED.
    assert "s_last_error" not in code(EXCEPTION)
    for name in ("sx_confirm_step", "sx_after_confirm_step", "sx_reconciling_report", "sx_report_ended", "sx_restart"):
        assert f"{name}(" in EXCEPTION


def test_the_restart_is_counted_before_the_safepoint_and_never_reports_progress() -> None:
    # M3: no READY_TO_BOOT report from RECONCILING (409), the counter first,
    # and once claimed the ESP restarts whatever happens.
    restart = block(EXCEPTION, "static void sx_restart(sx_record_t *record)", "\n}\n")
    assert restart.index("storage_repair_exception_store(record)") < restart.index("add_connector_claim_ota_restart()")
    assert "report_state" not in restart and "report_progress" not in restart and "wait_for_capture_safepoint" not in restart
    assert restart.rstrip().endswith("esp_restart();")
    ended = block(EXCEPTION, "static bool sx_report_ended(sx_record_t *record)", "\n}\n")
    assert ended.index('report_progress("FAILED", code') < ended.index("clear_journal()") < \
        ended.index("record->reported = 1")


def test_ota_and_its_guard_never_write_the_store() -> None:
    for name, source in (("ota_manager.c", OTA), ("zkt_ota_guard.c", GUARD)):
        for pattern in (r'fopen\([^)]*"[wa]', r"\bunlink\(", r"\bremove\(", r"\brename\(", r"\bSPIFFS_", r"qs_append",
                        r"ftruncate\(", r"\btruncate\("):
            assert not re.search(pattern, source), (name, pattern)
    # The only store read is the legacy-evidence listing; with the VFS
    # unmounted the boot repair's survey answers, after the floor and before
    # the queue store's lock (critique 1, M5).
    evidence = block(GUARD, "static const char *legacy_evidence(void)", "\n}\n")
    assert evidence.index("esp_spiffs_mounted(NULL)") < evidence.index("qs_local_read_begin()")
    before = block(GUARD, "const char *zj_ota_before_download(", "if (strcmp(version, ZJ_WRITER_VERSION))")
    assert before.index('"STORAGE_REDO_FLOOR"') < before.index("return legacy_evidence();")


def test_the_websocket_and_ipc_stacks() -> None:
    config = block(CONNECTOR, "esp_websocket_client_config_t config = {", "};")
    assert "#ifndef ZONE_LITE_HIKVISION" in config and ".task_stack = ADD_WEBSOCKET_TASK_STACK," in config
    assert "#define ADD_WEBSOCKET_TASK_STACK 6144" in (MAIN / "add_connector.h").read_text()
    zkt = (ROOT / "firmware/zone_lite/sdkconfig.zkt.defaults").read_text().splitlines()
    assert "CONFIG_ESP_IPC_TASK_STACK_SIZE=1792" in zkt
    hikvision = (ROOT / "firmware/zone_lite/sdkconfig.hikvision.defaults").read_text()
    assert "CONFIG_ESP_IPC_TASK_STACK_SIZE" not in hikvision


def test_the_record_key_is_outside_every_2_7_5_and_2_7_6_layout() -> None:
    header = (MAIN / "storage_exception.h").read_text()
    assert '#define SX_RECORD_KEY "exception"' in header
    keys = set(re.findall(r'SRR_NVS_NAMESPACE, "([a-z_]+)"', REPAIR))
    core = (MAIN / "storage_repair_core.c").read_text()
    keys |= set(re.findall(r'"(state|manifest|gaps|resets|reclaim|damage)"', core + REPAIR))
    assert "exception" not in keys and {"reclaim", "gaps", "resets", "damage"} <= keys
    assert "SX_RECORD_KEY" in REPAIR


def test_the_boot_repair_glue() -> None:
    before = block(REPAIR, "static void exception_before(srr_config_t *config)", "\n}\n")
    assert "config->custody_needs_hold = s_exception_active;" in before  # critique 11
    after_run = block(REPAIR, "static void exception_after_run(void)", "\n}\n")
    # Decision 5 (c) on exception images (critique 2), then the area test only
    # on a boot that could use the exception, CLAIMED journaled first (3).
    assert after_run.index("s_report.reclaim.waiting && !s_mount_blocked") < after_run.index("sx_repair_verdict(probe)")
    assert after_run.index("SRP_RECLAIM_CLAIMED") < after_run.index("srx_area_test(&REDO_IO")
    assert "s_probe_hold = s_area_test == SRX_TEST_PASSED;" in after_run
    boot = block(REPAIR, "void storage_repair_boot(void)", "/* After the VFS mount")
    assert boot.index("exception_before(&config);") < boot.index("srr_run(&s_fs, &config")
    assert boot.index("exception_after_run();") < boot.rindex("esp_partition_deregister_external(s_redo)")
    assert "__wrap_spiffs_api_write" in REPAIR and "__wrap_spiffs_api_erase" in REPAIR
    cmake = (MAIN / "CMakeLists.txt").read_text()
    repair_block = block(cmake, "if(NOT ZONE_LITE_FAMILY STREQUAL \"hikvision\" AND NOT ZONE_LITE_STORAGE_RECOVERY",
                         "endif()")
    assert '"-Wl,--wrap=spiffs_api_write" "-Wl,--wrap=spiffs_api_erase"' in repair_block
    assert "qs_hold_probe(storage_repair_hold_probe());" in APP


def test_local_failures_are_classified_by_the_errno_their_site_captured() -> None:
    # H1: only a captured errno decides; no site claims room by a literal.
    for source in (APP, CONNECTOR):
        assert not re.search(r"led_status_fault_errno\([^;]*ENOSPC\)", source)
        assert not re.search(r"led_status_fault_read\([^;]*EIO,", source)
    # The writes a wedged store refuses at runtime carry their errno.
    enqueue = block(APP, "static enqueue_result_t enqueue_event_to_files(", "\n}\n")
    assert enqueue.count("led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, g_enqueue_storage_errno)") == 2
    assert "led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, g_enqueue_storage_errno)" in \
        block(APP, "static bool recover_live_event_after_storage_error(", "\n}\n")
    append = block(APP, "static bool append_line_policy(", "\n}\n")
    assert "errno = ok ? 0 : error;" in append
    # Reads of the reclaim queues name the queue and where they started.
    assert "led_status_fault_read(LED_STATUS_LOCAL_FAILURE, read_error, LED_QUEUE_PENDING, read_offset)" in APP
    assert "LED_QUEUE_BLOCKED" in APP and "(led_queue_t)(LED_QUEUE_CORRUPT_ORDS + lane)" in APP
    for site in ("compact_outbox_locked(outbox, true)) led_status_fault_errno",
                 "led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, settle_error)",
                 "led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, admitted ? captured_error : 0)"):
        assert site in CONNECTOR
    # Every other incident stays ordinary: the gate refuses on it.
    assert APP.count("led_status_fault(LED_STATUS_LOCAL_FAILURE)") >= 10
