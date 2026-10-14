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


def test_complete_secure_rom_flow(monkeypatch):
    import serial
    from serial.tools import list_ports
    commands, messages = [], []
    monkeypatch.setattr(repair, "validate_payload", lambda: {})
    monkeypatch.setattr(list_ports, "comports", lambda: [port()])
    class Connection:
        def __init__(self):
            self.lines = iter([repair.MARKER.encode(), b"Poll OK; reason=none; stage=checkpoint_commit"])
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
    repair.Repair(lambda k, v: messages.append((k, v))).run()
    writes = [c for c in commands if "write-flash" in c]
    assert len(writes) == 2
    assert writes[0][-2] == "0x20000"
    assert writes[1][-2] == "0x17000"
    assert not any("verify-flash" in c or "read-flash" in c or "read-mac" in c for c in commands)
    assert messages[-1][0] == "done"


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
 assert(bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","G04600476","Admin"));
 assert(bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","G04600476","admin"));
 assert(!bld1_repair_matches(mac,"zkt","LF-ZONE-BLD1-01","1","G04600476","Admin"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD9-01","1","G04600476","Admin"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","2","G04600476","Admin"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","OTHER","Admin"));
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","G04600476","OTHER"));
 mac[5]=0x59;
 assert(!bld1_repair_matches(mac,"hikvision","LF-ZONE-BLD1-01","1","G04600476","Admin"));
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
