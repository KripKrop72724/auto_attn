"""The ADD client against a fake ADD over httpx.MockTransport."""
from __future__ import annotations

import base64
import json
import logging
import ssl
from dataclasses import replace

import httpx
import pytest

from sl_installer.core import texts
from sl_installer.core.add_client import (AddClient, LoginThrottle, configuration_payload,
                                          tls_context)
from sl_installer.core.api import (CancelToken, Family, HikSettings, InstallerError, InstallMode,
                                   PackageRequest)

from . import support
from .support import MAC, FakeClock

PASSWORD = "test-admin-pass"
API = "/api/v1/installer"


class FakeADD:
    def __init__(self, bundle=None):
        self.password = PASSWORD
        self.tokens = set()
        self.csrf = "csrf-1"
        self.requests = []
        self.expire_next = False
        self.rotate_csrf = False
        self.responses = {}
        self.bundle = bundle

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/api/v1/auth/login":
            body = json.loads(request.content)
            if (method, path) in self.responses:
                return self.responses[(method, path)]
            if body["password"] != self.password:
                return httpx.Response(401, json={"detail": "Invalid username or password."})
            token = f"tok{len(self.tokens)}"
            self.tokens.add(token)
            return httpx.Response(200, json={"ok": True, "username": body["username"],
                                             "csrf_token": self.csrf},
                                  headers={"set-cookie": f"add_admin={token}; Path=/; HttpOnly; "
                                                         "Secure; SameSite=strict"})
        cookie = request.headers.get("cookie", "")
        if self.expire_next or not any(f"add_admin={t}" in cookie for t in self.tokens):
            self.expire_next = False
            self.tokens.clear()
            return httpx.Response(401, json={"detail": "Session expired."})
        if path == "/api/v1/auth/session":
            return httpx.Response(200, json={"authenticated": True, "username": "admin",
                                             "csrf_token": self.csrf})
        if method == "POST":
            if self.rotate_csrf:
                self.rotate_csrf = False
                self.csrf = "csrf-2"
            if request.headers.get("x-csrf-token") != self.csrf:
                # installer_api.py's shape for ADD's shared CSRF check.
                return httpx.Response(403, json={
                    "code": "CSRF_INVALID",
                    "message": "Your sign-in is out of date. Please sign in again.",
                    "detail": "Invalid CSRF token."})
        if (method, path) in self.responses:
            return self.responses[(method, path)]
        if path == "/api/v1/auth/logout":
            return httpx.Response(200, json={"ok": True})
        if path == f"{API}/capabilities":
            return httpx.Response(200, json={"enabled": True, "provisioner_ready": True,
                                             "min_app_version": "1.0.0",
                                             "server_time": "2026-11-01T10:00:00Z",
                                             "bundle_count": 2})
        if path == f"{API}/bundles":
            items = [
                {"bundle_id": "hikvision-3.1.1-dc7d9c3-77620195", "family": "hikvision",
                 "label": "HIK 3.1.1", "version": "3.1.1", "recommended": True,
                 "release_state": "FIELD_BASELINE", "notes": "", "download_bytes": 9,
                 "manifest_sha256": "a" * 64},
                {"bundle_id": "zkt-2.8.0-00000000", "family": "zkt", "label": "ZKT 2.8.0",
                 "version": "2.8.0", "recommended": False, "release_state": "HIL_ONLY"},
                {"bundle_id": "zkt-2.7.8-b8d0ca7e", "family": "zkt", "label": "ZKT 2.7.8",
                 "version": "2.7.8", "recommended": True, "release_state": "AVAILABLE",
                 "download_bytes": 5000000, "manifest_sha256": "b" * 64},
            ]
            if self.bundle:
                from sl_installer.core.bundles import manifest_sha256
                items.append({"bundle_id": self.bundle.bundle_id, "family": "zkt",
                              "label": "ZKT 2.9.1", "version": "2.9.1",
                              "release_state": "AVAILABLE",
                              "manifest_sha256": manifest_sha256(self.bundle.manifest)})
            return httpx.Response(200, json={"bundles": items})
        if self.bundle and path == f"{API}/bundles/{self.bundle.bundle_id}/manifest":
            return httpx.Response(200, json={"manifest": self.bundle.manifest,
                                             "signature": self.bundle.signature})
        if self.bundle and path.startswith(f"{API}/bundles/{self.bundle.bundle_id}/files/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200, content=self.bundle.files[name])
        if path.startswith(f"{API}/devices/"):
            return httpx.Response(200, json={
                "known": True, "mac": MAC, "zone_id": "LF-ZONE-GUJRAT-01",
                "zone_name": "LF-ZONE-GUJRAT-01", "device_id": 1, "family": "zkt",
                "firmware_version": "2.7.8", "online": False, "ever_online": True,
                "last_seen_at": "2026-10-31T09:00:00Z", "public_name": "Gujrat · Device 1",
                "group": "Life", "status_text": "Offline since 09:00", "hik_source_epoch": None})
        if path == f"{API}/zones":
            return httpx.Response(200, json={"zones": [
                {"zone_id": "LF-ZONE-GUJRAT-01", "zone_name": "LF-ZONE-GUJRAT-01",
                 "device_ids": ["1", "2"], "families": ["zkt"]}]})
        if path == f"{API}/packages":
            body = json.loads(request.content)
            return httpx.Response(201, json={
                "request_id": body["request_id"],
                "provisioning_manifest": {"aad": {"request_id": body["request_id"]}},
                "provisioning_ciphertext_b64": base64.b64encode(b"nvs").decode(),
                "hmac_ciphertext_b64": base64.b64encode(b"key").decode()})
        if path == f"{API}/events":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "Not Found"})

    def count(self, path):
        return sum(1 for r in self.requests if r.url.path == path)


