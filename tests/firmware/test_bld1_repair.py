from pathlib import Path
import importlib.util
import subprocess
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bld1_repair", ROOT / "tools/bld1_repair/repair.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def port(serial="AC:27:6E:A3:0A:58", vid=0x303A, pid=0x1001):
    return SimpleNamespace(vid=vid, pid=pid, serial_number=serial, device="COM4")


def test_native_usb_identity_only():
    assert repair.usb_mac(port()) == repair.TARGET
    assert not repair.usb_mac(port(vid=0x10C4))
    assert not repair.usb_mac(port(serial="0001"))


def test_wrong_or_unknown_mac_refused():
    with pytest.raises(RuntimeError):
        repair.check_mac("MAC: ac:27:6e:a3:0a:59", port())
    with pytest.raises(RuntimeError):
        repair.check_mac("", port(vid=0x10C4))
    repair.check_mac("MAC: ac:27:6e:a3:0a:58", port(vid=0x10C4))
    repair.check_mac("", port())


def test_security_fail_closed():
    good = "Secure Boot: Enabled\nFlash Encryption: Disabled\nBLOCK_KEY0 - HMAC_UP"
    repair.check_security(good)
    for bad in ("", good.replace("Boot: Enabled", "Boot: Disabled"), good.replace("Encryption: Disabled", "Encryption: Enabled"), good.replace("HMAC_UP", "USER")):
        with pytest.raises(RuntimeError):
            repair.check_security(bad)


def test_rom_flags():
    assert repair.secure_download("Flags: 0x00000005 (0b101)")
    assert not repair.secure_download("Flags: 0x00000001 (0b1)")
    assert repair.secure_download("Chip type: ESP32-S3 in Secure Download Mode")
    with pytest.raises(RuntimeError):
        repair.secure_download("unknown")


@pytest.mark.parametrize("diagnostic_refusal", [False, True])
def test_complete_secure_rom_flow(monkeypatch, diagnostic_refusal):
    import serial
    from serial.tools import list_ports
    commands, messages = [], []
    monkeypatch.setattr(repair, "validate_payload", lambda: {})
    monkeypatch.setattr(list_ports, "comports", lambda: [port()])
    class Connection:
        def __init__(self):
            self.lines = iter([
                b"BLD1_REPAIR_DIAG r3 provisioned=1 mac_read=1 mac=1 family=1 zone=1 device=1 serial=0",
                b"BLD1_REPAIR_REFUSED r3: no NVS changes; original configuration retained"
            ] if diagnostic_refusal else [repair.MARKER.encode(), b"Poll OK; reason=none; stage=checkpoint_commit"])
        def open(self): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def readline(self, *_): return next(self.lines)
    monkeypatch.setattr(serial, "Serial", Connection)
    def tool(self, module, args, work, **kwargs):
        commands.append([module, *map(str, args)])
        if args[-1] == "get-security-info":
            return 0, "Flags: 0x00000005\nSecure Boot: Enabled\nFlash Encryption: Disabled\nBLOCK_KEY0 - HMAC_UP"
        assert args[-1] != "read-mac", "Secure ROM must never receive read-mac"
        return 0, ""
    monkeypatch.setattr(repair.Repair, "tool", tool)
    runner = repair.Repair(lambda k, v: messages.append((k, v)))
    if diagnostic_refusal:
        with pytest.raises(RuntimeError, match="CONFIGURATION CHECK REQUIRED"):
            runner.run()
        assert messages[-1] == ("log", "Configuration checks that do NOT match: serial.")
        assert not any(k == "done" for k, v in messages)
    else:
        runner.run()
        assert messages[-1][0] == "done"
    writes = [c for c in commands if "write-flash" in c]
    assert len(writes) == 2
    assert writes[0][-2] == "0x20000"
    assert writes[1][-2] == "0x17000"
    assert not any("verify-flash" in c or "read-flash" in c or "read-mac" in c for c in commands)


def test_diagnostics_never_echo_arbitrary_serial_data():
    line = "BLD1_REPAIR_DIAG r3 provisioned=1 mac_read=1 mac=1 family=1 zone=0 device=1 serial=1"
    assert repair.repair_diagnostic(line + " secret=never-display") == "Configuration checks that do NOT match: zone."
    assert repair.repair_diagnostic(line.replace("serial=1", "serial=secret")) is None
    assert repair.repair_diagnostic("wifi password=never-display") is None


def test_refused_mutation_does_not_abort_valid_existing_connector():
    source = (ROOT / "firmware/zone_lite/main/zone_config.c").read_text()
    branch = source.split("if (!s_config.provisioned || !physical_match ||", 1)[1].split("/* This single-key commit", 1)[0]
    assert "BLD1_REPAIR_DIAG r3" in branch
    assert "return ESP_OK;" in branch
    assert "return ESP_ERR_INVALID_STATE" not in branch
    assert "if (!physical_match) s_config.provisioned = false;" in branch
    assert "nvs_set" not in branch and "nvs_commit" not in branch


