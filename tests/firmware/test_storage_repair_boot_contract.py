"""ZKT 2.7.5: where the boot storage repair runs and what it may touch; 2.7.6:
its last-resort in-place reformat and the redo area past the partition table."""
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
    order = ["storage_repair_boot();", "seen_cache_init();", "if (storage_mount_allowed()) spiffs_ret = esp_vfs_spiffs_register(&conf);",
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
    for source in ("storage_repair_plan.c", "storage_repair_mirror.c", "storage_repair_redo.c", "storage_repair_core.c",
                   "storage_repair.c"):
        assert f'"{source}"' in block
    assert "ZONE_LITE_STORAGE_REPAIR=1" in block
    assert 'idf_component_get_property(zone_lite_spiffs_dir spiffs COMPONENT_DIR)' in block
    app = (MAIN / "zone_lite.c").read_text()
    for call in ("storage_repair_boot();", "storage_repair_after_mount();"):
        guard = app[:app.index(call)].rsplit("#if", 1)[1]
        assert guard.startswith(" defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR")


def test_no_reformat_and_the_check_callback_never_calls_spiffs() -> None:
    # 2.7.6 reformats in place, block by block, never the whole partition.
    for name in ("storage_repair_core.c", "storage_repair.c", "storage_repair_plan.c", "storage_repair_mirror.c",
                 "storage_repair_redo.c"):
        text = (MAIN / name).read_text()
        assert "SPIFFS_format" not in text and "esp_spiffs_format" not in text
        assert "format_if_mount_failed = true" not in text
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


def test_controlled_reboot_window_never_meets_the_every_boot_repair() -> None:
    # hil_reboot.py's controlled ESP reboot is issued in observation minute six
    # and must be healthy again by minute eight (as little as two minutes),
    # while the repair may take up to its 5 or 15 minute deadline. The two
    # never meet: the reboot belongs to the 2.7.0 journal writer, which does
    # not build the repair, and no image that runs the repair (2.7.5, 2.7.6
    # and the other platform images) can take or be offered the reboot. Making
    # one reboot-capable needs a recovery window that covers the repair first;
    # 2.7.6's rounds and redo recovery run inside the same deadlines.
    component = (MAIN / "CMakeLists.txt").read_text()
    repair = component[component.index("# ZKT 2.7.5: the custody-first boot storage repair"):]
    assert "NOT ZONE_LITE_JOURNAL_WRITES)" in repair[:repair.index("endif()")]
    writer = component[component.index("if(ZONE_LITE_JOURNAL_WRITES)\n"):]
    writer = writer[:writer.index("\nendif()\n")]
    assert 'elseif(PROJECT_VER STREQUAL "2.7.0")' in writer
    assert writer.index('elseif(PROJECT_VER STREQUAL "2.7.0")') < writer.index("ZONE_LITE_JOURNAL_WRITER_IMAGE=1")
    assert component.count("ZONE_LITE_JOURNAL_WRITER_IMAGE=1") == 1
    capable = _body((MAIN / "ota_manager.c").read_text(), "bool ota_manager_hil_reboot_capable(void)")
    assert "#if defined(ZONE_LITE_JOURNAL_WRITER_IMAGE)" in capable
    assert '!strcmp(app->version, "2.7.0")' in capable
    reboot = (ROOT / "apps/add_backend/zk_add/hil_reboot.py").read_text()
    scope = reboot[reboot.index("def _scope("):reboot.index("def _exclusive(")]
    assert 'release.version != "2.7.0"' in scope and '!= "ZKT_JOURNAL_V1"' in scope
    evidence = reboot[reboot.index("def collect_reboot_evidence("):]
    assert 'release.version == "2.7.0"' in evidence
    contract = (ROOT / "apps/add_backend/zk_add/storage_contract.py").read_text()
    platform = re.search(r"^FLEET_PLATFORM_VERSIONS = \((.*)\)$", contract, re.M)
    assert platform and '"2.7.5"' in platform.group(1) and '"2.7.6"' in platform.group(1)
    assert '"2.7.0"' not in platform.group(1)


def test_the_redo_area_is_reached_only_through_its_own_registered_handle() -> None:
    glue = (MAIN / "storage_repair.c").read_text()
    # Exactly the 384 KiB past the last partition of the 16 MiB chip.
    assert "#define SR_REDO_OFFSET 0xFA0000U" in glue and "#define SR_REDO_BYTES 0x60000U" in glue
    register = _body(glue, "static srx_area_state_t redo_register(void)")
    assert "esp_flash_get_physical_size(esp_flash_default_chip, &physical)" in register
    assert "partition->address + partition->size > SR_REDO_OFFSET" in register
    assert ("esp_partition_register_external(NULL, SR_REDO_OFFSET, SR_REDO_BYTES, \"srp_redo\", "
            "ESP_PARTITION_TYPE_DATA,") in register
    # Every write and erase of the area goes through that handle; no raw
    # esp_flash call anywhere in the repair.
    for name in ("storage_repair.c", "storage_repair_core.c", "storage_repair_redo.c"):
        text = (MAIN / name).read_text()
        assert "esp_flash_write" not in text and "esp_flash_erase" not in text and "spi_flash_write" not in text
    assert "esp_partition_write(s_redo, address, s_bounce, n)" in _body(glue, "static bool area_write(")
    assert "esp_partition_erase_range(s_redo, address, length)" in _body(glue, "static bool area_erase(")
    # Discovery reads every sector before anything is written to the area,
    # and an area that is not READY (foreign, absent, overlapping) is never
    # written: the engine checks the state before a round.
    recover = _body(glue, "static void redo_recover(srr_config_t *config, bool timed)")
    assert recover.index("redo_register()") < recover.index("srx_discover(") < recover.index("srx_recover(")
    core = (MAIN / "storage_repair_core.c").read_text()
    assert "c->area.state == SRX_AREA_READY" in _body(core, "static SR_NOINLINE void reclaim_round(run_t *r, const char *trigger)")
    # Deregistered on every way out of the boot.
    boot = _body(glue, "void storage_repair_boot(void)")
    assert boot.count("esp_partition_deregister_external(s_redo)") == 2


def test_the_redo_runs_on_raw_flash_before_any_mount_in_every_mode() -> None:
    glue = (MAIN / "storage_repair.c").read_text()
    boot = _body(glue, "void storage_repair_boot(void)")
    # Before the mirror and before the private mount, and before the run can
    # decide REPORT_ONLY; the hard deadline already armed.
    assert boot.index("esp_timer_start_once(s_deadline_timer") < boot.index("redo_recover(&config, timed);")
    assert boot.index("redo_recover(&config, timed);") < boot.index("srm_init(") < boot.index("SPIFFS_mount(")
    assert boot.index("redo_recover(&config, timed);") < boot.index("if (!timed) {")
    # A block that may be open and cannot be journaled keeps the store
    # unmounted: no private mount, and storage_init does not mount it.
    assert "&& !s_mount_blocked;" in boot
    assert "bool storage_repair_storage_allowed(void) { return !s_mount_blocked; }" in glue
    app = (MAIN / "zone_lite.c").read_text()
    allowed = _body(app, "static bool storage_mount_allowed(void)")
    assert "return storage_repair_storage_allowed();" in allowed
    init = _body(app, "static void storage_init(void)")
    assert "if (storage_mount_allowed()) spiffs_ret = esp_vfs_spiffs_register(&conf);" in init
    # The redo decision table: a record is replayed only over a block with no
    # magic, the old image or the target, never for another image or an
    # unconfirmed one, and a source never again.
    redo = (MAIN / "storage_repair_redo.c").read_text()
    decide = _body(redo, "srx_action_t srx_decide(")
    assert "(r->flags & SRX_FLAG_UNCONFIRMED) || r->image_tag != running_tag || force_sources" in decide
    assert "srx_attempts_used(r->attempts) >= SRX_ATTEMPTS" in decide
    assert "!srx_has_magic(raw_block, block_count, r->block) || crc == r->old_crc" in decide
    assert "if (r->done != 0xFFFFFFFFU || srx_is_source(r)) return SRX_ACT_NONE;" in decide
    # The magic is cleared first and programmed last.
    writer = _body(redo, "srx_write_t srx_write_block(")
    assert writer.index("base + SRX_MAGIC_OFFSET, cleared, 2") < writer.index("io->store_erase(") \
        < writer.index("base + SRX_ERASE_COUNT_OFFSET") < writer.index("base + SRX_MAGIC_OFFSET, target + SRX_MAGIC_OFFSET")


def test_a_round_never_comes_between_a_check_pass_and_its_restore() -> None:
    core = (MAIN / "storage_repair_core.c").read_text()
    assert "reclaim_round(" not in _body(core, "static bool run_checks(")
    run = _body(core, "static void run_once(")
    # At the end of a run: after its finish, its result and its journal.
    assert run.index("finish_report(r);") < run.index("const char *trigger = reclaim_trigger(r, check_failed);") \
        < run.index("if (trigger) reclaim_round(r, trigger);")
    assert core.count("reclaim_round(r, trigger)") == 1
    # The store is remounted after a round, never patched.
    assert "r->ports->remount(r->ports->context)" in _body(core, "static SR_NOINLINE void reclaim_round(run_t *r, const char *trigger)")
    assert "free_blocks++" not in core and "stats_p_deleted -=" not in core


def test_the_deadline_waits_for_an_open_window_and_brownouts_count() -> None:
    glue = (MAIN / "storage_repair.c").read_text()
    expired = _body(glue, "static void deadline_expired(void *argument)")
    assert "atomic_load(&s_window_open) && !atomic_exchange(&s_deadline_deferred, true)" in expired
    assert "esp_timer_start_once(s_deadline_timer, SR_WINDOW_GRACE_US)" in expired
    assert expired.index("SR_WINDOW_GRACE_US") < expired.index("esp_restart();")
    assert "#define SR_WINDOW_GRACE_US 2000000LL" in glue
    assert "reason == ESP_RST_BROWNOUT" in _body(glue, "static srr_config_t boot_config(void)")


def test_the_heartbeat_reclaim_fields_are_the_ones_add_accepts() -> None:
    glue = (MAIN / "storage_repair.c").read_text()
    append = _body(glue, "bool storage_repair_append(cJSON *storage)")
    sent = set(re.findall(r'cJSON_Add\w+ToObject\(reclaim, "([a-z_]+)"', append))
    schemas = (ROOT / "apps/add_backend/zk_add/schemas.py").read_text()
    model = schemas[schemas.index("class StorageRepairReclaim(BaseModel):"):schemas.index("class StorageRepairDiagnostics(")]
    accepted = set(re.findall(r"^    ([a-z_]+): ", model, re.M))
    assert sent and sent <= accepted, sent - accepted
    assert 'cJSON_AddNumberToObject(repair, "runs", r->runs)' in append
    assert '{SRR_FILE_REDO_SOURCE, "REDO_SOURCE"}' in glue


def test_a_boot_without_its_deadline_writes_nothing_and_returns_its_breadcrumb() -> None:
    # Round 1 review, low 2: without the deadline timer no flash is written
    # (open records go to the journal as sources, as for an area that cannot
    # be read), the boot's repair breadcrumb says it returned, and the
    # REFORMAT breadcrumb is set only while recovery acts on an open record.
    glue = (MAIN / "storage_repair.c").read_text()
    boot = _body(glue, "void storage_repair_boot(void)")
    no_timer = boot[boot.index("if (!timed) {"):]
    no_timer = no_timer[:no_timer.index("return;")]
    assert "boot_report_repair_returned();" in no_timer
    assert "srx_recover(" not in no_timer and "SPIFFS_mount(" not in no_timer
    recover = _body(glue, "static void redo_recover(srr_config_t *config, bool timed)")
    assert "inspected = timed && (s_area.state == SRX_AREA_READY || s_area.state == SRX_AREA_FOREIGN);" in recover
    assert recover.count("boot_report_repair(") == 1 and recover.count("srx_recover(") == 1
    acts = recover[recover.index("if (inspected && records_open) {"):]
    assert acts.index("boot_report_repair(SRP_PHASE_REFORMAT, 0, false);") < acts.index("srx_recover(") < acts.index("}")
    assert recover.index("srx_discover(") < recover.index("if (inspected && records_open) {")
    # A boot that cannot read records that may hold bytes tells the engine.
    assert "config->sources_unread = have && srx_sources_unread(&journal, inspected);" in recover
    assert "srx_journal_after_recovery(&journal, inspected, open, srx_sources_left(&s_area))" in recover


def test_the_redo_geometry_is_pinned_at_build_time_and_checked_before_any_round() -> None:
    # Round 1 review, low 5: the records and the round hold 256-byte pages in
    # 4 KiB blocks with SPIFFS's length magic.
    glue = (MAIN / "storage_repair.c").read_text()
    assert '_Static_assert(CONFIG_SPIFFS_PAGE_SIZE == SRX_PAGE, ' in glue
    assert '_Static_assert(SRX_BLOCK == 4096U && SRX_ENTRIES + 1U == SRX_BLOCK / SRX_PAGE, ' in glue
    assert "#if !defined(CONFIG_SPIFFS_USE_MAGIC) || !defined(CONFIG_SPIFFS_USE_MAGIC_LENGTH)" in glue
    register = _body(glue, "static srx_area_state_t redo_register(void)")
    assert "s_partition->erase_size != SRX_BLOCK || s_partition->size % SRX_BLOCK" in register
    assert "uint32_t page = CONFIG_SPIFFS_PAGE_SIZE, block = SRX_BLOCK;" in _body(glue, "void storage_repair_boot(void)")
    core = (MAIN / "storage_repair_core.c").read_text()
    assert "_Static_assert(SPIFFS_USE_MAGIC && SPIFFS_USE_MAGIC_LENGTH, " in core
    geometry = _body(core, "static bool redo_geometry(const run_t *r)")
    assert "r->page_size == SRX_PAGE && r->block_size == SRX_BLOCK && r->ppb == SRX_ENTRIES + 1U" in geometry
    round_ = _body(core, "static SR_NOINLINE void reclaim_round(run_t *r, const char *trigger)")
    assert "bool geometry = redo_geometry(r);" in round_
    assert "r->ports->remount && r->rx &&\n        geometry;" in round_
    assert "!redo_geometry(r)) return true;" in _body(core, "static SR_NOINLINE bool load_sources(run_t *r)")
    assert "redo_geometry(r)" in _body(core, "static bool round_possible(const run_t *r)")


def test_a_record_nothing_marks_a_source_keeps_the_store_unmounted() -> None:
    # Round 2 review, R2-4: a round whose failed block's record could be made
    # neither a source nor journaled as one stops writing the store, and the
    # VFS does not mount it that boot. R2-2: records recovery leaves open are
    # journaled as sources even when NVS had no reclaim journal.
    glue = (MAIN / "storage_repair.c").read_text()
    boot = _body(glue, "void storage_repair_boot(void)")
    run = boot[boot.index("srr_run(&s_fs, &config, &PORTS, &s_report);"):]
    assert run.index("SPIFFS_unmount(&s_fs);") < run.index("if (s_report.reclaim.store_blocked) s_mount_blocked = true;")
    recover = _body(glue, "static void redo_recover(srr_config_t *config, bool timed)")
    assert "if (srx_journal_after_recovery(&journal, inspected, open, srx_sources_left(&s_area)) &&" in recover
    assert "have && srx_journal_after_recovery" not in recover
    core = (MAIN / "storage_repair_core.c").read_text()
    round_ = _body(core, "static SR_NOINLINE void reclaim_round(run_t *r, const char *trigger)")
    assert round_.index("if (r->unguarded && !journaled) {") < round_.index("r->ports->remount(r->ports->context)")
    failed = _body(core, "static SR_NOINLINE bool failed_block(")
    assert failed.index("srx_close_block(") < failed.index("SRX_OFF_SETTLE, SRX_SETTLE_SOURCE")