@pytest.fixture
def add():
    return FakeADD()


def client(add, tmp_path, store=None, clock=None, throttle_file=None):
    clock = clock or FakeClock()
    throttle = LoginThrottle(clock, throttle_file)
    return AddClient("https://autoattn.test", app_version="1.0.0", store=store, clock=clock,
                     throttle=throttle, transport=httpx.MockTransport(add))


def signed_in(add, tmp_path, **kwargs):
    c = client(add, tmp_path, **kwargs)
    c.login("StateHealthAdmin", PASSWORD)
    return c


def request(config=None):
    return PackageRequest("sli-0123456789abcdef", MAC, InstallMode.NEW_BOARD,
                          config or support.zkt_config(), "cmVjaXBpZW50", "1.0.0")


def test_login_and_capabilities(add, tmp_path):
    c = signed_in(add, tmp_path)
    assert c.signed_in_user == "StateHealthAdmin"
    caps = c.capabilities()
    assert caps.enabled and caps.provisioner_ready and caps.min_app_version == "1.0.0"
    assert caps.server_time == "2026-11-01T10:00:00Z"
    assert "add_admin=tok0" in add.requests[-1].headers["cookie"]


def test_not_signed_in_never_calls_the_server(add, tmp_path):
    with pytest.raises(InstallerError) as caught:
        client(add, tmp_path).capabilities()
    assert caught.value.advice is texts.SESSION_EXPIRED and add.requests == []


def test_package_request_carries_csrf_password_and_payload(add, tmp_path):
    c = signed_in(add, tmp_path)
    package = c.create_package(request())
    sent = add.requests[-1]
    body = json.loads(sent.content)
    assert sent.headers["x-csrf-token"] == "csrf-1"
    assert body["password"] == PASSWORD
    assert body["mode"] == "new_board" and body["mac"] == MAC
    assert body["recipient_public_key"] == "cmVjaXBpZW50" and body["app_version"] == "1.0.0"
    assert body["configuration"] == {
        "firmware_family": "zkt", "zone_id": "LF-ZONE-GUJRAT-01",
        "zone_name": "LF-ZONE-GUJRAT-01", "device_id": "1", "wifi_ssid": "RE7",
        "wifi_password": "test-wifi-pass", "zkt_port": 4370, "zkt_comm_key": 1979,
        "zkt_preferred_ip": "0.0.0.0"}
    assert package.provisioning_ciphertext == b"nvs" and package.hmac_ciphertext == b"key"