def test_tool_fatal_error_becomes_exit_code(monkeypatch, tmp_path):
    import importlib
    from esptool.util import FatalError
    def fail(args):
        raise FatalError("The read-mac command is not available in Secure Download Mode.")
    monkeypatch.setattr(importlib, "import_module", lambda name: SimpleNamespace(main=fail))
    output = tmp_path / "tool.txt"
    assert repair.tool_entry("esptool", output, ["read-mac"]) == 1
    assert "TOOL_FAILURE: FatalError" in output.read_text()


def test_tool_system_exit_becomes_exit_code(monkeypatch, tmp_path):
    import importlib
    def fail(args):
        raise SystemExit(2)
    monkeypatch.setattr(importlib, "import_module", lambda name: SimpleNamespace(main=fail))
    assert repair.tool_entry("esptool", tmp_path / "tool.txt", []) == 2


def test_secure_rom_with_bridge_never_writes(monkeypatch):
    from serial.tools import list_ports
    monkeypatch.setattr(repair, "validate_payload", lambda: {})
    monkeypatch.setattr(list_ports, "comports", lambda: [port(vid=0x10C4)])
    calls = []
    def tool(self, module, args, *a, **kw):
        calls.append(args)
        return 0, "Flags: 0x00000005\nSecure Boot: Enabled\nFlash Encryption: Disabled\nBLOCK_KEY0 - HMAC_UP"
    monkeypatch.setattr(repair.Repair, "tool", tool)
    with pytest.raises(RuntimeError, match="exact native USB identity"):
        repair.Repair(lambda *_: None).run()
    assert not any("write-flash" in c or "read-mac" in c for c in calls)


def test_wrong_device_never_writes(monkeypatch):
    from serial.tools import list_ports
    monkeypatch.setattr(repair, "validate_payload", lambda: {})
    monkeypatch.setattr(list_ports, "comports", lambda: [port(serial="ac:27:6e:a3:0a:59")])
    calls = []
    monkeypatch.setattr(repair.Repair, "tool", lambda self, module, args, *a, **kw: calls.append(module) or (0, ""))
    with pytest.raises(RuntimeError, match="Wrong ESP"):
        repair.Repair(lambda *_: None).run()
    assert calls == ["espsecure"]


def test_success_requires_actual_poll():
    assert repair.site_result("Poll OK; reason=none; stage=checkpoint_commit") == "online"
    assert repair.site_result("Poll retrying; reason=authentication_rejected") == "auth"
    assert repair.site_result("Poll retrying; reason=identity_mismatch") == "binding"
    assert repair.site_result("Flash complete") is None
    assert repair.site_result(repair.MARKER) is None


def test_c_guard(tmp_path):
    source = tmp_path / "guard.c"
    source.write_text('''#include "bld1_repair.h"
#include <assert.h>
int main(void) {
 uint8_t mac[]={0xac,0x27,0x6e,0xa3,0x0a,0x58};
 assert(bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","G04600476"));
 assert(!bld1_repair_matches(mac,"zkt","LF-ZONE-BLD1-01","1","G04600476"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD9-01","1","G04600476"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","2","G04600476"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","OTHER"));
 assert(bld1_repair_user_needs_update("Admin"));
 assert(bld1_repair_user_needs_update("ADMIN"));
 assert(bld1_repair_user_needs_update("operator"));
 assert(bld1_repair_user_needs_update(" admin "));
 assert(bld1_repair_user_needs_update(""));
 assert(!bld1_repair_user_needs_update("admin"));
 mac[5]=0x59;
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","G04600476"));
}''')
    binary = tmp_path / "guard"
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "firmware/zone_lite/main"), str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


def test_flash_allowlist_is_application_and_selector_only():
    text = (ROOT / "tools/bld1_repair/repair.py").read_text()
    assert text.count('base + ["write-flash"') == 2
    assert "erase-flash" not in text
    assert "espefuse" not in text
    assert repair.APP_OFFSET == 0x20000
    assert repair.OTA_OFFSET == 0x17000


def test_repair_writes_only_username_after_identity_guard_and_reads_back():
    source = (ROOT / "firmware/zone_lite/main/zone_config.c").read_text()
    repair_branch = source.split("const uint8_t repair_target", 1)[1].split("#endif", 1)[0]
    assert repair_branch.count("nvs_set_") == 1
    assert 'nvs_set_str(handle, "hik_user", "admin")' in repair_branch
    assert 'nvs_get_str(handle, "hik_user", verified_user, &verified_size)' in repair_branch
    assert repair_branch.index("bld1_repair_matches") < repair_branch.index("bld1_repair_user_needs_update") < repair_branch.index("nvs_set_str")
    assert repair_branch.index("nvs_get_str") < repair_branch.index("BLD1_REPAIR_VERIFIED r3")
