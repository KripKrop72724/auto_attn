"""Connectivity, redaction, records, validation, naming and the text catalogue."""
from __future__ import annotations

import ast
import base64
import hashlib
import logging
import re
import zipfile
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from sl_installer.core import naming, texts, validation
from sl_installer.core.api import (Advice, BootOutcome, BootReport, BundleInfo, Family,
                                   HikSettings, InstallMode, InstallSummary, ZoneSummary,
                                   ZktSettings)
from sl_installer.core.connectivity import Connectivity
from sl_installer.core.logging_setup import (RedactingFilter, redact, register_secret,
                                             setup_logging)
from sl_installer.core.records import Records

from . import support
from .support import MAC


# ------------------------------------------------------------ connectivity

def connectivity(handler):
    return Connectivity("https://autoattn.test", transport=httpx.MockTransport(handler))


def test_connectivity_all_good():
    def handler(request):
        if request.url.host == "www.msftconnecttest.com":
            return httpx.Response(200, text="Microsoft Connect Test")
        assert request.url.path == "/health/live"
        return httpx.Response(200, json={"ok": True, "app": "attendance-device-dashboard"})
    status = connectivity(handler).check()
    assert status.internet and status.server


def test_connectivity_server_down_but_internet_up():
    def handler(request):
        if request.url.host == "www.msftconnecttest.com":
            return httpx.Response(200, text="Microsoft Connect Test")
        raise httpx.ConnectError("refused")
    status = connectivity(handler).check()
    assert status.internet and not status.server and "head office" in status.detail


def test_connectivity_captive_portal_and_offline():
    portal = connectivity(lambda r: httpx.Response(200, text="<html>Sign in to Wi-Fi</html>")
                          if r.url.host == "www.msftconnecttest.com" else httpx.Response(302))
    status = portal.check()
    assert not status.internet and "sign in" in status.detail.lower()

    def offline(request):
        raise httpx.ConnectError("[Errno -2] Name or service not known")
    assert connectivity(offline).check() == connectivity(offline).check()
    assert not connectivity(offline).check().internet


def test_server_answering_proves_internet():
    def handler(request):
        if request.url.host == "www.msftconnecttest.com":
            raise httpx.ConnectError("blocked")
        return httpx.Response(200, json={"ok": True})
    assert connectivity(handler).check().internet


# --------------------------------------------------------------- redaction

ENVELOPE_BLOB = base64.b64encode(hashlib.sha512(b"k").digest()).decode()


@pytest.mark.parametrize("text, secret", [
    ('{"password": "test-admin-pass", "user": "x"}', "test-admin-pass"),
    ('{"zkt_comm_key": 1979}', "1979"),
    ("wifi_password=test-wifi-pass;next", "test-wifi-pass"),
    ("X-CSRF-Token: abcdef0123456789", "abcdef0123456789"),
    ("Cookie: add_admin=Zm9vYmFyYmF6", "Zm9vYmFyYmF6"),
    ("Authorization: Bearer sk-live-123456", "sk-live-123456"),
    ("hmac_key=deadbeef", "deadbeef"),
    ("envelope " + ENVELOPE_BLOB, ENVELOPE_BLOB[:24]),
])
def test_redaction_removes_secrets(text, secret):
    assert secret not in redact(text)


@pytest.mark.parametrize("text", [
    "sha256 f39db7c1c7626ee6156dbab5da7912968040a489d1d927c1fb288d1f42c5ec70",
    "/Users/statelifehealth/Downloads/auto_attn-installer/apps/esp_installer/src/x.bin",
    "STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED and SECURE_BOOT_DIGEST0",
    "Wrote 3072 bytes (103 compressed) at 0x00010000 in 0.1 seconds",
    "board ac:27:6e:a4:3d:30 zone LF-ZONE-GUJRAT-01",
])
def test_redaction_keeps_useful_facts(text):
    assert redact(text) == text


def test_registered_secrets_vanish_everywhere():
    register_secret("Sup3r-Secret-Pw")
    register_secret("1979")  # too short to blank safely; caught by name rules instead
    assert redact("typed Sup3r-Secret-Pw twice: Sup3r-Secret-Pw") == \
        "typed [redacted] twice: [redacted]"
    assert redact("Wrote 1979 bytes") == "Wrote 1979 bytes"


