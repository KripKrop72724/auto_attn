"""The vendored envelope decrypt against the repo's own encryptor."""
from __future__ import annotations

import pytest

from sl_installer.core.envelope import EnvelopeError, OneTimeKey, open_package

from . import support
from .support import MAC

REQUEST = "sli-0123456789abcdef"
NVS = bytes(range(256)) * 96
KEY = bytes(range(32))


def package(key: OneTimeKey, **changes):
    values = dict(family="zkt", zone_id="ZONE-X-01", device_id="1", nvs=NVS, hmac_key=KEY)
    values.update(changes)
    return support.make_package(REQUEST, MAC, key.public_key_b64, **values)


def test_opens_settings_and_key():
    key = OneTimeKey()
    opened = open_package(package(key), key, request_id=REQUEST, mac=MAC, want_hmac_key=True)
    assert opened.nvs == NVS and opened.hmac_key == KEY
    assert opened.aad["zone_id"] == "ZONE-X-01"
    assert "bytes" in repr(opened) and str(KEY) not in repr(opened)


def test_key_is_not_decrypted_unless_needed():
    key = OneTimeKey()
    opened = open_package(package(key), key, request_id=REQUEST, mac=MAC, want_hmac_key=False)
    assert opened.hmac_key is None


def test_mac_is_compared_case_insensitively():
    key = OneTimeKey()
    opened = open_package(package(key), key, request_id=REQUEST, mac=MAC.upper(),
                          want_hmac_key=True)
    assert opened.nvs == NVS


@pytest.mark.parametrize("tamper, message", [
    (lambda m: m["aad"].update(target_mac="aa:bb:cc:dd:ee:ff"), "MAC mismatch"),
    (lambda m: m["aad"].update(request_id="sli-other-request"), "request ID"),
    (lambda m: m.update(ciphertext_sha256="0" * 64), "ciphertext hash"),
    (lambda m: m["aad"].update(nvs_offset="0x9000"), "0x11000"),
    (lambda m: m["envelope"].update(algorithm="X25519-OTHER"), "algorithm"),
    (lambda m: m["hmac_key"]["aad"].update(purpose="something-else"), "purpose"),
    (lambda m: m["hmac_key"].update(ciphertext_sha256="1" * 64), "HMAC ciphertext"),
])
def test_every_check(tamper, message):
    key = OneTimeKey()
    with pytest.raises(EnvelopeError, match=message):
        open_package(package(key, tamper=tamper), key, request_id=REQUEST, mac=MAC,
                     want_hmac_key=True)


def test_aad_change_breaks_decryption():
    key = OneTimeKey()
    with pytest.raises(EnvelopeError):
        open_package(package(key, tamper=lambda m: m["aad"].update(zone_id="ZONE-Y-01")), key,
                     request_id=REQUEST, mac=MAC, want_hmac_key=False)


def test_package_for_another_key_does_not_open():
    with pytest.raises(EnvelopeError, match="does not decrypt"):
        open_package(package(OneTimeKey()), OneTimeKey(), request_id=REQUEST, mac=MAC,
                     want_hmac_key=False)


def test_wrong_sizes_are_refused():
    key = OneTimeKey()
    with pytest.raises(EnvelopeError):
        open_package(package(key, nvs=b"\0" * 100), key, request_id=REQUEST, mac=MAC,
                     want_hmac_key=False)
    key = OneTimeKey()
    with pytest.raises(EnvelopeError, match="wrong size"):
        open_package(package(key, hmac_key=b"\1" * 16), key, request_id=REQUEST, mac=MAC,
                     want_hmac_key=True)


def test_missing_key_when_needed():
    key = OneTimeKey()
    with pytest.raises(EnvelopeError, match="no encrypted security key"):
        open_package(package(key, hmac_key=None), key, request_id=REQUEST, mac=MAC,
                     want_hmac_key=True)


def test_discarded_key_cannot_decrypt():
    key = OneTimeKey()
    sealed = package(key)
    key.discard()
    with pytest.raises(EnvelopeError, match="discarded"):
        open_package(sealed, key, request_id=REQUEST, mac=MAC, want_hmac_key=False)
