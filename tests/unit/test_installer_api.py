"""The State Life Device Installer API (/api/v1/installer/*, docs/esp-installer-server.md).

Requests go through the real app, admin login, CSRF and password step-up, on a
file-backed database so every request commits or rolls back like production.
The provisioner is an httpx.MockTransport that returns real envelopes, so a
test can open what the app would receive with the app's one-time key.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zk_add import installer_api, provisioning_api, web
from zk_add.db import Base, create_database_engine
from zk_add.hikvision_delivery import HikvisionPolicy
from zk_add.hikvision_evidence import HikvisionEvidence
from zk_add.models import AuditEvent, Connector
from zk_add.ota import FirmwareRelease
from zk_add.provisioning_api import FixedWindowLimiter
from zk_add.security import hash_admin_password
from zk_add.service import onboard_connector
from zk_add.settings import settings
from zk_add.time_utils import utc_now

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "firmware" / "zone_lite" / "tools"
PREFIX = "/api/v1/installer"

# Test-only values, chosen to be easy to search for in responses and audit rows.
ADMIN_PASSWORD = "installer-test-admin-password"
WIFI_PASSWORD = "test-only-wifi-passphrase"
COMM_KEY = 246813579
HIK_PASSWORD = "test-only-terminal-secret"
INTERNAL_TOKEN = "test-only-internal-token-0123456789abcdef"
MAC = "ac:27:6e:a4:3d:30"
ZKT_BUNDLE = "zkt-2.7.8-b8d0ca7e"
HIK_BUNDLE = "hikvision-3.1.1-dc7d9c3-21ac81b2"
SECRETS = (ADMIN_PASSWORD, WIFI_PASSWORD, str(COMM_KEY), HIK_PASSWORD, INTERNAL_TOKEN)
# The owner's words for a board whose key may come from an older root (B1/F1).
SETTINGS_REFUSED = "This board was set up before this app. Only head office can change its settings."
LEFT_OUT = object()


def _load_envelope():
    spec = importlib.util.spec_from_file_location(
        "installer_test_envelope", TOOLS / "provisioning_envelope.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


envelope = _load_envelope()


@pytest.fixture(scope="module")
def signing_key():
    # A throwaway RSA-3072 key standing in for vault key-1.
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture(scope="module")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture(scope="module")
def admin_password_hash():
    return hash_admin_password(ADMIN_PASSWORD)


def public_pem_b64(key) -> str:
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(pem).decode()


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def sign(key, manifest: dict) -> str:
    signature = key.sign(
        canonical(manifest),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def write_bundle(store: Path, key, bundle_id: str, *, family: str = "zkt", version: str = "2.7.8",
                 recommended: bool = True, created_at: str = "2026-11-01T00:00:00Z",
                 release_state: str = "AVAILABLE", mutate=None, sign_with=None) -> Path:
    directory = store / bundle_id
    directory.mkdir(parents=True)
    contents = {
        "bootloader-signed.bin": b"boot" * 256,
        "partition-table.bin": b"\xaa\x50" * 1536,
        "ota-data.bin": b"\xff" * 512,
        "zone-lite-signed.bin": bundle_id.encode() * 64,
        "rollback-predecessor.bin": b"predecessor-2.5.2" * 32,
        "storage-empty.bin": b"\xff" * 4096,
    }
    layout = [
        ("bootloader", "bootloader-signed.bin", 0),
        ("partition-table", "partition-table.bin", 0x8000),
        ("otadata", "ota-data.bin", 0x17000),
        ("factory", "zone-lite-signed.bin", 0x20000),
        ("ota_0", "zone-lite-signed.bin", 0x2A0000),
        ("ota_1", "rollback-predecessor.bin", 0x520000),
        ("storage", "storage-empty.bin", 0x7A0000),
    ]
    if family == "hikvision":
        layout = [row for row in layout if row[0] not in {"ota_0", "ota_1"}]
        contents.pop("rollback-predecessor.bin")
    images = []
    for role, name, offset in layout:
        data = contents[name]
        image = {"role": role, "name": name, "offset": offset, "size": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
        if role == "factory":
            image.update(app_project=installer_api.APP_PROJECTS[family], app_version=version)
        images.append(image)
    manifest = {
        "schema": "zone-lite-installer-bundle-v1",
        "bundle_id": bundle_id,
        "firmware_family": family,
        "label": f"{'ZKT' if family == 'zkt' else 'HIK'} {version}",
        "version": version,
        "release_state": release_state,
        "recommended": recommended,
        "git_sha": "b" * 40,
        "created_at": created_at,
        "partition_layout": "zone-lite-ota-v1",
        "notes": "Test bundle.",
        "images": images,
    }
    if mutate:
        mutate(manifest)
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "manifest.sig").write_text(sign(sign_with or key, manifest), encoding="ascii")
    return directory


class FakeProvisioner:
    """The protected provisioner's v2 endpoint, returning real envelopes."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.ready_status = 200
        self.respond = self.build
        self.nvs = b"NVS-PLAINTEXT".ljust(24576, b"\xff")
        self.hmac_key = bytes(range(32))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/health/ready":
            return httpx.Response(self.ready_status, json={"ok": self.ready_status == 200})
        return self.respond(request)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=5)

    @property
    def builds(self) -> list[httpx.Request]:
        return [item for item in self.requests if item.url.path.endswith("/nvs-packages")]

    def build(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        config = payload["configuration"]
        aad = {
            "schema_version": 1, "firmware_family": config["firmware_family"],
            "request_id": payload["request_id"], "target_mac": payload["target_mac"],
            "zone_id": config["zone_id"], "zone_device_id": config["device_id"],
            "nvs_offset": "0x11000", "nvs_size": len(self.nvs),
            "nvs_sha256": hashlib.sha256(self.nvs).hexdigest(),
        }
        ciphertext, nvs_envelope = envelope.encrypt_for_recipient(
            self.nvs, payload["recipient_public_key"], aad
        )
        hmac_aad = {
            "schema_version": 1, "request_id": payload["request_id"],
            "target_mac": payload["target_mac"], "purpose": "zone-lite-nvs-hmac-efuse-key",
            "key_size": 32,
        }
        hmac_ciphertext, hmac_envelope = envelope.encrypt_for_recipient(
            self.hmac_key, payload["recipient_public_key"], hmac_aad
        )
        manifest = {
            "schema_version": 1, "created_at": utc_now().isoformat(), "aad": aad,
            "envelope": nvs_envelope, "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            "hmac_key": {"aad": hmac_aad, "envelope": hmac_envelope,
                         "ciphertext_sha256": hashlib.sha256(hmac_ciphertext).hexdigest()},
        }
        return httpx.Response(201, json={
            "provisioning_manifest": manifest,
            "provisioning_ciphertext_b64": base64.b64encode(ciphertext).decode(),
            "hmac_ciphertext_b64": base64.b64encode(hmac_ciphertext).decode(),
        })