def test_log_file_is_redacted(tmp_path):
    path = setup_logging(tmp_path)
    logger = logging.getLogger("sl_installer.test")
    logger.info("sign-in with password=%s", "test-admin-pass")
    try:
        raise RuntimeError("token=abc123def456")  # gitleaks:allow -- fake token for this test
    except RuntimeError:
        logger.exception("failed")
    for handler in logging.getLogger("sl_installer").handlers:
        handler.flush()
    content = path.read_text(encoding="utf-8")
    assert "test-admin-pass" not in content and "abc123def456" not in content
    assert "sign-in with password=[redacted]" in content
    logging.getLogger("sl_installer").propagate = True


def test_filter_handles_bad_format_strings():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s", ("only-one",), None)
    assert RedactingFilter().filter(record) and record.args is None


# ---------------------------------------------------------------- records

def summary(config=None):
    config = config or support.zkt_config()
    return InstallSummary("sli-0123456789abcdef", InstallMode.NEW_BOARD, MAC, config,
                          BundleInfo("zkt-2.9.1-e89380a1", Family.ZKT, "ZKT 2.9.1", "2.9.1",
                                     True, "AVAILABLE"),
                          "2026-11-01T10:00:00Z", "2026-11-01T10:04:00Z")


def boot_ok():
    return BootReport(BootOutcome.OK, "LF-ZONE-GUJRAT-01", "1", "2.9.1",
                      advice=texts.boot_ok("LF-ZONE-GUJRAT-01", "1", "2.9.1"))


def test_install_record_has_the_facts_and_no_secrets(tmp_path):
    records = Records(tmp_path / "records", tmp_path / "logs", "1.0.0",
                      now=lambda: datetime(2026, 11, 1, 15, 30, 0))
    path = Path(records.save_install_record(summary(), boot_ok()))
    # The record is UTF-8 ("·" in the public name); Windows' own default is cp1252.
    text = path.read_text(encoding="utf-8")
    assert path.name == "setup-LF-ZONE-GUJRAT-01-1-ac276ea43d30-20261101-153000.txt"
    for fact in ("ac:27:6e:a4:3d:30", "LF-ZONE-GUJRAT-01", "Gujrat · Device 1 (Life)",
                 "SLIC ATTENDANCE-3D31", "http://192.168.254.1", "ZKT 2.9.1",
                 "The board started correctly", "version 2.9.1", "RE7"):
        assert fact in text
    assert "test-wifi-pass" not in text and "1979" not in text


def test_hikvision_record_omits_the_machine_password(tmp_path):
    config = support.zkt_config(family=Family.HIKVISION, zkt=None, hik=HikSettings(
        host="192.168.1.2", password="test-hik-pass", expected_serial="S1", profile="p",
        source_epoch="lf-zone-x-20261101"))
    text = Records(tmp_path, tmp_path, "1.0.0").install_record_text(summary(config), None)
    assert "192.168.1.2" in text and "lf-zone-x-20261101" in text
    assert "test-hik-pass" not in text and "Not watched." in text


def test_help_file_zips_redacted_logs_and_records(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "installer.log").write_text("ok line\npassword=test-admin-pass\n", encoding="utf-8")
    (logs / "installer.log.1").write_text("older\n", encoding="utf-8")
    records = Records(tmp_path / "records", logs, "1.0.0")
    records.save_install_record(summary(), boot_ok())
    with zipfile.ZipFile(records.save_help_file()) as archive:
        names = set(archive.namelist())
        assert {"about-this-computer.txt", "logs/installer.log", "logs/installer.log.1"} <= names
        assert any(name.startswith("records/setup-") for name in names)
        assert "test-admin-pass" not in archive.read("logs/installer.log").decode()


# ------------------------------------------------------- validation, naming

@pytest.mark.parametrize("zone_id, ok", [
    ("LF-ZONE-GUJRAT-01", True), ("ZONE-SLICTOWER-3FL", True), ("A" * 64, True),
    ("A" * 65, False), ("LF-ZONE-G&PBLD8-01", False), ("-ZONE", False), ("", False),
    ("ZONE ONE", False),
])
def test_zone_id_rules(zone_id, ok):
    assert (validation.check_zone_id(zone_id) is None) is ok


@pytest.mark.parametrize("password, ok", [
    ("12345678", True), ("x" * 63, True), ("x" * 64, False), ("a1" * 32, True),
    ("1234567", False), ("", False),
])
def test_wifi_password_rules(password, ok):
    assert (validation.check_wifi("RE7", password) is None) is ok


