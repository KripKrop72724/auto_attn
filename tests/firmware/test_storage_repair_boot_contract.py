"""ZKT 2.7.5: where the boot storage repair runs and what it may touch."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start:source.index("\n}\n", start)]


def test_repair_runs_first_in_storage_init_and_before_the_vfs() -> None:
    app = (MAIN / "zone_lite.c").read_text()
    init = _body(app, "static void storage_init(void)")
    order = ["storage_repair_boot();", "seen_cache_init();", "esp_vfs_spiffs_register(&conf);",
             "storage_repair_after_mount();", "storage_upgrade_init()", "g_queue_store_ready = qs_init();"]
    positions = [init.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert ".format_if_mount_failed = false," in init
    # The storage-ready gate opens only after the repair and the mount, on
    # every path out of the mount.
    assert init.count("add_connector_storage_ready();") == 2
    assert init.index("storage_repair_after_mount();") < init.rindex("add_connector_storage_ready();")
    # The OTA task (rollback deadline, confirmation budget) starts after storage.
    main = app[app.index("void app_main(void)"):]
    assert main.index("storage_init();") < main.index("ota_manager_start();")


def test_repair_is_built_only_into_zkt_production_roles() -> None:
    component = (MAIN / "CMakeLists.txt").read_text()
    block = component[component.index("# ZKT 2.7.5: the custody-first boot storage repair"):]
    block = block[:block.index("endif()")]
    assert ('if(NOT ZONE_LITE_FAMILY STREQUAL "hikvision" AND NOT ZONE_LITE_STORAGE_RECOVERY AND '
            "NOT ZONE_LITE_JOURNAL_WRITES)") in block
    for source in ("storage_repair_plan.c", "storage_repair_mirror.c", "storage_repair_core.c", "storage_repair.c"):
        assert f'"{source}"' in block
    assert "ZONE_LITE_STORAGE_REPAIR=1" in block
    assert 'idf_component_get_property(zone_lite_spiffs_dir spiffs COMPONENT_DIR)' in block
    app = (MAIN / "zone_lite.c").read_text()
    for call in ("storage_repair_boot();", "storage_repair_after_mount();"):
        guard = app[:app.index(call)].rsplit("#if", 1)[1]
        assert guard.startswith(" defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR")


def test_no_reformat_and_the_check_callback_never_calls_spiffs() -> None:
    for name in ("storage_repair_core.c", "storage_repair.c", "storage_repair_plan.c", "storage_repair_mirror.c"):
        assert "SPIFFS_format" not in (MAIN / name).read_text()
    core = (MAIN / "storage_repair_core.c").read_text()
    callback = _body(core, "void srr_check_callback(")
    assert "SPIFFS_" not in callback.replace("SPIFFS_CHECK_", "")
    assert "r->ports->yield(r->ports->context);" in callback
    # SPIFFS_check returns only its last step's result: failures come from the callback.
    assert "s32_t res = SPIFFS_check(r->fs);" in core and "r->errors == errors" in core


def test_glue_has_a_deadline_no_task_and_psram_holds() -> None:
    glue = (MAIN / "storage_repair.c").read_text()
    boot = _body(glue, "void storage_repair_boot(void)")
    assert boot.index("esp_timer_start_once(s_deadline_timer") < boot.index("SPIFFS_mount(")
    assert "esp_restart();" in _body(glue, "static void deadline_expired(void *argument)")
    assert "xTaskCreate" not in glue
    assert re.search(r"\bmalloc\(", glue.replace("heap_caps_malloc(", "")) is None
    assert "MALLOC_CAP_SPIRAM" in _body(glue, "static void *port_alloc(void *context, size_t bytes)")
    # Flash transfers to or from PSRAM go through the one internal bounce buffer.
    assert "heap_caps_malloc(SR_BOUNCE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)" in boot
    assert "uxTaskGetStackHighWaterMark(NULL)" in boot


def test_command_inbox_waits_for_storage_and_reports_go_through_the_outbox_task() -> None:
    connector = (MAIN / "add_connector.c").read_text()
    restore = _body(connector, "static void restore_command_inbox(void)")
    assert "!atomic_load(&s_storage_ready)" in restore
    contains = _body(connector, "static int command_journal_contains_locked(const char *command_id)")
    assert "if (!atomic_load(&s_storage_ready)) return -1;" in contains
    merge = _body(connector, "bool add_connector_repair_command_inbox(void)")
    assert merge.index("xSemaphoreTake(s_command_lock") < merge.index("cim_merge(&config, &result)") \
        < merge.index("xSemaphoreGive(s_command_lock)")
    assert ".merge = ADD_COMMAND_INBOX_MERGE_PATH," in merge
    # Every kept line is on flash in the merge file (only ever appended to)
    # before the backup or the stage goes; the merge file goes last.
    module = (MAIN / "command_inbox_merge.c").read_text()
    body = _body(module, "bool cim_merge(const cim_config_t *c, cim_result_t *result)")
    order = ["extend_merge_file(c, used, count)", "ft_checkpoint_idle(c->port)", "remove(c->stage)",
             "remove(c->backup)", "ft_replace(", "remove(c->merge)"]
    positions = [body.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert 'fopen(c->merge, "a")' in module and 'fopen(c->merge, "w")' not in module
    # A NUL ends a line, as '\n' does.
    assert "if (ch == '\\n' || ch == 0) {" in module
    # A merge an earlier boot left unfinished runs again even unflagged.
    after = _body((MAIN / "storage_repair.c").read_text(), "void storage_repair_after_mount(void)")
    assert "add_connector_command_inbox_merge_pending()" in after
    outbox = _body(connector, "static void outbox_task(void *arg)\n{")
    assert outbox.index("if (!add_connector_is_connected()) {") < outbox.index("storage_repair_report_step();")
    # Evidence sends make direct priority senders wait: never inside a hold.
    assert outbox.index("if (priority_delivery_hold_active()) {") < outbox.index("storage_repair_report_step();")
    glue = (MAIN / "storage_repair.c").read_text()
    step = _body(glue, "void storage_repair_report_step(void)")
    # Nothing pending: no NVS read, no allocation.
    assert step.index("if (!atomic_load(&s_records_pending)) return;") < step.index("heap_caps_malloc(")
    # The evidence buffers live in PSRAM, not on the outbox task's stack.
    assert "char generation[80], record[120], raw[512];" not in glue.replace("    char generation[80], record[120], raw[512];\n} sr_send_t;", "")
    assert "if (!storage_repair_append(storage)) goto failed;" in connector


def test_rollback_tag_names_the_image_the_bootloader_returns_to() -> None:
    # A reset of an unconfirmed image boots the other otadata entry only while
    # the bootloader still accepts it (bootloader_common_ota_select_valid:
    # not INVALID, not ABORTED); otherwise, as on the first OTA after a
    # factory flash (the CI flash writes the factory slot), the factory app.
    glue = (MAIN / "storage_repair.c").read_text()
    pick = _body(glue, "static const esp_partition_t *rollback_partition(const esp_partition_t *running)")
    assert "esp_ota_get_state_partition(other, &state) == ESP_OK" in pick
    for rejected in ("ESP_OTA_IMG_INVALID", "ESP_OTA_IMG_ABORTED", "ESP_OTA_IMG_PENDING_VERIFY"):
        assert f"state != {rejected}" in pick
    assert pick.index("return other;") < pick.index(
        "esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_FACTORY, NULL)")
    config = _body(glue, "static srr_config_t boot_config(void)")
    assert "rollback_partition(running)" in config
    assert "esp_ota_get_next_update_partition" not in config
    partitions = (ROOT / "firmware/zone_lite/partitions.csv").read_text()
    assert re.search(r"^factory,\s*app,\s*factory,", partitions, re.M)