def test_hikvision_payload_never_sends_a_comm_key():
    config = support.zkt_config(family=Family.HIKVISION, zkt=None, hik=HikSettings(
        host="192.168.1.2", password="test-hik-pass", expected_serial="S1",
        profile="p", source_epoch="e"))
    payload = configuration_payload(config)
    assert payload["zkt_comm_key"] == 0 and payload["hik_transport"] == "http_digest"
    assert payload["hik_port"] == 80 and payload["hik_username"] == "admin"
    assert payload["hik_password"] == "test-hik-pass" and payload["hik_ca_pem"] == ""


def test_expired_session_signs_in_again_silently(add, tmp_path):
    c = signed_in(add, tmp_path)
    add.expire_next = True
    assert c.device(MAC).known
    assert add.count("/api/v1/auth/login") == 2


def test_changed_password_ends_the_session(add, tmp_path):
    c = signed_in(add, tmp_path)
    add.expire_next = True
    add.password = "someone-changed-it"
    with pytest.raises(InstallerError) as caught:
        c.zones()
    assert caught.value.advice is texts.SESSION_EXPIRED and c.signed_in_user is None


def test_csrf_token_is_refreshed_once(add, tmp_path):
    c = signed_in(add, tmp_path)
    add.rotate_csrf = True
    c.create_package(request())
    assert add.count("/api/v1/auth/session") == 1
    assert add.requests[-1].headers["x-csrf-token"] == "csrf-2"


def test_three_wrong_passwords_pause_sign_in_for_ten_minutes(add, tmp_path):
    clock = FakeClock()
    state = tmp_path / "pause.json"
    c = client(add, tmp_path, clock=clock, throttle_file=state)
    for attempt in range(2):
        with pytest.raises(InstallerError) as caught:
            c.login("StateHealthAdmin", "wrong-password-1")
        assert caught.value.advice is texts.LOGIN_FAILED
    with pytest.raises(InstallerError) as caught:
        c.login("StateHealthAdmin", "wrong-password-1")
    assert caught.value.advice.code == "LOGIN_PAUSED"
    assert "10 minutes" in caught.value.advice.what_happened
    assert "15 minutes" in " ".join(caught.value.advice.what_to_do)
    sent = add.count("/api/v1/auth/login")
    # Even the right password waits, and nothing reaches ADD meanwhile.
    with pytest.raises(InstallerError):
        c.login("StateHealthAdmin", PASSWORD)
    assert add.count("/api/v1/auth/login") == sent
    # The pause survives a restart of the app.
    again = client(add, tmp_path, clock=clock, throttle_file=state)
    with pytest.raises(InstallerError):
        again.login("StateHealthAdmin", PASSWORD)
    clock.advance(601)
    assert again.login("StateHealthAdmin", PASSWORD) == "StateHealthAdmin"


def test_failures_spread_over_time_do_not_pause(add, tmp_path):
    clock = FakeClock()
    c = client(add, tmp_path, clock=clock)
    for _ in range(4):
        with pytest.raises(InstallerError) as caught:
            c.login("StateHealthAdmin", "wrong-password-1")
        assert caught.value.advice is texts.LOGIN_FAILED
        clock.advance(400)


def test_server_lock_is_explained_and_respected(add, tmp_path):
    clock = FakeClock()
    add.responses[("POST", "/api/v1/auth/login")] = httpx.Response(
        429, json={"detail": "Too many login attempts. Try again later."})
    c = client(add, tmp_path, clock=clock)
    with pytest.raises(InstallerError) as caught:
        c.login("StateHealthAdmin", PASSWORD)
    assert caught.value.advice is texts.ACCOUNT_LOCKED
    clock.advance(600)
    with pytest.raises(InstallerError) as caught:
        c.login("StateHealthAdmin", PASSWORD)
    assert caught.value.advice.code == "LOGIN_PAUSED"