def test_other_field_rules():
    assert validation.check_zone_name(" padded") is not None
    assert validation.check_zone_name("X" * 121) is not None
    assert validation.check_zone_name("LF-ZONE-GUJRAT-01") is None
    assert validation.check_device_id("1") is None and validation.check_device_id("x" * 32)
    assert validation.check_zkt(ZktSettings(comm_key=1979)) is None
    assert validation.check_zkt(ZktSettings(port=0)).code == "INVALID_FIELD:zkt_port"
    assert validation.check_zkt(ZktSettings(comm_key=-1)) is not None
    assert validation.check_zkt(ZktSettings(preferred_ip="8.8.8.8")) is not None
    assert validation.check_zkt(ZktSettings(preferred_ip="192.168.1.20")) is None
    hik = HikSettings(host="192.168.1.2", password="pw", expected_serial="S1")
    assert validation.check_hik(hik) is None
    from dataclasses import replace
    assert validation.check_hik(replace(hik, host="169.254.1.2")) is not None
    assert validation.check_hik(replace(hik, expected_serial="")).code == \
        "INVALID_FIELD:hik_expected_serial"
    assert validation.check_hik(replace(hik, transport="telnet")) is not None
    # One NVS string holds at most 3967 bytes; the package builder fails on more.
    assert validation.check_hik(replace(hik, ca_pem="x" * 3967)) is None
    assert validation.check_hik(replace(hik, ca_pem="x" * 3968)).code == \
        "INVALID_FIELD:hik_ca_pem"
    assert validation.check_hik(replace(hik, ca_pem="é" * 1984)) is not None  # 3968 bytes


def test_hikvision_https_needs_the_machine_certificate():
    from dataclasses import replace

    # Without one the board checks the machine against the public certificate
    # authorities and never reaches it (review CR2); the app cannot add one.
    hik = HikSettings(host="192.168.1.2", password="pw", expected_serial="S1", port=443,
                      transport="https")
    assert validation.check_hik(hik).code == "INVALID_FIELD:hik_transport"
    assert validation.check_hik(replace(hik, ca_pem=" \n")).code == "INVALID_FIELD:hik_transport"
    assert validation.check_hik(replace(hik, ca_pem="test-ca")) is None
    assert validation.check_hik_transport("http_digest") is None
    assert validation.check_hik_transport("https") is not None
    # The engine checks this before it asks head office for the board's settings.
    config = support.zkt_config(family=Family.HIKVISION, zkt=None, hik=hik)
    assert [a.code for a in validation.problems(config)] == ["INVALID_FIELD:hik_transport"]
    steps = texts.invalid_field("hik_transport").what_to_do
    assert "Under Advanced settings, set Connection to Normal (HTTP)." in steps
    assert not any("HTTPS" in step for step in steps)


def test_problems_lists_every_field_in_screen_order():
    config = support.zkt_config(zone_id="bad id", device_id="", wifi_password="short")
    assert [a.code for a in validation.problems(config)] == [
        "INVALID_FIELD:zone_id", "INVALID_FIELD:device_id", "INVALID_FIELD:wifi_password"]


def test_hik_profile_and_epoch_defaults():
    assert validation.hik_profile_for(HikSettings("192.168.1.2", "p", model_key="ds-k1t341amf")) \
        == "ds-k1t341amf-unverified-v1"
    assert validation.default_source_epoch("LF-ZONE-JHELUM-01", date(2026, 10, 30)) == \
        "lf-zone-jhelum-01-20261030"
    long_epoch = validation.default_source_epoch("Z" * 64, date(2026, 10, 30))
    assert len(long_epoch) == 64 and long_epoch.endswith("-20261030")
    assert validation.default_source_epoch("Z", date(2026, 1, 1), "kept") == "kept"


@pytest.mark.parametrize("mac, name", [
    ("14:c1:9f:46:ae:1c", "SLIC ATTENDANCE-AE1D"),
    ("ac:27:6e:a3:16:d4", "SLIC ATTENDANCE-16D5"),
    ("AC276EA43D30", "SLIC ATTENDANCE-3D31"),
    ("ac:27:6e:a4:12:ff", "SLIC ATTENDANCE-1200"),  # +1 without carry
    ("not a mac", None),
])
def test_hotspot_name(mac, name):
    assert naming.hotspot_name(mac) == name


