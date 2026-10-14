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