@pytest.mark.parametrize("response, code", [
    (httpx.Response(409, json={"code": "FAMILY_MISMATCH", "message": "Other family."}),
     "FAMILY_MISMATCH"),
    (httpx.Response(409, json={"detail": {"code": "DEVICE_ID_IN_USE", "suggested_device_id": "3",
                                          "message": "Taken."}}), "DEVICE_ID_IN_USE"),
    (httpx.Response(422, json={"code": "INVALID_FIELD", "field": "zone_name",
                               "message": "The zone name is too long."}),
     "INVALID_FIELD:zone_name"),
    (httpx.Response(502, json={"code": "PROVISIONER_UNAVAILABLE"}), "PROVISIONER_UNAVAILABLE"),
    (httpx.Response(426, json={"detail": "Update required."}), "APP_TOO_OLD"),
    (httpx.Response(429, json={"detail": "Slow down."}), "RATE_LIMITED"),
    (httpx.Response(500, text="boom"), "SERVER_ERROR"),
    (httpx.Response(403, json={"detail": "Password confirmation failed."}), "STEP_UP_FAILED"),
    (httpx.Response(403, json={"code": "PASSWORD_REJECTED", "field": "password",
                               "message": "The password was not accepted.",
                               "detail": "Password confirmation failed."}), "STEP_UP_FAILED"),
    (httpx.Response(409, json={"code": "BUNDLE_FAMILY_MISMATCH", "field": "bundle_id",
                               "message": "Other machine.", "detail": {
                                   "code": "BUNDLE_FAMILY_MISMATCH", "field": "bundle_id"}}),
     "BUNDLE_WRONG_FAMILY"),
    (httpx.Response(409, json={"code": "BUNDLE_UNAVAILABLE", "field": "bundle_id",
                               "message": "Gone."}), "NOT_FOUND"),
    (httpx.Response(409, json={"code": "FAMILY_MISMATCH", "field": "firmware_family",
                               "message": "Other kind.", "known_family": "hikvision",
                               "detail": {"code": "FAMILY_MISMATCH",
                                          "known_family": "hikvision"}}), "FAMILY_MISMATCH"),
])
def test_package_errors(add, tmp_path, response, code):
    c = signed_in(add, tmp_path)
    add.responses[("POST", f"{API}/packages")] = response
    with pytest.raises(InstallerError) as caught:
        c.create_package(request())
    advice = caught.value.advice
    assert advice.code == code
    if code == "FAMILY_MISMATCH":
        assert "Hikvision" in advice.what_happened
    if code == "DEVICE_ID_IN_USE":
        assert "Use device number 3 instead, under Advanced settings." in advice.what_to_do
    if code.startswith("INVALID_FIELD"):
        assert advice.what_happened == "The zone name is too long."


@pytest.mark.parametrize("error, advice", [
    (httpx.ConnectError("[Errno -2] Name or service not known"), texts.NO_INTERNET),
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
     texts.SECURE_CONNECTION_FAILED),
    (httpx.ConnectError("[Errno 111] Connection refused"), texts.SERVER_UNREACHABLE),
    (httpx.ReadTimeout("timed out"), texts.SERVER_UNREACHABLE),
])
def test_network_errors(tmp_path, error, advice):
    def broken(request):
        raise error
    c = AddClient("https://autoattn.test", app_version="1.0.0", store=None,
                  transport=httpx.MockTransport(broken))
    with pytest.raises(InstallerError) as caught:
        c.login("StateHealthAdmin", PASSWORD)
    assert caught.value.advice is advice


def test_bundles_are_filtered_and_sorted(add, tmp_path):
    c = signed_in(add, tmp_path)
    all_bundles = c.bundles()
    assert [b.bundle_id for b in all_bundles] == ["hikvision-3.1.1-dc7d9c3-77620195",
                                                  "zkt-2.7.8-b8d0ca7e"]
    zkt = c.bundles(Family.ZKT)
    assert [b.label for b in zkt] == ["ZKT 2.7.8"] and zkt[0].download_bytes == 5000000