@pytest.mark.parametrize("zone_name, name, group", [
    ("LF-ZONE-BLD9-03", "Building 9 · Device 3", "Life"),
    ("LF-ZONE-G&PBLD8-01", "Group & Pension Building 8 · Device 1", "Life"),
    ("ZONE-SLICTOWER-3FL", "SLIC Tower · 3rd Floor", "Health"),
    ("ZONE-SLICTOWER-13FL", "SLIC Tower · 13th Floor", "Health"),
    ("ZONE-LAHORE-02", "Lahore · Device 2", "Health"),
    ("LF ZONE GUJRAT 01", "Gujrat · Device 1", "Life"),
])
def test_public_name_preview(zone_name, name, group):
    preview = naming.public_name_preview(zone_name)
    assert (preview.name, preview.group) == (name, group)


def test_zone_name_suggestion_reuses_head_office_name():
    zones = [ZoneSummary("ZONE-LAHORE-02", "ZONE-LAHORE-02", ("1",))]
    assert naming.suggest_zone_name("zone-lahore-02", zones) == "ZONE-LAHORE-02"
    assert naming.suggest_zone_name("lf-zone-new-01", zones) == "LF-ZONE-NEW-01"


# ------------------------------------------------------------ text catalogue

JARGON = re.compile(r"\b(e-?fuses?|nvs|partitions?|com ?ports?|mac address(es)?|esp32\S*|"
                    r"esp|hmac|spiffs|bootloader|firmware flash|flash (memory|chip|size)|"
                    r"flashing the board|download mode|serial port|stub)\b", re.I)


def all_texts():
    for advice in texts.STATIC.values():
        yield advice
    yield texts.restart_loop_faulty(10.3)
    yield texts.restart_loop_fast(2.0)
    yield texts.login_paused(9)
    yield texts.invalid_field("zone_id")
    yield texts.family_mismatch(Family.HIKVISION)
    yield texts.device_id_in_use("1", "ZONE-X", "2")
    yield texts.boot_ok("Z", "1", "2.9.1")
    yield texts.wrong_settings("zone A", "zone B")
    yield texts.wifi_placeholder("RE7", "SLIC ATTENDANCE-3D31")
    yield texts.wifi_not_joining("SiteNet", None)
    yield texts.hotspot_open("SLIC ATTENDANCE-3D31")
    yield texts.board_offline(None)
    yield texts.resume_needed(texts.AUTO_DOWNLOAD_FAILED)
    yield texts.rate_limited(7)
    for cause in (texts.invalid_field("zone_id"), texts.device_id_in_use("1", "Z", "2"),
                  texts.PORT_BUSY, texts.SERVER_UNREACHABLE):
        yield texts.resume_needed(cause)
    for state in texts.LED_GUIDE:
        yield texts.light_hint(state)


@pytest.mark.parametrize("advice", list(all_texts()), ids=lambda a: a.code)
def test_every_advice_is_complete_and_plain(advice: Advice):
    assert advice.code and advice.title and advice.what_happened
    assert advice.illustration is None or advice.illustration in texts.ILLUSTRATIONS
    for text in (advice.title, advice.what_happened, *advice.what_to_do):
        assert not JARGON.search(text), (advice.code, text)
        assert text.strip() == text and "  " not in text


def test_errors_that_cannot_be_retried_say_what_to_do_instead():
    for advice in texts.STATIC.values():
        if not advice.can_retry:
            assert advice.what_to_do, advice.code


def test_light_guide_is_complete():
    keys = {state.key for state in texts.LED_GUIDE}
    firmware_states = {"BOOTING", "WIFI_CONNECTING", "ZKT_DISCOVERING", "ZKT_AUTHENTICATED",
                       "SYNCING", "HEALTHY", "BACKLOG", "ORDS_FAILURE", "ZKT_FAILURE",
                       "ZKT_FLAPPING", "TRUTH_REPAIR", "RECOVERY_REBOOT", "LOCAL_FAILURE",
                       "FATAL", "BLOCKED_IDENTITY", "STORAGE_REPAIR"}
    assert firmware_states <= keys and "OFF" in keys
    for state in texts.LED_GUIDE:
        assert state.rhythm in texts.LED_RHYTHMS and state.what_to_do
        assert not JARGON.search(state.meaning)
    assert [s.key for s in texts.led_states(rhythm="slow blink", colour="blue")] == [
        "WIFI_CONNECTING"]