@dataclass
class Harness:
    client: TestClient
    sessions: sessionmaker
    store: Path
    signing_key: object
    provisioner: FakeProvisioner
    app_key: X25519PrivateKey  # gitleaks:allow -- type annotation, not key material
    csrf: str = ""
    monkeypatch: object = None
    extra: dict = field(default_factory=dict)

    @property
    def app_public_key(self) -> str:
        return base64.b64encode(self.app_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

    def login(self) -> str:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": "StateHealthAdmin", "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        return self.csrf

    def get(self, path: str, **kwargs):
        return self.client.get(PREFIX + path, **kwargs)

    def post(self, path: str, body: dict, *, csrf: str | None = None):
        return self.client.post(
            PREFIX + path, json=body, headers={"X-CSRF-Token": self.csrf if csrf is None else csrf}
        )

    def audits(self, action: str | None = None) -> list[AuditEvent]:
        with self.sessions() as db:
            statement = select(AuditEvent).order_by(AuditEvent.id)
            if action:
                statement = statement.where(AuditEvent.action == action)
            return list(db.scalars(statement))


@pytest.fixture
def installer(tmp_path, monkeypatch, signing_key, admin_password_hash):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'installer.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(provisioning_api, "SessionLocal", sessions)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    store = tmp_path / "installer-bundles"
    store.mkdir()
    for name, value in {
        "admin_username": "StateHealthAdmin",
        "admin_password_hash": admin_password_hash,
        "admin_cookie_secure": False,
        "provisioning_enabled": True,
        "installer_enabled": True,
        "provisioning_internal_token": INTERNAL_TOKEN,
        "provisioning_worker_url": "http://add-provisioner.test",
        "firmware_signing_public_key_pem_b64": public_pem_b64(signing_key),
        "installer_bundle_store_path": str(store),
        "installer_min_app_version": "1.0.0",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(installer_api, "package_limiter", FixedWindowLimiter(30, 600))
    monkeypatch.setattr(installer_api, "event_limiter", FixedWindowLimiter(240, 600))
    provisioner = FakeProvisioner()
    monkeypatch.setattr(installer_api, "_provisioner_client", provisioner.client)
    installer_api.bundle_store.clear()
    harness = Harness(
        client=TestClient(web.app), sessions=sessions, store=store, signing_key=signing_key,
        provisioner=provisioner, app_key=X25519PrivateKey.generate(), monkeypatch=monkeypatch,
    )
    yield harness
    installer_api.bundle_store.clear()
    engine.dispose()


def zkt_configuration(**overrides) -> dict:
    values = {
        "firmware_family": "zkt", "zone_id": "ZONE-TEST-01", "zone_name": "ZONE-TEST-01",
        "device_id": "1", "wifi_ssid": "RE7", "wifi_password": WIFI_PASSWORD,
        "zkt_port": 4370, "zkt_comm_key": COMM_KEY, "zkt_preferred_ip": "0.0.0.0",
    }
    values.update(overrides)
    return values


def hik_configuration(**overrides) -> dict:
    values = {
        "firmware_family": "hikvision", "zone_id": "LF-ZONE-JHELUM-01",
        "zone_name": "LF-ZONE-JHELUM-01", "device_id": "1", "wifi_ssid": "RE7",
        "wifi_password": WIFI_PASSWORD, "hik_host": "192.168.10.20", "hik_username": "admin",
        "hik_password": HIK_PASSWORD, "hik_expected_serial": "DS-TEST-SERIAL-0001",
        "hik_profile": "ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1",
        "hik_source_epoch": "lf-zone-jhelum-01-20261101",
    }
    values.update(overrides)
    return values


def package_body(harness: Harness, **overrides) -> dict:
    body = {
        "password": ADMIN_PASSWORD, "request_id": "installer-test-0001", "mac": "AC:27:6E:A4:3D:30",
        "mode": "new_board", "bundle_id": ZKT_BUNDLE, "app_version": "1.0.0",
        "recipient_public_key": harness.app_public_key, "configuration": zkt_configuration(),
    }
    body.update(overrides)
    return body


def add_connector(harness: Harness, mac: str, *, zone_id: str, device_id: str = "1",
                  family: str = "zkt", name: str | None = None, seen: bool = False,
                  online: bool = False, spare: bool = False, active: bool = True) -> int:
    with harness.sessions() as db:
        connector, _token, _created = onboard_connector(
            db, hardware_id=mac, zone_id=zone_id, zone_name=name or zone_id, device_id=device_id,
            firmware_version="2.7.8", expected_serial=None, actor="test", ip_address=None,
            firmware_family=family,
        )
        connector.is_spare = spare
        connector.active = active
        now = utc_now()
        if seen or online:
            connector.last_seen_at = now - timedelta(seconds=5)
        if online:
            connector.connected = True
            connector.lifecycle_state = "ONLINE"
            zkt = connector.zkt_device
            zkt.connection_state = "ONLINE"
            zkt.online = True
            zkt.last_seen_at = now
            zkt.last_transition_at = now - timedelta(minutes=30)
        db.commit()
        return connector.id


# The shared sign-in checks keep their own sentence under "detail".
SHARED_DETAILS = {
    "AUTH_REQUIRED": "Authentication required.",
    "SESSION_EXPIRED": "Session expired.",
    "CSRF_INVALID": "Invalid CSRF token.",
    "PASSWORD_REJECTED": "Password confirmation failed.",
    "NOT_FOUND": "Not Found",
}


def assert_installer_error(response, status: int, code: str, field: str | None = None) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    if code in SHARED_DETAILS:
        assert body["detail"] == SHARED_DETAILS[code]
    else:
        assert body["detail"] == {key: value for key, value in body.items() if key != "detail"}
    assert isinstance(body["message"], str) and body["message"]
    assert body.get("field") == field
    return body


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# ------------------------------------------------------------------ access


ROUTES = [
    ("get", "/capabilities"), ("get", "/bundles"), ("get", f"/bundles/{ZKT_BUNDLE}/manifest"),
    ("get", f"/bundles/{ZKT_BUNDLE}/files/manifest.json"), ("get", f"/devices/{MAC}"),
    ("get", "/zones"), ("post", "/packages"), ("post", "/events"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_needs_the_admin_session(installer, method, path):
    response = getattr(installer.client, method)(PREFIX + path, **({"json": {}} if method == "post" else {}))
    assert_installer_error(response, 401, "AUTH_REQUIRED")


@pytest.mark.parametrize("path", ["/packages", "/events"])
@pytest.mark.parametrize("csrf", ["", "wrong-token"])
def test_mutations_need_the_csrf_token(installer, path, csrf):
    installer.login()
    response = installer.post(path, {}, csrf=csrf)
    assert_installer_error(response, 403, "CSRF_INVALID")


def test_package_needs_the_password_step_up(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    response = installer.post("/packages", package_body(installer, password="not-the-password"))
    assert_installer_error(response, 403, "PASSWORD_REJECTED", "password")
    assert installer.provisioner.builds == []
    assert installer.audits("INSTALLER_PACKAGE_ISSUED") == []


def test_other_routes_keep_their_error_shapes(installer):
    unauthenticated = installer.client.get("/api/v1/overview")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "Authentication required."}
    invalid = installer.client.post("/api/v1/auth/login", json={})
    assert invalid.status_code == 422
    assert all(set(item) == {"type", "loc", "msg"} for item in invalid.json()["detail"])
    assert "code" not in invalid.json()


def test_unknown_installer_paths_use_the_installer_shape(installer):
    installer.login()
    assert_installer_error(installer.get("/nothing-here"), 404, "NOT_FOUND")


# ------------------------------------------------------------ capabilities


def test_capabilities_report_the_server_state(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    body = installer.get("/capabilities").json()
    assert body["enabled"] is True
    assert body["provisioner_ready"] is True
    assert body["min_app_version"] == "1.0.0"
    assert body["server_time"].endswith("Z")
    assert body["bundle_count"] == 1
    installer.provisioner.ready_status = 503
    assert installer.get("/capabilities").json()["provisioner_ready"] is False

    def unreachable(request):
        raise httpx.ConnectError("refused", request=request)

    installer.provisioner.handler = unreachable
    assert installer.get("/capabilities").json()["provisioner_ready"] is False
    installer.monkeypatch.setattr(settings, "provisioning_internal_token", None)
    assert installer.get("/capabilities").json()["provisioner_ready"] is False


@pytest.mark.parametrize("flag", ["installer_enabled", "provisioning_enabled"])
def test_the_kill_switch_closes_every_route_but_capabilities(installer, flag):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    installer.monkeypatch.setattr(settings, flag, False)
    capabilities = installer.get("/capabilities").json()
    assert (capabilities["enabled"], capabilities["provisioner_ready"], capabilities["bundle_count"]) \
        == (False, False, 0)
    for method, path in ROUTES[1:]:
        if method == "get":
            response = installer.get(path)
        else:
            response = installer.post(path, package_body(installer) if path == "/packages" else {})
        assert_installer_error(response, 409, "INSTALLER_DISABLED")
    assert installer.provisioner.builds == []


# ----------------------------------------------------------------- bundles


def test_bundles_list_only_verified_bundles_and_recommend_the_newest(installer, other_key):
    key, store = installer.signing_key, installer.store
    write_bundle(store, key, "zkt-2.7.6-aaaaaaaa", version="2.7.6", created_at="2026-10-01T00:00:00Z")
    newest = write_bundle(store, key, ZKT_BUNDLE, created_at="2026-11-01T00:00:00Z")
    write_bundle(store, key, "zkt-2.8.0-cccccccc", version="2.8.0", recommended=False,
                 created_at="2026-11-02T00:00:00Z", release_state="HIL_ONLY")
    write_bundle(store, key, HIK_BUNDLE, family="hikvision", version="3.1.1",
                 release_state="FIELD_BASELINE")
    # Each of these is skipped, and none breaks the others.
    write_bundle(store, key, "zkt-other-key", sign_with=other_key)
    tampered = write_bundle(store, key, "zkt-tampered-image")
    (tampered / "zone-lite-signed.bin").write_bytes(b"changed after signing")
    edited = write_bundle(store, key, "zkt-edited-manifest")
    manifest = json.loads((edited / "manifest.json").read_text())
    manifest["notes"] = "edited after signing"
    (edited / "manifest.json").write_text(json.dumps(manifest))
    write_bundle(store, key, "zkt-renamed", mutate=lambda item: item.update(bundle_id="zkt-elsewhere"))
    write_bundle(store, key, "zkt-revoked", release_state="REVOKED")
    write_bundle(store, key, "zkt-wrong-layout", mutate=lambda item: item.update(partition_layout="x"))
    write_bundle(store, key, "zkt-hik-image", mutate=lambda item: item["images"][3].update(
        app_project="zone_lite_hikvision"))
    write_bundle(store, key, "zkt-no-factory", mutate=lambda item: item.update(
        images=[image for image in item["images"] if image["role"] != "factory"]))
    no_signature = write_bundle(store, key, "zkt-no-signature")
    (no_signature / "manifest.sig").unlink()
    garbage = store / "zkt-garbage"
    garbage.mkdir()
    (garbage / "manifest.json").write_bytes(b"\x00\xff not json")
    (garbage / "manifest.sig").write_text("not base64 !!")
    listed = write_bundle(store, key, "zkt-list-manifest")
    (listed / "manifest.json").write_text("[]")
    (store / ".publishing-tmp").mkdir()
    (store / "README.txt").write_text("not a bundle")
    installer.login()

    response = installer.get("/bundles")
    assert response.status_code == 200, response.text
    rows = response.json()["bundles"]
    assert [row["bundle_id"] for row in rows] == [
        HIK_BUNDLE, ZKT_BUNDLE, "zkt-2.8.0-cccccccc", "zkt-2.7.6-aaaaaaaa",
    ]
    by_id = {row["bundle_id"]: row for row in rows}
    # Recommended: the newest bundle flagged recommended, once per family.
    assert {bundle_id for bundle_id, row in by_id.items() if row["recommended"]} == {
        ZKT_BUNDLE, HIK_BUNDLE}
    assert set(by_id[ZKT_BUNDLE]) == {
        "bundle_id", "family", "label", "version", "recommended", "release_state", "notes",
        "download_bytes", "manifest_sha256",
    }
    signed = json.loads((newest / "manifest.json").read_text())
    assert by_id[ZKT_BUNDLE]["manifest_sha256"] == hashlib.sha256(canonical(signed)).hexdigest()
    # Distinct files once each: factory and ota_0 share one image.
    assert by_id[ZKT_BUNDLE]["download_bytes"] == sum(
        path.stat().st_size for path in newest.iterdir() if path.suffix == ".bin"
    )
    assert (by_id[HIK_BUNDLE]["family"], by_id[HIK_BUNDLE]["release_state"]) == (
        "hikvision", "FIELD_BASELINE")
    assert [row["bundle_id"] for row in installer.get("/bundles?family=hikvision").json()["bundles"]] \
        == [HIK_BUNDLE]
    assert_installer_error(installer.get("/bundles?family=other"), 422, "INVALID_FIELD", "family")
    assert installer.get("/capabilities").json()["bundle_count"] == 4


def test_bundles_need_the_signing_key_and_the_store(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    installer.monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", None)
    assert installer.get("/bundles").json() == {"bundles": []}
    installer.monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", "bm90IGEga2V5")
    assert installer.get("/bundles").json() == {"bundles": []}
    installer.monkeypatch.setattr(settings, "installer_bundle_store_path", str(installer.store / "gone"))
    assert installer.get("/bundles").json() == {"bundles": []}
    assert installer.get("/capabilities").json()["bundle_count"] == 0


def test_bundle_hashes_are_checked_once_and_again_after_a_change(installer):
    directory = write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    hashed = []
    real = installer_api._sha256_file

    def counting(path):
        hashed.append(path.name)
        return real(path)

    installer.monkeypatch.setattr(installer_api, "_sha256_file", counting)
    installer.login()
    assert len(installer.get("/bundles").json()["bundles"]) == 1
    first = len(hashed)
    assert first == 6  # each distinct image once
    installer.get("/bundles")
    installer.get(f"/bundles/{ZKT_BUNDLE}/manifest")
    assert len(hashed) == first
    changed = directory / "storage-empty.bin"
    before = changed.stat().st_mtime_ns
    changed.write_bytes(b"\x00" * 4096)  # same size, other bytes
    os.utime(changed, ns=(before + 10**9, before + 10**9))
    assert installer.get("/bundles").json()["bundles"] == []
    assert len(hashed) > first


def test_manifest_and_files_are_served_as_signed(installer):
    directory = write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    body = installer.get(f"/bundles/{ZKT_BUNDLE}/manifest").json()
    assert set(body) == {"manifest", "signature"}
    installer.signing_key.public_key().verify(
        base64.b64decode(body["signature"]), canonical(body["manifest"]),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256(),
    )
    image = installer.get(f"/bundles/{ZKT_BUNDLE}/files/zone-lite-signed.bin")
    assert image.status_code == 200
    assert image.content == (directory / "zone-lite-signed.bin").read_bytes()
    assert image.headers["content-type"] == "application/octet-stream"
    assert image.headers["x-content-sha256"] == hashlib.sha256(image.content).hexdigest()
    assert_installer_error(installer.get(f"/bundles/{ZKT_BUNDLE}/files/manifest.sig"), 404,
                           "FILE_NOT_FOUND")
    assert_installer_error(installer.get("/bundles/zkt-unknown/manifest"), 404, "BUNDLE_NOT_FOUND")


def test_paths_never_leave_the_bundle_store(installer, tmp_path):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    (tmp_path / "secret.txt").write_text("TOP-SECRET-OUTSIDE-THE-STORE")
    # A bundle reached through a symlink, or with an image that points out of
    # its directory, verifies but is still refused.
    outside = write_bundle(tmp_path / "outside", installer.signing_key, "zkt-linked")
    (installer.store / "zkt-linked").symlink_to(outside, target_is_directory=True)
    escaping = write_bundle(installer.store, installer.signing_key, "zkt-escaping-image")
    (escaping / "storage-empty.bin").unlink()
    (escaping / "storage-empty.bin").symlink_to(outside / "storage-empty.bin")
    installer.login()
    assert [row["bundle_id"] for row in installer.get("/bundles").json()["bundles"]] == [ZKT_BUNDLE]
    for path in (
        "/bundles/%2e%2e/manifest",
        "/bundles/..%2F..%2Fsecret.txt/manifest",
        "/bundles/zkt-linked/manifest",
        "/bundles/zkt-escaping-image/files/storage-empty.bin",
        f"/bundles/{ZKT_BUNDLE}/files/..%2F..%2Fsecret.txt",
        f"/bundles/{ZKT_BUNDLE}/files/%2e%2e",
        f"/bundles/{ZKT_BUNDLE}/files/.hidden",
        f"/bundles/{ZKT_BUNDLE}/files/zone-lite-signed.bin%00.txt",
        f"/bundles/{ZKT_BUNDLE}%2F..%2F..%2Fsecret.txt/files/x",
    ):
        response = installer.get(path)
        assert response.status_code == 404, path
        assert response.json()["code"] in {"NOT_FOUND", "BUNDLE_NOT_FOUND", "FILE_NOT_FOUND"}
        assert "TOP-SECRET" not in response.text


def test_a_store_entry_that_cannot_be_read_is_skipped_and_logged_once(installer, caplog):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    loop = installer.store / "zkt-loop"
    os.symlink(loop, loop)  # Path.resolve raises RuntimeError for it on Python 3.12
    installer.login()
    caplog.set_level(logging.WARNING)
    for _attempt in range(2):
        response = installer.get("/bundles")
        assert response.status_code == 200, response.text
        assert [row["bundle_id"] for row in response.json()["bundles"]] == [ZKT_BUNDLE]
        assert installer.get("/capabilities").json()["bundle_count"] == 1
    assert_installer_error(installer.get("/bundles/zkt-loop/manifest"), 404, "BUNDLE_NOT_FOUND")
    assert [record.getMessage() for record in caplog.records if "zkt-loop" in record.getMessage()] \
        == ["Installer bundle zkt-loop skipped: its directory cannot be read"]


def test_a_file_transfer_holds_no_database_transaction(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    opened = []

    def tracked_session():
        opened.append(installer.sessions())
        return opened[-1]

    installer.monkeypatch.setattr(provisioning_api, "SessionLocal", tracked_session)
    during_transfer = []

    class WatchedFileResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            during_transfer.extend(session.in_transaction() for session in opened)
            await super().__call__(scope, receive, send)

    installer.monkeypatch.setattr(installer_api, "FileResponse", WatchedFileResponse)
    # The request's session closes only after the file is sent; a slow link must
    # not keep its transaction open past PostgreSQL's idle-in-transaction limit.
    response = installer.get(f"/bundles/{ZKT_BUNDLE}/files/zone-lite-signed.bin")
    assert response.status_code == 200, response.text
    assert during_transfer == [False]


# ----------------------------------------------------------------- devices


def test_an_unknown_board_is_reported_as_new(installer):
    installer.login()
    body = installer.get("/devices/AC-27-6E-A4-3D-30").json()
    assert body == {
        "known": False, "mac": MAC, "connector_id": None, "zone_id": None, "zone_name": None,
        "device_id": None, "family": None, "firmware_version": None, "online": None,
        "last_seen_at": None, "ever_online": False, "public_name": None, "group": None,
        "status_text": None, "hik_source_epoch": None,
        "settings_change_allowed": True, "settings_change_reason": None,
    }


@pytest.mark.parametrize("bad", ["ac:27:6e:a4:3d", "zz:27:6e:a4:3d:30", "ac:27-6e:a4:3d:30", "x" * 40])
def test_a_bad_board_id_is_an_invalid_field(installer, bad):
    installer.login()
    response = installer.get(f"/devices/{bad}")
    assert_installer_error(response, 422, "INVALID_FIELD", "mac")
    assert bad not in response.text


def test_a_board_that_only_onboarded_was_never_online(installer):
    add_connector(installer, MAC, zone_id="ZONE-SWAT-01")
    installer.login()
    for form in ("AC:27:6E:A4:3D:30", "ac276ea43d30", "ac-27-6e-a4-3d-30"):
        body = installer.get(f"/devices/{form}").json()
        assert (body["known"], body["mac"], body["ever_online"], body["online"]) == (
            True, MAC, False, False)
        assert body["status_text"] == "Set up, but it has not connected to head office yet."
        assert (body["public_name"], body["group"], body["family"]) == ("Swat", "Health", "zkt")
        assert body["last_seen_at"] is None
        assert (body["settings_change_allowed"], body["settings_change_reason"]) == (True, None)


def test_an_online_board_reports_its_record_in_plain_words(installer):
    add_connector(installer, MAC, zone_id="LF-ZONE-BLD9-03", online=True)
    add_connector(installer, "ac:27:6e:a4:3d:31", zone_id="LF-ZONE-BLD9-01")
    installer.login()
    body = installer.get(f"/devices/{MAC}").json()
    assert (body["known"], body["ever_online"], body["online"]) == (True, True, True)
    assert (body["zone_id"], body["device_id"], body["firmware_version"]) == (
        "LF-ZONE-BLD9-03", "1", "2.7.8")
    assert (body["public_name"], body["group"]) == ("Building 9 · Device 3", "Life")
    assert body["status_text"].startswith("Online since ")
    assert body["status_text"].endswith(("AM", "PM"))
    assert body["last_seen_at"].endswith("Z")
    assert body["hik_source_epoch"] is None
    # Online, and not prepared by this app: its key may come from an older root.
    assert (body["settings_change_allowed"], body["settings_change_reason"]) == (
        False, SETTINGS_REFUSED)


def test_an_offline_board_says_why(installer):
    connector_pk = add_connector(installer, MAC, zone_id="ZONE-SLICTOWER-3FL", seen=True, spare=True)
    with installer.sessions() as db:
        db.get(Connector, connector_pk).last_seen_at = utc_now() - timedelta(days=3)
        db.commit()
    installer.login()
    body = installer.get(f"/devices/{MAC}").json()
    assert (body["ever_online"], body["online"]) == (True, False)
    assert body["public_name"] == "SLIC Tower · 3rd Floor"
    assert body["status_text"].startswith("Offline since ")
    assert "No internet." in body["status_text"]
    assert body["status_text"].endswith("It is marked as a spare board.")


def test_a_hikvision_board_reports_the_epoch_add_has(installer):
    connector_pk = add_connector(installer, MAC, zone_id="LF-ZONE-JHELUM-01", family="hikvision",
                                 seen=True)
    installer.login()
    assert installer.get(f"/devices/{MAC}").json()["hik_source_epoch"] is None
    with installer.sessions() as db:
        for epoch in ("lf-zone-jhelum-01-20261030", "lf-zone-jhelum-01-20261031"):
            db.add(HikvisionEvidence(
                connector_id=connector_pk, terminal_serial="DS-TEST", source_epoch=epoch,
                observation_sha256=hashlib.sha256(epoch.encode()).hexdigest(), channel="POLL",
                disposition="QUALIFICATION_PENDING", raw_encrypted="-", captured_epoch=1,
            ))
        db.commit()
    body = installer.get(f"/devices/{MAC}").json()
    assert (body["family"], body["hik_source_epoch"]) == ("hikvision", "lf-zone-jhelum-01-20261031")
    with installer.sessions() as db:
        db.add(HikvisionPolicy(connector_id=connector_pk, enabled=True, terminal_serial="DS-TEST",
                               source_epoch="lf-zone-jhelum-01-policy", profile_id="test",
                               success_codes=[], excluded_codes=[]))
        db.commit()
    assert installer.get(f"/devices/{MAC}").json()["hik_source_epoch"] == "lf-zone-jhelum-01-policy"


def test_zones_group_active_boards(installer):
    add_connector(installer, "ac:27:6e:00:00:01", zone_id="ZONE-PESHAWAR-02", device_id="10")
    add_connector(installer, "ac:27:6e:00:00:02", zone_id="ZONE-PESHAWAR-02", device_id="2",
                  family="hikvision", name="ZONE-PESHAWAR-02 renamed")
    add_connector(installer, "ac:27:6e:00:00:03", zone_id="ZONE-PESHAWAR-02", device_id="1",
                  spare=True)
    add_connector(installer, "ac:27:6e:00:00:04", zone_id="ZONE-PESHAWAR-02", device_id="7",
                  active=False)
    add_connector(installer, "ac:27:6e:00:00:05", zone_id="ZONE-LAHORE-01")
    installer.login()
    assert installer.get("/zones").json() == {"zones": [
        {"zone_id": "ZONE-LAHORE-01", "zone_name": "ZONE-LAHORE-01", "device_ids": ["1"],
         "families": ["zkt"]},
        {"zone_id": "ZONE-PESHAWAR-02", "zone_name": "ZONE-PESHAWAR-02",
         "device_ids": ["1", "2", "10"], "families": ["hikvision", "zkt"]},
    ]}


# ---------------------------------------------------------------- packages


def test_a_package_is_built_audited_and_readable_only_with_the_app_key(installer, caplog):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    caplog.set_level(logging.DEBUG)
    response = installer.post("/packages", package_body(installer))
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"request_id", "provisioning_manifest", "provisioning_ciphertext_b64",
                         "hmac_ciphertext_b64"}
    assert body["request_id"] == "installer-test-0001"
    # What the provisioner was asked for.
    (build,) = installer.provisioner.builds
    assert str(build.url) == "http://add-provisioner.test/internal/v2/nvs-packages"
    assert build.headers["authorization"] == f"Bearer {INTERNAL_TOKEN}"
    sent = json.loads(build.content)
    assert set(sent) == {"request_id", "target_mac", "recipient_public_key", "configuration"}
    assert (sent["request_id"], sent["target_mac"], sent["recipient_public_key"]) == (
        "installer-test-0001", MAC, installer.app_public_key)
    assert sent["configuration"] == zkt_configuration()
    # The app opens both envelopes with its one-time key.
    private = installer.app_key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    manifest = body["provisioning_manifest"]
    assert envelope.decrypt_envelope(
        base64.b64decode(body["provisioning_ciphertext_b64"]), private, manifest
    ) == installer.provisioner.nvs
    assert envelope.decrypt_envelope(
        base64.b64decode(body["hmac_ciphertext_b64"]), private, manifest["hmac_key"]
    ) == installer.provisioner.hmac_key
    # One audit row, with the agreed fields only and no secret anywhere.
    (audit,) = installer.audits("INSTALLER_PACKAGE_ISSUED")
    assert (audit.actor, audit.target_type, audit.target_id, audit.outcome, audit.request_id) == (
        "StateHealthAdmin", "esp", MAC, "SUCCESS", "installer-test-0001")
    assert audit.after == {"mode": "new_board", "bundle_id": ZKT_BUNDLE, "zone_id": "ZONE-TEST-01",
                           "device_id": "1", "firmware_family": "zkt", "app_version": "1.0.0"}
    with installer.sessions() as db:
        rows = db.scalars(select(AuditEvent)).all()
    assert_no_secrets(json.dumps([[row.actor, row.action, row.target_id, row.before, row.after,
                                   row.request_id] for row in rows]))
    assert_no_secrets(caplog.text)


def test_a_hikvision_package_gets_the_family_defaults(installer):
    write_bundle(installer.store, installer.signing_key, HIK_BUNDLE, family="hikvision", version="3.1.1")
    installer.login()
    for transport, port in ((None, 443), ("https", 443), ("http_digest", 80)):
        configuration = hik_configuration()
        if transport:
            configuration["hik_transport"] = transport
        response = installer.post("/packages", package_body(
            installer, bundle_id=HIK_BUNDLE, configuration=configuration))
        assert response.status_code == 201, response.text
        sent = json.loads(installer.provisioner.builds[-1].content)["configuration"]
        assert (sent["hik_transport"], sent["hik_port"]) == (transport or "https", port)
        assert (sent["zkt_comm_key"], sent["zkt_preferred_ip"], sent["zkt_port"]) == (0, "0.0.0.0", 4370)
        assert sent["hik_password"] == HIK_PASSWORD
        assert sent["hik_ca_pem"] == ""
    response = installer.post("/packages", package_body(
        installer, bundle_id=HIK_BUNDLE, configuration=hik_configuration(hik_port=8080)))
    assert response.status_code == 201
    assert json.loads(installer.provisioner.builds[-1].content)["configuration"]["hik_port"] == 8080
    assert_no_secrets(json.dumps([row.after for row in installer.audits()]))


def test_a_hikvision_certificate_may_fill_one_nvs_string(installer):
    # 3967 bytes is the most ESP-IDF 5.5.3's NVS generator stores in one string;
    # one byte more is refused here as the field, not as a failed build (SV2).
    write_bundle(installer.store, installer.signing_key, HIK_BUNDLE, family="hikvision",
                 version="3.1.1")
    installer.login()
    footer = "\n-----END CERTIFICATE-----\n"
    certificate = "-----BEGIN CERTIFICATE-----\n".ljust(3967 - len(footer), "A") + footer
    assert len(certificate.encode("utf-8")) == 3967
    response = installer.post("/packages", package_body(
        installer, bundle_id=HIK_BUNDLE, configuration=hik_configuration(hik_ca_pem=certificate)))
    assert response.status_code == 201, response.text
    sent = json.loads(installer.provisioner.builds[-1].content)["configuration"]
    assert sent["hik_ca_pem"] == certificate
    response = installer.post("/packages", package_body(
        installer, request_id="installer-test-0002", bundle_id=HIK_BUNDLE,
        configuration=hik_configuration(hik_ca_pem=certificate + "A")))
    body = assert_installer_error(response, 422, "INVALID_FIELD", "hik_ca_pem")
    assert body["message"] == "The machine certificate is too long (up to 3967 characters)."
    assert len(installer.provisioner.builds) == 1


INVALID_CONFIGURATIONS = [
    ("zone_id", zkt_configuration(zone_id="BAD ZONE!")),
    ("zone_id", zkt_configuration(zone_id="Z" * 65)),
    ("zone_id", zkt_configuration(zone_id="-STARTS-WITH-DASH")),
    ("zone_id", zkt_configuration(zone_id="LF-ZONE-G&PBLD8-01")),
    ("zone_name", zkt_configuration(zone_name=" LEADING-SPACE")),
    ("zone_name", zkt_configuration(zone_name="TAB\tNAME")),
    ("zone_name", zkt_configuration(zone_name="N" * 121)),
    ("device_id", zkt_configuration(device_id="D" * 32)),
    ("device_id", zkt_configuration(device_id="dev 1")),
    ("device_id", zkt_configuration(device_id=1)),
    ("wifi_ssid", zkt_configuration(wifi_ssid="S" * 33)),
    ("wifi_ssid", zkt_configuration(wifi_ssid="")),
    ("wifi_password", zkt_configuration(wifi_password="short7!")),
    ("wifi_password", zkt_configuration(wifi_password="P" * 64)),
    ("zkt_port", zkt_configuration(zkt_port=0)),
    ("zkt_port", zkt_configuration(zkt_port=65536)),
    ("zkt_port", zkt_configuration(zkt_port="4370")),
    ("zkt_comm_key", zkt_configuration(zkt_comm_key=2**32)),
    ("zkt_comm_key", zkt_configuration(zkt_comm_key=-1)),
    ("zkt_comm_key", zkt_configuration(zkt_comm_key="1979")),
    ("zkt_comm_key", zkt_configuration(zkt_comm_key=True)),
    ("zkt_comm_key", {key: value for key, value in zkt_configuration().items()
                      if key != "zkt_comm_key"}),
    ("zkt_preferred_ip", zkt_configuration(zkt_preferred_ip="8.8.8.8")),
    ("zkt_preferred_ip", zkt_configuration(zkt_preferred_ip="192.168.001.020")),
    ("zkt_preferred_ip", {key: value for key, value in zkt_configuration().items()
                          if key != "zkt_preferred_ip"}),
    ("hik_password", zkt_configuration(hik_password="zkt-has-no-hikvision-password")),
    ("firmware_family", zkt_configuration(firmware_family="suprema")),
    ("hik_host", hik_configuration(hik_host="8.8.4.4")),
    ("hik_host", hik_configuration(hik_host="terminal.local")),
    ("hik_host", hik_configuration(hik_host="")),
    ("hik_username", hik_configuration(hik_username="")),
    ("hik_password", hik_configuration(hik_password="X" * 128)),
    ("hik_expected_serial", hik_configuration(hik_expected_serial="S" * 120)),
    ("hik_profile", hik_configuration(hik_profile="")),
    ("hik_source_epoch", hik_configuration(hik_source_epoch="E" * 65)),
    # One NVS string holds at most 3967 bytes; the limit counts UTF-8 bytes.
    ("hik_ca_pem", hik_configuration(hik_ca_pem="C" * 3968)),
    ("hik_ca_pem", hik_configuration(hik_ca_pem="é" * 1984)),
    ("hik_port", hik_configuration(hik_port=0)),
    ("hik_transport", hik_configuration(hik_transport="telnet")),
    ("zkt_comm_key", hik_configuration(zkt_comm_key=COMM_KEY)),
]


@pytest.mark.parametrize(("expected", "configuration"), INVALID_CONFIGURATIONS)
def test_each_invalid_setting_names_its_field_and_never_its_value(installer, expected, configuration):
    family = configuration.get("firmware_family")
    bundle = HIK_BUNDLE if family == "hikvision" else ZKT_BUNDLE
    write_bundle(installer.store, installer.signing_key, bundle,
                 family="hikvision" if family == "hikvision" else "zkt")
    installer.login()
    response = installer.post("/packages", package_body(
        installer, bundle_id=bundle, configuration=configuration))
    assert_installer_error(response, 422, "INVALID_FIELD", expected)
    submitted = configuration.get(expected)
    if isinstance(submitted, str) and len(submitted) > 3:
        assert submitted not in response.text
    assert_no_secrets(response.text)
    assert installer.provisioner.builds == []


@pytest.mark.parametrize(("expected", "overrides"), [
    ("mac", {"mac": "not-a-mac"}),
    ("request_id", {"request_id": "short"}),
    ("request_id", {"request_id": "bad id with spaces"}),
    ("mode", {"mode": "factory_reset"}),
    ("bundle_id", {"bundle_id": "../zkt"}),
    ("app_version", {"app_version": "one.two"}),
    ("recipient_public_key", {"recipient_public_key": base64.b64encode(b"x" * 31).decode() + "AAAA"}),
    ("recipient_public_key", {"recipient_public_key": "!" * 44}),
    ("configuration", {"configuration": None}),
    ("password", {"password": ""}),
])
def test_each_invalid_request_field_is_named(installer, expected, overrides):
    installer.login()
    response = installer.post("/packages", package_body(installer, **overrides))
    assert_installer_error(response, 422, "INVALID_FIELD", expected)
    assert installer.provisioner.builds == []


def test_an_old_app_must_update(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    response = installer.post("/packages", package_body(installer, app_version="0.9.9"))
    assert_installer_error(response, 426, "APP_UPDATE_REQUIRED")


def test_a_board_keeps_the_family_add_knows(installer):
    write_bundle(installer.store, installer.signing_key, HIK_BUNDLE, family="hikvision", version="3.1.1")
    add_connector(installer, MAC, zone_id="ZONE-TEST-01", family="zkt", seen=True)
    installer.login()
    response = installer.post("/packages", package_body(
        installer, bundle_id=HIK_BUNDLE, configuration=hik_configuration(), mode="change_settings"))
    body = assert_installer_error(response, 409, "FAMILY_MISMATCH", "firmware_family")
    assert body["known_family"] == body["detail"]["known_family"] == "zkt"
    assert installer.provisioner.builds == []
    assert installer.audits("INSTALLER_PACKAGE_ISSUED") == []


def test_a_used_device_id_is_refused_with_the_next_free_number(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    add_connector(installer, "ac:27:6e:00:00:01", zone_id="ZONE-TEST-01", device_id="1")
    add_connector(installer, "ac:27:6e:00:00:02", zone_id="ZONE-TEST-01", device_id="2")
    add_connector(installer, "ac:27:6e:00:00:03", zone_id="ZONE-TEST-01", device_id="4")
    add_connector(installer, "ac:27:6e:00:00:04", zone_id="ZONE-TEST-01", device_id="3", active=False)
    installer.login()
    response = installer.post("/packages", package_body(installer))
    body = assert_installer_error(response, 409, "DEVICE_ID_IN_USE", "device_id")
    assert body["suggested_device_id"] == "3"
    assert installer.provisioner.builds == []
    # An inactive board's number is free, and a board may keep its own number.
    accepted = installer.post("/packages", package_body(
        installer, configuration=zkt_configuration(device_id="3")))
    assert accepted.status_code == 201, accepted.text
    # This app prepared the board just above, so it may change settings once online.
    add_connector(installer, MAC, zone_id="ZONE-TEST-01", device_id="5", seen=True)
    again = installer.post("/packages", package_body(
        installer, mode="change_settings", configuration=zkt_configuration(device_id="5")))
    assert again.status_code == 201, again.text
    assert [row.after["mode"] for row in installer.audits("INSTALLER_PACKAGE_ISSUED")] == [
        "new_board", "change_settings"]


@pytest.mark.parametrize("mode", ["new_board", "resume"])
def test_a_firmware_write_needs_a_verified_bundle_of_the_family(installer, other_key, mode):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    write_bundle(installer.store, other_key, "zkt-unsigned")
    installer.login()
    left_out = package_body(installer, mode=mode)
    del left_out["bundle_id"]
    for body in (left_out, package_body(installer, mode=mode, bundle_id=None),
                 package_body(installer, mode=mode, bundle_id="")):
        assert_installer_error(installer.post("/packages", body), 422, "INVALID_FIELD", "bundle_id")
    for bundle_id in ("zkt-missing", "zkt-unsigned"):
        response = installer.post("/packages", package_body(installer, mode=mode, bundle_id=bundle_id))
        assert_installer_error(response, 409, "BUNDLE_UNAVAILABLE", "bundle_id")
    response = installer.post("/packages", package_body(
        installer, mode=mode, configuration=hik_configuration()))
    assert_installer_error(response, 409, "BUNDLE_FAMILY_MISMATCH", "bundle_id")
    assert installer.provisioner.builds == []


def add_release(harness: Harness, image: Path, version: str, *, state: str = "AVAILABLE",
                revoked_at=None) -> None:
    with harness.sessions() as db:
        db.add(FirmwareRelease(
            release_id=f"zone-lite-{version}", version=version, git_sha="b" * 40,
            image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
            image_size=image.stat().st_size, signing_key_id="production-key",
            partition_layout="zone-lite-ota-v1", storage_name=f"zone-lite-{version}/firmware.bin",
            manifest={}, manifest_signature="test-signature", state=state, revoked_at=revoked_at,
        ))
        db.commit()


@pytest.mark.parametrize("revocation", [{"state": "REVOKED"}, {"revoked_at": utc_now()}],
                         ids=["state", "revoked-at"])
def test_a_bundle_whose_release_add_revoked_is_never_offered(installer, revocation):
    key, store = installer.signing_key, installer.store
    older = write_bundle(store, key, "zkt-2.7.7-aaaaaaaa", version="2.7.7",
                         created_at="2026-10-01T00:00:00Z")
    newest = write_bundle(store, key, ZKT_BUNDLE)
    write_bundle(store, key, HIK_BUNDLE, family="hikvision", version="3.1.1",
                 release_state="FIELD_BASELINE")
    add_release(installer, older / "zone-lite-signed.bin", "2.7.7")
    add_release(installer, newest / "zone-lite-signed.bin", "2.7.8", **revocation)
    installer.login()

    rows = installer.get("/bundles").json()["bundles"]
    # The revoked bundle is gone, and the newest one left is recommended instead.
    assert [(row["bundle_id"], row["recommended"]) for row in rows] == [
        (HIK_BUNDLE, True), ("zkt-2.7.7-aaaaaaaa", True)]
    assert installer.get("/capabilities").json()["bundle_count"] == 2
    assert_installer_error(installer.get(f"/bundles/{ZKT_BUNDLE}/manifest"), 404, "BUNDLE_NOT_FOUND")
    assert_installer_error(installer.get(f"/bundles/{ZKT_BUNDLE}/files/zone-lite-signed.bin"), 404,
                           "BUNDLE_NOT_FOUND")
    for mode in ("new_board", "resume"):
        response = installer.post("/packages", package_body(installer, mode=mode))
        assert_installer_error(response, 409, "BUNDLE_UNAVAILABLE", "bundle_id")
    assert installer.provisioner.builds == []
    response = installer.post("/packages", package_body(installer, bundle_id="zkt-2.7.7-aaaaaaaa"))
    assert response.status_code == 201, response.text


def test_a_revoked_rollback_predecessor_withdraws_every_bundle_that_carries_it(installer):
    key, store = installer.signing_key, installer.store
    newest = write_bundle(store, key, ZKT_BUNDLE)
    write_bundle(store, key, "zkt-2.7.7-aaaaaaaa", version="2.7.7", created_at="2026-10-01T00:00:00Z")
    write_bundle(store, key, HIK_BUNDLE, family="hikvision", version="3.1.1",
                 release_state="FIELD_BASELINE")
    # Both ZKT bundles carry the same predecessor image in ota_1.
    add_release(installer, newest / "rollback-predecessor.bin", "2.5.2", state="REVOKED")
    installer.login()
    assert [row["bundle_id"] for row in installer.get("/bundles").json()["bundles"]] == [HIK_BUNDLE]
    assert installer.get(f"/bundles/{HIK_BUNDLE}/manifest").status_code == 200


@pytest.mark.parametrize("bundle_id", [LEFT_OUT, None, "", "zkt-missing", HIK_BUNDLE],
                         ids=["left-out", "null", "empty", "unknown", "other-family"])
def test_a_settings_change_needs_no_firmware_bundle(installer, bundle_id):
    # The store is empty: a settings-only change writes no firmware, so the
    # family's bundle may be withdrawn or not published yet.
    add_connector(installer, MAC, zone_id="ZONE-TEST-01")
    installer.login()
    body = package_body(installer, mode="change_settings", bundle_id=bundle_id)
    if bundle_id is LEFT_OUT:
        del body["bundle_id"]
    response = installer.post("/packages", body)
    assert response.status_code == 201, response.text
    (audit,) = installer.audits("INSTALLER_PACKAGE_ISSUED")
    # No firmware was written, so no bundle is recorded.
    assert (audit.after["mode"], audit.after["bundle_id"]) == ("change_settings", None)


def test_a_settings_change_without_a_bundle_keeps_the_family_rules(installer):
    add_connector(installer, MAC, zone_id="ZONE-TEST-01", family="zkt")
    installer.login()
    response = installer.post("/packages", package_body(
        installer, mode="change_settings", bundle_id=None, configuration=hik_configuration()))
    assert_installer_error(response, 409, "FAMILY_MISMATCH", "firmware_family")
    response = installer.post("/packages", package_body(
        installer, mode="change_settings", bundle_id=None,
        configuration=zkt_configuration(hik_password="zkt-has-no-hikvision-password")))
    assert_installer_error(response, 422, "INVALID_FIELD", "hik_password")
    assert installer.provisioner.builds == []
    assert installer.audits("INSTALLER_PACKAGE_ISSUED") == []


def test_packages_are_rate_limited_per_user(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.monkeypatch.setattr(installer_api, "package_limiter", FixedWindowLimiter(2, 600))
    installer.login()
    for _attempt in range(2):
        assert installer.post("/packages", package_body(installer)).status_code == 201
    response = installer.post("/packages", package_body(installer))
    body = assert_installer_error(response, 429, "RATE_LIMITED")
    # The wait lasts until the first request leaves the 10-minute window.
    assert 590 <= int(response.headers["retry-after"]) <= 600
    assert "Wait 10 minutes" in body["message"]
    assert len(installer.provisioner.builds) == 2


def test_the_wait_lasts_until_the_oldest_counted_request_expires(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(provisioning_api, "time", SimpleNamespace(monotonic=lambda: clock.now))
    limiter = FixedWindowLimiter(3, 600)
    for moment in (1000.0, 1060.0, 1180.0):  # three requests within three minutes
        clock.now = moment
        assert limiter.allow("StateHealthAdmin")
    clock.now = 1200.0
    assert not limiter.allow("StateHealthAdmin")
    assert limiter.retry_after("StateHealthAdmin") == 400  # the first expires at 1600
    refusal = installer_api._rate_limited(400)
    assert refusal.headers == {"Retry-After": "400"}
    assert "Wait 7 minutes, then try again." in refusal.detail["message"]
    clock.now = 1599.5
    assert not limiter.allow("StateHealthAdmin")
    assert limiter.retry_after("StateHealthAdmin") == 1
    clock.now = 1600.0
    assert limiter.allow("StateHealthAdmin")
    # Refusals were never counted: the next slot frees with the second request.
    assert not limiter.allow("StateHealthAdmin")
    assert limiter.retry_after("StateHealthAdmin") == 60
    assert limiter.retry_after("another-user") == 1
    assert "Wait 1 minute, then try again." in installer_api._rate_limited(59).detail["message"]


def _answer(status: int, payload):
    return lambda request: httpx.Response(status, json=payload)


def _raise(error):
    def respond(request):
        raise error(f"simulated {error.__name__}", request=request)
    return respond


def _tampered(change):
    def respond(request):
        answer = FakeProvisioner.build(fake, request)
        payload = answer.json()
        change(payload)
        return httpx.Response(201, json=payload)
    fake = FakeProvisioner()
    return respond


@pytest.mark.parametrize(("respond", "status", "code", "field"), [
    (_raise(httpx.ConnectError), 502, "PROVISIONER_UNAVAILABLE", None),
    (_raise(httpx.ReadTimeout), 502, "PROVISIONER_UNAVAILABLE", None),
    (_answer(500, {"code": "BUILD_FAILED"}), 502, "PROVISIONER_UNAVAILABLE", None),
    (_answer(401, {"detail": "Internal authentication required."}), 502, "PROVISIONER_UNAVAILABLE", None),
    (_answer(422, {"code": "INVALID_FIELD", "field": "zone_name"}), 422, "INVALID_FIELD", "zone_name"),
    (_answer(422, {"code": "INVALID_FIELD", "field": "target_mac"}), 422, "INVALID_FIELD", "mac"),
    (_answer(422, {"code": "INVALID_FIELD", "field": "not_a_field"}), 502, "PROVISIONER_UNAVAILABLE",
     None),
    (_answer(422, ["unexpected"]), 502, "PROVISIONER_UNAVAILABLE", None),
    (_answer(201, {"provisioning_manifest": {}}), 502, "PROVISIONER_UNAVAILABLE", None),
    (_tampered(lambda payload: payload["provisioning_manifest"]["aad"].update(
        target_mac="ac:27:6e:00:00:99")), 502, "PROVISIONER_UNAVAILABLE", None),
    (_tampered(lambda payload: payload.update(provisioning_ciphertext_b64=base64.b64encode(
        b"other").decode())), 502, "PROVISIONER_UNAVAILABLE", None),
    (_tampered(lambda payload: payload["provisioning_manifest"]["hmac_key"]["aad"].update(
        request_id="installer-test-9999")), 502, "PROVISIONER_UNAVAILABLE", None),
])
def test_provisioner_failures_are_mapped_and_never_audited(installer, caplog, respond, status, code,
                                                           field):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.provisioner.respond = respond
    installer.login()
    caplog.set_level(logging.DEBUG)
    response = installer.post("/packages", package_body(installer))
    assert_installer_error(response, status, code, field)
    assert installer.audits("INSTALLER_PACKAGE_ISSUED") == []
    assert_no_secrets(response.text)
    assert_no_secrets(caplog.text)


def test_without_the_internal_token_nothing_is_sent(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.monkeypatch.setattr(settings, "provisioning_internal_token", None)
    installer.login()
    assert_installer_error(installer.post("/packages", package_body(installer)), 502,
                           "PROVISIONER_UNAVAILABLE")
    assert installer.provisioner.requests == []


# ------------------------------------------------------------------ events


def test_events_are_audited_without_secrets(installer):
    installer.login()
    response = installer.post("/events", {
        "request_id": "installer-test-0001", "mac": "AC:27:6E:A4:3D:30", "kind": "board_faulty",
        "detail": {"step": "lock_key", "loop_period_s": 9.8, "wifi_password": WIFI_PASSWORD,
                   "csrf_token": "c", "nested": {"session_cookie": "s", "hik_password": HIK_PASSWORD},
                   "attempts": [{"device_token": "t", "ok": False}]},
    })
    assert response.status_code == 204, response.text
    assert response.content == b""
    (audit,) = installer.audits("INSTALLER_BOARD_FAULTY")
    assert (audit.actor, audit.target_type, audit.target_id, audit.outcome, audit.request_id) == (
        "StateHealthAdmin", "esp", MAC, "REPORTED", "installer-test-0001")
    assert audit.after == {
        "step": "lock_key", "loop_period_s": 9.8, "wifi_password": "[REDACTED]",
        "csrf_token": "[REDACTED]",
        "nested": {"session_cookie": "[REDACTED]", "hik_password": "[REDACTED]"},
        "attempts": [{"device_token": "[REDACTED]", "ok": False}],
    }
    assert_no_secrets(json.dumps(audit.after))


def test_events_hide_the_firmware_s_own_secret_names_too(installer):
    installer.login()
    # The firmware's NVS names and other common secret names. A stored audit
    # row is hash-chained, so a value written there could never be removed.
    hidden = {
        "wifi_pass": "test-only-w1", "hik_pass": "test-only-h1", "tel_pass": "test-only-t1",
        "zkt_key": 135792468, "api_key": "test-only-a1", "pwd": "test-only-p1",
        "pass": "test-only-p2", "psk": "test-only-k1", "comm-key": "test-only-c1",
        "Wifi_Pass": "test-only-w2",
    }
    # What the app reports today stays readable.
    kept = {
        "mode": "new_board", "app_version": "1.0.0", "bundle_id": ZKT_BUNDLE,
        "step": "write_settings", "code": "NVS_FAILED", "outcome": "failed",
        "zone_id_seen": "ZONE-TEST-01", "device_id_seen": "1", "app_version_seen": "2.7.8",
        "advice": "SETTINGS_UNREADABLE", "loop_period_s": 9.8, "port": "COM5",
        "lock_burned": True,
    }
    response = installer.post("/events", {
        "request_id": "installer-test-0001", "mac": MAC, "kind": "install_failed",
        "detail": {**hidden, **kept, "nested": {"wifi_pass": "test-only-n1", "attempt": 2}},
    })
    assert response.status_code == 204, response.text
    (audit,) = installer.audits("INSTALLER_INSTALL_FAILED")
    assert audit.after == {**{key: "[REDACTED]" for key in hidden}, **kept,
                           "nested": {"wifi_pass": "[REDACTED]", "attempt": 2}}
    stored = json.dumps(audit.after)
    assert "test-only-" not in stored and "135792468" not in stored


def test_events_judge_each_key_before_it_is_cut_to_80_characters(installer):
    installer.login()
    # ADD stores only the first 80 characters of a key. A secret word after the
    # cut, or split by it, must still hide the value, at any depth (review SV3).
    long_keys = {
        "k" * 90 + "password": "test-only-l1",
        "x" * 76 + "_password": "test-only-l2",  # the cut keeps only "x…x_pas"
        "y" * 85 + "_token": "test-only-l3",  # an installer word
        "z" * 81 + "fleet_root": "test-only-l4",  # one of ADD's usual names
    }
    response = installer.post("/events", {
        "request_id": "installer-test-0001", "mac": MAC, "kind": "install_failed",
        "detail": {**long_keys, "step": "write_settings",
                   "nested": {**long_keys, "attempts": [dict(long_keys)]}},
    })
    assert response.status_code == 204, response.text
    (audit,) = installer.audits("INSTALLER_INSTALL_FAILED")
    hidden = {key[:80]: "[REDACTED]" for key in long_keys}
    assert len(hidden) == len(long_keys)
    assert audit.after == {**hidden, "step": "write_settings",
                           "nested": {**hidden, "attempts": [hidden]}}
    assert "test-only-" not in json.dumps(audit.after)


@pytest.mark.parametrize(("expected", "overrides"), [
    ("kind", {"kind": "PACKAGE_ISSUED"}),
    ("kind", {"kind": "package_issued"}),
    ("kind", {"kind": "board faulty!"}),
    ("kind", {"kind": "X"}),
    ("mac", {"mac": "nope"}),
    ("request_id", {"request_id": "x"}),
    ("detail", {"detail": {"text": "y" * 9000}}),
    ("detail", {"detail": ["not", "an", "object"]}),
])
def test_bad_events_are_refused(installer, expected, overrides):
    installer.login()
    body = {"request_id": "installer-test-0001", "mac": MAC, "kind": "FLASH_STARTED", "detail": {}}
    body.update(overrides)
    assert_installer_error(installer.post("/events", body), 422, "INVALID_FIELD", expected)
    assert [row.action for row in installer.audits() if row.action.startswith("INSTALLER_")] == []


def test_events_are_rate_limited_per_user(installer):
    installer.monkeypatch.setattr(installer_api, "event_limiter", FixedWindowLimiter(1, 600))
    installer.login()
    body = {"request_id": "installer-test-0001", "mac": MAC, "kind": "FLASH_STARTED"}
    assert installer.post("/events", body).status_code == 204
    response = installer.post("/events", body)
    assert_installer_error(response, 429, "RATE_LIMITED")
    assert 590 <= int(response.headers["retry-after"]) <= 600
    with installer.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "INSTALLER_FLASH_STARTED")) == 1


def test_times_read_like_the_status_page_in_pakistan_time():
    from datetime import datetime, timezone

    now = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)  # 2:00 PM in Pakistan
    words = installer_api._pakistan_time
    assert words(datetime(2026, 11, 1, 5, 42, tzinfo=timezone.utc), now) == "10:42 AM"
    assert words(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc), now) == "12:00 PM"
    # 00:05 in Pakistan is still today, though it is yesterday in UTC.
    assert words(datetime(2026, 10, 31, 19, 5, tzinfo=timezone.utc), now) == "12:05 AM"
    assert words(datetime(2026, 10, 31, 18, 59, tzinfo=timezone.utc), now) == "yesterday, 11:59 PM"
    assert words(datetime(2026, 10, 24, 7, 3, tzinfo=timezone.utc), now) == "24 Oct, 12:03 PM"
    assert words(datetime(2025, 12, 31, 20, 0, tzinfo=timezone.utc), now) == "1 Jan, 1:00 AM"
    assert words(datetime(2025, 12, 31, 18, 0, tzinfo=timezone.utc), now) == "31 Dec 2025, 11:00 PM"


def test_an_inactive_board_is_known_but_not_in_use(installer):
    add_connector(installer, MAC, zone_id="ZONE-SWAT-01", seen=True, active=False)
    installer.login()
    body = installer.get(f"/devices/{MAC}").json()
    assert (body["known"], body["ever_online"], body["online"]) == (True, True, False)
    assert body["status_text"] == "Not in use: head office has switched this board off."


def test_a_known_hikvision_board_can_be_set_up_again(installer):
    write_bundle(installer.store, installer.signing_key, HIK_BUNDLE, family="hikvision", version="3.1.1")
    installer.login()
    # This app prepared the board, which has since been online at its site.
    first = installer.post("/packages", package_body(
        installer, bundle_id=HIK_BUNDLE, configuration=hik_configuration()))
    assert first.status_code == 201, first.text
    add_connector(installer, MAC, zone_id="LF-ZONE-JHELUM-01", family="hikvision", seen=True)
    response = installer.post("/packages", package_body(
        installer, request_id="installer-test-0002", bundle_id=None,
        configuration=hik_configuration(), mode="change_settings"))
    assert response.status_code == 201, response.text
    assert [(audit.after["mode"], audit.after["firmware_family"], audit.after["bundle_id"])
            for audit in installer.audits("INSTALLER_PACKAGE_ISSUED")] == [
        ("new_board", "hikvision", HIK_BUNDLE), ("change_settings", "hikvision", None)]


# ------------------------------------------------ settings for a locked key


def test_a_board_set_up_before_this_app_keeps_its_settings(installer):
    # ADD has seen the board online and this app never prepared it, so its key
    # may come from an older root: settings from the fleet root could stop it.
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    add_connector(installer, MAC, zone_id="ZONE-TEST-01", online=True)
    installer.login()
    record = installer.get(f"/devices/{MAC}").json()
    assert (record["settings_change_allowed"], record["settings_change_reason"]) == (
        False, SETTINGS_REFUSED)
    for mode in ("change_settings", "resume"):
        response = installer.post("/packages", package_body(installer, mode=mode))
        body = assert_installer_error(response, 409, "SETTINGS_CHANGE_NOT_SUPPORTED")
        assert body["message"] == SETTINGS_REFUSED
    assert installer.provisioner.builds == []
    assert installer.audits("INSTALLER_PACKAGE_ISSUED") == []


def test_a_board_that_never_ran_may_get_new_settings_or_be_resumed(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    # ADD does not know the board, and then knows it only from onboarding:
    # it has never been online, so it holds no punches yet.
    for known in (False, True):
        if known:
            add_connector(installer, MAC, zone_id="ZONE-TEST-01")
        record = installer.get(f"/devices/{MAC}").json()
        assert (record["known"], record["settings_change_allowed"],
                record["settings_change_reason"]) == (known, True, None)
        for mode in ("change_settings", "resume"):
            response = installer.post("/packages", package_body(installer, mode=mode))
            assert response.status_code == 201, response.text
    assert [row.after["mode"] for row in installer.audits("INSTALLER_PACKAGE_ISSUED")] == [
        "change_settings", "resume", "change_settings", "resume"]


@pytest.mark.parametrize("prepared_with", [("new_board",), ("new_board", "resume")],
                         ids=["new_board", "new_board-then-resume"])
def test_a_board_this_app_set_up_may_change_settings_once_online(installer, prepared_with):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    # This app burned the key, so it is the fleet root's for the MAC. Finishing
    # that same setup with a resume changes nothing.
    for number, mode in enumerate(prepared_with, start=1):
        response = installer.post("/packages", package_body(
            installer, mode=mode, request_id=f"installer-test-000{number}"))
        assert response.status_code == 201, response.text
    add_connector(installer, MAC, zone_id="ZONE-TEST-01", online=True)
    record = installer.get("/devices/AC-27-6E-A4-3D-30").json()
    assert (record["settings_change_allowed"], record["settings_change_reason"]) == (True, None)
    changed = installer.post("/packages", package_body(
        installer, mode="change_settings", request_id="installer-test-0008", bundle_id=None))
    assert changed.status_code == 201, changed.text
    # A board that has started is never resumed: only an unfinished setup is.
    resumed = installer.post("/packages", package_body(
        installer, mode="resume", request_id="installer-test-0009"))
    assert_installer_error(resumed, 409, "SETTINGS_CHANGE_NOT_SUPPORTED")
    assert [row.after["mode"] for row in installer.audits("INSTALLER_PACKAGE_ISSUED")] == [
        *prepared_with, "change_settings"]


def test_a_resume_alone_never_proves_the_board_s_key(installer):
    # A resume burns no key. The board may have been locked before this app,
    # with an older root, and then set up again by head office with that root,
    # which ADD does not record (review SV1). Its unsent punches must not be
    # erased by fleet-root settings.
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    installer.login()
    assert installer.post("/packages", package_body(installer, mode="resume")).status_code == 201
    add_connector(installer, MAC, zone_id="ZONE-TEST-01", online=True)
    record = installer.get(f"/devices/{MAC}").json()
    assert (record["settings_change_allowed"], record["settings_change_reason"]) == (
        False, SETTINGS_REFUSED)
    changed = installer.post("/packages", package_body(
        installer, mode="change_settings", request_id="installer-test-0002", bundle_id=None))
    body = assert_installer_error(changed, 409, "SETTINGS_CHANGE_NOT_SUPPORTED")
    assert body["message"] == SETTINGS_REFUSED
    assert len(installer.provisioner.builds) == 1
    assert [row.after["mode"] for row in installer.audits("INSTALLER_PACKAGE_ISSUED")] == [
        "resume"]


def test_only_this_app_s_key_packages_for_the_mac_prove_its_root(installer):
    write_bundle(installer.store, installer.signing_key, ZKT_BUNDLE)
    connector_pk = add_connector(installer, MAC, zone_id="ZONE-TEST-01")
    installer.login()
    # None of these says where this board's key came from: a settings-only
    # package from before it was online, a report from the app, and a new
    # board package for another MAC.
    assert installer.post("/packages", package_body(installer, mode="change_settings")).status_code \
        == 201
    assert installer.post("/events", {
        "request_id": "installer-test-0001", "mac": MAC, "kind": "INSTALL_FINISHED",
        "detail": {"mode": "new_board"},
    }).status_code == 204
    assert installer.post("/packages", package_body(
        installer, mac="ac:27:6e:00:00:99", configuration=zkt_configuration(device_id="2"),
    )).status_code == 201
    with installer.sessions() as db:
        db.get(Connector, connector_pk).last_seen_at = utc_now()
        db.commit()
    record = installer.get(f"/devices/{MAC}").json()
    assert (record["settings_change_allowed"], record["settings_change_reason"]) == (
        False, SETTINGS_REFUSED)
    response = installer.post("/packages", package_body(installer, mode="change_settings"))
    assert_installer_error(response, 409, "SETTINGS_CHANGE_NOT_SUPPORTED")
    assert len(installer.provisioner.builds) == 2