def test_device_and_zones(add, tmp_path):
    c = signed_in(add, tmp_path)
    device = c.device(MAC)
    assert device.known and device.family is Family.ZKT and device.device_id == "1"
    assert device.ever_online and device.online is False and device.group == "Life"
    assert add.requests[-1].url.path == f"{API}/devices/{MAC}"
    zone = c.zones()[0]
    assert zone.device_ids == ("1", "2") and zone.families == (Family.ZKT,)


def test_events_are_best_effort_and_never_carry_secrets(add, tmp_path):
    c = signed_in(add, tmp_path)
    c.report("sli-0123456789abcdef", MAC, "STARTED", {"mode": "new_board",
                                                       "wifi_password": "x" * 9,
                                                       "note": "password=hunter2-hunter2"})
    body = json.loads(add.requests[-1].content)
    assert body["kind"] == "STARTED" and "wifi_password" not in body["detail"]
    assert "hunter2" not in json.dumps(body)
    add.responses[("POST", f"{API}/events")] = httpx.Response(500)
    c.report("sli-0123456789abcdef", MAC, "FAILED", {})  # must not raise


def test_failure_events_tell_head_office_the_key_is_locked(add, tmp_path):
    c = signed_in(add, tmp_path)
    c.report("sli-0123456789abcdef", MAC, "FAILED", {
        "mode": "new_board", "step": "lock_key", "code": "BOARD_SWAPPED", "lock_burned": True,
        "hmac_key": "00" * 32, "comm_key": 1979, "zkt_comm_key": 1979, "key": "k" * 9,
        "csrf_token": "c" * 9, "hik_password": "p" * 9, "recipient_public_key": "r" * 9,
        "provisioning_ciphertext_b64": "b" * 9})
    detail = json.loads(add.requests[-1].content)["detail"]
    assert detail == {"mode": "new_board", "step": "lock_key", "code": "BOARD_SWAPPED",
                      "lock_burned": True}


@pytest.mark.parametrize("call", ["login", "device", "zones"])
def test_head_office_down_behind_its_gateway_is_not_a_settings_problem(add, tmp_path, call):
    # During a deploy the gateway answers 502 with no installer code, on every route.
    c = signed_in(add, tmp_path)
    for path in ("/api/v1/auth/login", f"{API}/devices/{MAC}", f"{API}/zones"):
        add.responses[("POST" if "login" in path else "GET", path)] = httpx.Response(502)
    with pytest.raises(InstallerError) as caught:
        {"login": lambda: c.login("StateHealthAdmin", PASSWORD), "device": lambda: c.device(MAC),
         "zones": c.zones}[call]()
    assert caught.value.advice is texts.SERVER_UNREACHABLE


@pytest.mark.parametrize("response, code", [
    (httpx.Response(502, json={"code": "PROVISIONER_UNAVAILABLE", "message": "Wait."}),
     "PROVISIONER_UNAVAILABLE"),
    (httpx.Response(503, text="Service Unavailable"), "SERVER_UNREACHABLE"),
    (httpx.Response(504), "SERVER_UNREACHABLE"),
])
def test_only_the_provisioners_own_code_blames_the_settings_service(add, tmp_path, response,
                                                                    code):
    c = signed_in(add, tmp_path)
    add.responses[("POST", f"{API}/packages")] = response
    with pytest.raises(InstallerError) as caught:
        c.create_package(request())
    assert caught.value.advice.code == code


@pytest.mark.parametrize("headers, words", [
    ({"Retry-After": "420"}, "Wait 7 minutes."),
    ({"Retry-After": "61"}, "Wait 2 minutes."),
    ({"Retry-After": "30"}, "Wait one minute."),
    ({}, "Wait one minute."),
    ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, "Wait one minute."),
    ({"Retry-After": "86400"}, "Wait 60 minutes."),
])
def test_rate_limit_says_how_long_head_office_asked_to_wait(add, tmp_path, headers, words):
    c = signed_in(add, tmp_path)
    add.responses[("POST", f"{API}/packages")] = httpx.Response(
        429, json={"code": "RATE_LIMITED", "message": "Too many."}, headers=headers)
    with pytest.raises(InstallerError) as caught:
        c.create_package(request())
    advice = caught.value.advice
    assert advice.code == "RATE_LIMITED" and words in advice.what_to_do