def test_setup_mode_advice_names_the_board_buttons_and_the_connect_screen_s_next():
    advice = texts.SETUP_MODE_NEEDED
    assert advice.title == texts.AUTO_DOWNLOAD_FAILED.title == "Put the board into setup mode"
    # The button steps the app already gives when it cannot start setup mode by itself.
    assert advice.what_to_do[:3] == texts.AUTO_DOWNLOAD_FAILED.what_to_do[:3] == (
        "Hold down the BOOT button on the board.",
        "While you hold BOOT, press and release the RST button.",
        "Let go of BOOT.")
    assert advice.illustration == texts.AUTO_DOWNLOAD_FAILED.illustration == "boot_button"
    assert advice.severity is texts.WRONG_SOCKET.severity
    # Connect does not move on by itself: it says Board found and waits for Next.
    assert advice.what_to_do[3] == ("The app then finds the board by itself. When this "
                                    "screen says Board found, click Next.")
    # When the buttons do not work: unplug, and hold BOOT while the board goes back in.
    unplug, replug, last = advice.what_to_do[4:]
    assert unplug.startswith("If you still see this message") and "unplug the board" in unplug
    assert replug.startswith("Hold down BOOT while you plug the board back into the socket "
                             "marked USB.") and replug.endswith("Then let go of BOOT.")
    # Then a help file for head office: a board running other software is not faulty, and
    # a new board's demo is normal (not UNSECURED_USED's "another tool changed it").
    assert last.startswith("If you still see this message after 3 tries, open Help.")
    assert "Save a help file" in last and "head office" in last
    assert advice.what_happened.endswith("This is normal for a new board.")
    words = " ".join((advice.title, advice.what_happened) + advice.what_to_do)
    assert words.isascii() and "other software" in advice.what_happened
    # The board's buttons and its socket's mark.
    assert set(re.findall(r"\b[A-Z]{2,}\b", words)) == {"BOOT", "RST", "USB"}
    # Connect's only way on, and the help file's button inside Help.
    assert re.findall(r"\b[Cc]lick (\w+)", words) == ["Next", "Save"]
    for sentence in re.split(r"(?<=\.) ", words.replace(advice.title, "", 1).strip()):
        assert len(sentence.split()) <= 14, sentence  # short sentences
    # Shown where the board is connected but not ready: no cable or faulty-board tips.
    assert not re.search(r"cable|faulty|hub|aside", words, re.I)


def test_resume_advice_keeps_the_cause_steps():
    advice = texts.resume_needed(texts.AUTO_DOWNLOAD_FAILED)
    assert advice.code == "RESUME_NEEDED" and advice.illustration == "boot_button"
    assert "Hold down the BOOT button on the board." in advice.what_to_do
    assert advice.what_to_do[0] == "Do not throw the board away."
    assert advice.what_to_do[-1] == "Click Finish the setup."
    assert texts.resume_needed(None) is texts.RESUME_NEEDED


# Once the key is locked, the screen offers "Finish the setup" (and "Change the
# details" when the details were the problem), never Try again or Next.
_NO_BUTTON_THERE = re.compile(r"click (?:Try again|Next)|^If it ", re.I)


@pytest.mark.parametrize("cause", [a for a in texts.STATIC.values() if a.can_retry],
                         ids=lambda a: a.code)
def test_resume_advice_names_only_buttons_on_its_screen(cause):
    advice = texts.resume_needed(cause)
    for step in advice.what_to_do:
        assert not _NO_BUTTON_THERE.search(step), (cause.code, step)
    assert advice.what_to_do[-1] == "Click Finish the setup."
    assert sum("Finish the setup" in step for step in advice.what_to_do) == 1


def test_resume_advice_keeps_what_to_do_without_the_retry_click():
    assert "Unplug the board, plug it back in." in texts.resume_needed(texts.PORT_BUSY).what_to_do
    assert "Wait a few minutes." in texts.resume_needed(texts.SERVER_UNREACHABLE).what_to_do


@pytest.mark.parametrize("cause", [texts.invalid_field("device_id"),
                                   texts.device_id_in_use("1", "LF-ZONE-BLD9-04", "2")],
                         ids=lambda a: a.code)
def test_resume_after_a_details_problem_points_to_change_the_details(cause):
    steps = texts.resume_needed(cause).what_to_do
    assert steps[-1].startswith("Click Change the details") and "Finish the setup" in steps[-1]
    assert not any(re.search(r"click Next|^Click Change the details\.$", s) for s in steps)