def test_board_head_office_cannot_change_is_explained_and_not_retried(add, tmp_path):
    c = signed_in(add, tmp_path)
    add.responses[("POST", f"{API}/packages")] = httpx.Response(409, json={
        "code": "SETTINGS_CHANGE_NOT_SUPPORTED", "message": "Call head office.",
        "detail": {"code": "SETTINGS_CHANGE_NOT_SUPPORTED"}})
    with pytest.raises(InstallerError) as caught:
        c.create_package(replace(request(), mode=InstallMode.CHANGE_SETTINGS))
    assert caught.value.advice is texts.SETTINGS_CHANGE_NOT_SUPPORTED
    assert not caught.value.advice.can_retry


@pytest.mark.parametrize("fields, allowed, reason", [
    ({}, True, None),
    ({"settings_change_allowed": True, "settings_change_reason": None}, True, None),
    ({"settings_change_allowed": False,
      "settings_change_reason": "Set up before this app. Call head office. "},
     False, "Set up before this app. Call head office."),
    ({"settings_change_allowed": "no"}, False, None),
])
def test_device_record_says_whether_settings_may_change(add, tmp_path, fields, allowed, reason):
    record = {"known": True, "mac": MAC, "zone_id": "LF-ZONE-GUJRAT-01", "family": "zkt",
              **fields}
    add.responses[("GET", f"{API}/devices/{MAC}")] = httpx.Response(200, json=record)
    device = signed_in(add, tmp_path).device(MAC)
    assert (device.settings_change_allowed, device.settings_change_reason) == (allowed, reason)


@pytest.mark.parametrize("mode, bundle_id, sent", [
    (InstallMode.CHANGE_SETTINGS, "", None),
    (InstallMode.CHANGE_SETTINGS, "zkt-2.9.1-e89380a1", "zkt-2.9.1-e89380a1"),
    (InstallMode.NEW_BOARD, "zkt-2.9.1-e89380a1", "zkt-2.9.1-e89380a1"),
    (InstallMode.RESUME, "zkt-2.9.1-e89380a1", "zkt-2.9.1-e89380a1"),
])
def test_settings_change_names_board_software_only_when_chosen(add, tmp_path, mode, bundle_id,
                                                               sent):
    c = signed_in(add, tmp_path)
    c.create_package(replace(request(support.zkt_config(bundle_id=bundle_id)), mode=mode))
    body = json.loads(add.requests[-1].content)
    assert body.get("bundle_id", None) == sent and ("bundle_id" in body) is (sent is not None)


def test_logout_clears_the_session(add, tmp_path):
    c = signed_in(add, tmp_path)
    c.logout()
    assert add.requests[-1].url.path == "/api/v1/auth/logout"
    assert add.requests[-1].headers["x-csrf-token"] == "csrf-1"
    with pytest.raises(InstallerError):
        c.zones()


def test_password_never_reaches_the_log(add, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="sl_installer")
    from sl_installer.core.logging_setup import RedactingFilter
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    c = signed_in(add, tmp_path)
    c.create_package(request())
    assert PASSWORD not in caplog.text and "test-wifi-pass" not in caplog.text


def test_fetch_bundle_over_http(tmp_path, store, zkt_bundle):
    add = FakeADD(zkt_bundle)
    c = signed_in(add, tmp_path, store=store)
    c.bundles(Family.ZKT)
    local = c.fetch_bundle(zkt_bundle.bundle_id, None, CancelToken())
    assert local.info.bundle_id == zkt_bundle.bundle_id
    downloaded = [r.url.path.rsplit("/", 1)[1] for r in add.requests if "/files/" in r.url.path]
    assert "storage-empty.bin" not in downloaded and "zone-lite-signed.bin" in downloaded


def test_tls_context_is_usable():
    assert isinstance(tls_context(), ssl.SSLContext)