def test_help_file_is_saved_from_the_help_panel():
    # "Save a help file" is a button inside Help only, so every step says to open Help.
    steps = [step for advice in all_texts() for step in advice.what_to_do
             if "Save a help file" in step]
    assert steps and all("open Help" in step or "Open Help" in step for step in steps)


@pytest.mark.parametrize("advice", [
    texts.wifi_placeholder("RE7", "SLIC ATTENDANCE-3D31"),
    texts.hotspot_open("SLIC ATTENDANCE-3D31"),
    texts.wifi_not_joining("SiteNet", "SLIC ATTENDANCE-3D31")], ids=lambda a: a.code)
def test_board_wifi_password_is_called_what_the_screens_call_it(advice):
    words = " ".join((advice.title, advice.what_happened) + advice.what_to_do)
    assert "board's Wi-Fi password shown on this screen" in words
    assert "SLIC ATTENDANCE-3D31" in words and "board's own Wi-Fi" in words
    assert "hotspot" not in words.lower() and "shown by this app" not in words


def test_one_name_for_each_thing():
    # The form's "Device number" and "Machine address"; "the board's own Wi-Fi".
    stale = re.compile(r"device ID|Preferred IP|fixed IP|hotspot|under Advanced(?! settings)")
    for advice in all_texts():
        for words in (advice.title, advice.what_happened, *advice.what_to_do):
            assert not stale.search(words), (advice.code, words)
    taken = texts.device_id_in_use("1", "LF-ZONE-BLD9-04", "2")
    assert "Device number 1" in taken.what_happened
    assert texts.invalid_field("zkt_preferred_ip").title == "Please check the machine address"


def test_light_hints_use_the_light_guides_words():
    from sl_installer.ui import leds  # plain Python: no Qt

    titles = {rhythm.key: rhythm.title.lower() for rhythm in leds.RHYTHMS}
    for state in texts.LED_GUIDE:
        guide = leds.BY_KEY[state.key]
        assert state.colour == guide.colour.lower(), state.key
        assert state.rhythm == titles[guide.rhythm], state.key
    booting = texts.light_hint(next(s for s in texts.LED_GUIDE if s.key == "BOOTING"))
    assert booting.title == "The light should be white (slowly brighter and dimmer)"
    assert "15 minutes" in " ".join(booting.what_to_do)
    off = texts.light_hint(next(s for s in texts.LED_GUIDE if s.key == "OFF"))
    assert off.title == "The light should be off"


def test_rate_limit_waits_the_minutes_head_office_asked_for():
    assert "Wait 7 minutes." in texts.rate_limited(7).what_to_do
    assert texts.RATE_LIMITED.what_to_do[0] == "Wait one minute."


APP = Path(__file__).resolve().parents[2]


def _text_mode_open(node: ast.Call) -> bool:
    """open(path[, mode]) or path.open(mode) in text mode; serial/Qt .open() are not files."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        mode = node.args[1] if len(node.args) > 1 else None
    elif isinstance(func, ast.Attribute) and func.attr == "open" and node.args and not (
            isinstance(func.value, ast.Name) and func.value.id in ("os", "gzip")):
        mode = node.args[0]
    else:
        return False
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return True
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
        and "b" not in mode.value


def test_text_files_are_read_and_written_as_utf8():
    # Windows reads and writes text in its own code page (cp1252) unless told otherwise.
    found = []
    for root in (APP / "src", APP / "tests", APP / "packaging"):
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or any(
                        keyword.arg == "encoding" for keyword in node.keywords):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name in ("read_text", "write_text") or _text_mode_open(node):
                    found.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not found, found


def test_field_labels_cover_the_payload():
    from sl_installer.core.add_client import configuration_payload
    payload = configuration_payload(support.zkt_config(family=Family.HIKVISION, zkt=None,
                                                       hik=HikSettings("192.168.1.2", "p")))
    for key in payload:
        if key not in ("firmware_family",):
            assert texts.invalid_field(key).title != "Please check the details", key


def test_api_dataclasses_are_unchanged_here():
    # The engine relies on these fields; api.py is the shared contract.
    assert [f.name for f in fields(BootReport)] == [
        "outcome", "zone_id_seen", "device_id_seen", "app_version_seen", "events", "advice"]
