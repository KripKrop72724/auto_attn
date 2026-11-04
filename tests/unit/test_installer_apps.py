"""Installer app downloads: /api/v1/installer/apps (docs/esp-installer-server.md, "App store").

Requests go through the real app and admin login, on a file-backed database,
with a temporary store laid out as the release workflow publishes it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from zk_add import installer_api, installer_apps, provisioning_api, web
from zk_add.db import Base, create_database_engine
from zk_add.models import AuditEvent
from zk_add.security import hash_admin_password
from zk_add.settings import settings

PREFIX = "/api/v1/installer"
ADMIN_PASSWORD = "installer-apps-test-admin-password"
GIT_SHA = "c" * 40
PUBLISHED_AT = "2026-11-04T09:30:00Z"
SETUP = "StateLife-Device-Installer-Setup-{version}.exe"
DMG = "StateLife-Device-Installer-{version}-macOS-arm64.dmg"


@pytest.fixture(scope="module")
def admin_password_hash():
    return hash_admin_password(ADMIN_PASSWORD)


def file_entries(store: Path, version: str) -> list[dict]:
    rows = []
    for platform, arch, kind, name in (
        ("windows", "x64", "setup", SETUP.format(version=version)),
        ("macos", "arm64", "dmg", DMG.format(version=version)),
    ):
        data = f"{platform} {version} ".encode() * 512
        (store / version / name).write_bytes(data)
        rows.append({"platform": platform, "arch": arch, "kind": kind, "filename": name,
                     "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return rows


def publish(store: Path, version: str, *, mutate=None, raw: bytes | None = None) -> Path:
    """One version directory as the release workflow writes it."""
    directory = store / version
    directory.mkdir(parents=True)
    manifest = {
        "schema": 1, "product": "state-life-device-installer", "version": version,
        "git_sha": GIT_SHA, "published_at": PUBLISHED_AT, "files": file_entries(store, version),
    }
    if mutate:
        mutate(manifest, directory)
    (directory / "manifest.json").write_bytes(
        raw if raw is not None else json.dumps(manifest).encode("utf-8")
    )
    return directory


@dataclass
class Harness:
    client: TestClient
    sessions: sessionmaker
    store: Path
    monkeypatch: object

    def login(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": "StateHealthAdmin", "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200, response.text

    def get(self, path: str):
        return self.client.get(PREFIX + path)

    def latest(self) -> dict | None:
        response = self.get("/apps")
        assert response.status_code == 200, response.text
        return response.json()["latest"]

    def downloads(self) -> list[AuditEvent]:
        with self.sessions() as db:
            return list(db.scalars(
                select(AuditEvent).where(AuditEvent.action == "INSTALLER_APP_DOWNLOADED")
                .order_by(AuditEvent.id)
            ))


@pytest.fixture
def apps(tmp_path, monkeypatch, admin_password_hash):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'installer-apps.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(provisioning_api, "SessionLocal", sessions)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    store = tmp_path / "installer-apps"
    store.mkdir()
    for name, value in {
        "admin_username": "StateHealthAdmin",
        "admin_password_hash": admin_password_hash,
        "admin_cookie_secure": False,
        "provisioning_enabled": True,
        "installer_enabled": True,
        "installer_app_store_path": str(store),
        "installer_min_app_version": "1.0.0",
    }.items():
        monkeypatch.setattr(settings, name, value)
    installer_apps.app_store.clear()
    yield Harness(TestClient(web.app), sessions, store, monkeypatch)
    installer_apps.app_store.clear()
    engine.dispose()


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


# ------------------------------------------------------------------ access


ROUTES = ["/apps", "/apps/1.1.0/" + SETUP.format(version="1.1.0")]


@pytest.mark.parametrize("path", ROUTES)
def test_both_routes_need_the_admin_session(apps, path):
    publish(apps.store, "1.1.0")
    assert_error(apps.get(path), 401, "AUTH_REQUIRED")
    assert apps.downloads() == []


@pytest.mark.parametrize("flag", ["installer_enabled", "provisioning_enabled"])
@pytest.mark.parametrize("path", ROUTES)
def test_the_kill_switch_closes_both_routes(apps, flag, path):
    publish(apps.store, "1.1.0")
    apps.login()
    apps.monkeypatch.setattr(settings, flag, False)
    assert_error(apps.get(path), 409, "INSTALLER_DISABLED")
    assert apps.downloads() == []


# --------------------------------------------------------------- catalogue


def test_a_missing_or_empty_store_has_no_app(apps):
    apps.login()
    assert apps.get("/apps").json() == {"latest": None, "min_app_version": "1.0.0"}
    apps.monkeypatch.setattr(settings, "installer_app_store_path", str(apps.store / "missing"))
    assert apps.get("/apps").json() == {"latest": None, "min_app_version": "1.0.0"}


def test_the_newest_version_is_offered_with_both_platforms(apps):
    for version in ("1.0.0", "1.9.0", "1.10.0"):
        publish(apps.store, version)
    (apps.store / "notes").mkdir()  # not a version: ignored
    apps.monkeypatch.setattr(settings, "installer_min_app_version", "1.1.0")
    apps.login()
    body = apps.get("/apps").json()
    assert body["min_app_version"] == "1.1.0"
    manifest = json.loads((apps.store / "1.10.0" / "manifest.json").read_text(encoding="utf-8"))
    entries = {row["platform"]: row for row in manifest["files"]}
    path = f"{PREFIX}/apps/1.10.0/"
    assert body["latest"] == {
        "version": "1.10.0", "git_sha": GIT_SHA, "published_at": PUBLISHED_AT,
        "files": [
            {**entries["windows"],
             "download_path": path + "StateLife-Device-Installer-Setup-1.10.0.exe"},
            {**entries["macos"],
             "download_path": path + "StateLife-Device-Installer-1.10.0-macOS-arm64.dmg"},
        ],
    }


def test_a_version_may_offer_one_platform(apps):
    def windows_only(manifest, directory):
        manifest["files"] = manifest["files"][:1]
    publish(apps.store, "1.1.0", mutate=windows_only)
    apps.login()
    assert [row["platform"] for row in apps.latest()["files"]] == ["windows"]


def test_the_macos_file_may_be_listed_first(apps):
    publish(apps.store, "1.1.0", mutate=lambda manifest, _directory: manifest["files"].reverse())
    apps.login()
    assert [row["platform"] for row in apps.latest()["files"]] == ["windows", "macos"]


def _set(key, value):
    def change(manifest, _directory):
        manifest[key] = value
    return change


def _set_file(index, key, value):
    def change(manifest, _directory):
        manifest["files"][index][key] = value
    return change


def _drop(key):
    def change(manifest, _directory):
        manifest.pop(key)
    return change


def _rename(index, name):
    def change(manifest, directory):
        entry = manifest["files"][index]
        if "/" not in name:  # a path in the name cannot be created in the directory
            (directory / entry["filename"]).rename(directory / name)
        entry["filename"] = name
    return change


def _corrupt(index):
    def change(manifest, directory):
        path = directory / manifest["files"][index]["filename"]
        data = bytearray(path.read_bytes())
        data[0] ^= 1
        path.write_bytes(bytes(data))
    return change


def _remove(index):
    def change(manifest, directory):
        (directory / manifest["files"][index]["filename"]).unlink()
    return change


INVALID = [
    ("schema as text", _set("schema", "1"), "the manifest schema is not 1"),
    ("schema true", _set("schema", True), "the manifest schema is not 1"),
    ("schema 2", _set("schema", 2), "the manifest schema is not 1"),
    ("no product", _drop("product"), "the manifest is for another product"),
    ("other product", _set("product", "zone-lite-companion"),
     "the manifest is for another product"),
    ("other version", _set("version", "1.0.9"), "version does not match its directory"),
    ("short commit", _set("git_sha", "c" * 39), "git_sha is not a full commit"),
    ("upper-case commit", _set("git_sha", "C" * 40), "git_sha is not a full commit"),
    ("no time zone", _set("published_at", "2026-11-04T09:30:00"), "published_at is not a UTC time"),
    ("offset", _set("published_at", "2026-11-04T09:30:00+05:00"), "published_at is not a UTC time"),
    ("impossible date", _set("published_at", "2026-02-30T09:30:00Z"),
     "published_at is not a UTC time"),
    ("no files", _set("files", []), "the manifest lists no files"),
    ("files not a list", _set("files", {"windows": {}}), "the manifest lists no files"),
    ("entry not an object", _set("files", ["x.exe"]), "a file entry is not an object"),
    ("unknown platform", _set_file(0, "platform", "linux"), "a file is for an unknown platform"),
    ("unhashable platform", _set_file(0, "platform", ["windows"]),
     "a file is for an unknown platform"),
    ("arm Windows", _set_file(0, "arch", "arm64"), "the windows file is not x64 setup"),
    ("Intel Mac", _set_file(1, "arch", "x64"), "the macos file is not arm64 dmg"),
    ("zip for Mac", _set_file(1, "kind", "zip"), "the macos file is not arm64 dmg"),
    ("platform twice", _set_file(1, "platform", "windows"), "windows is listed twice"),
    ("path in the name", _rename(0, "../StateLife-Device-Installer-Setup-1.1.0.exe"),
     "the windows file name is not allowed"),
    ("hidden name", _rename(0, ".StateLife-Device-Installer-Setup-1.1.0.exe"),
     "the windows file name is not allowed"),
    ("space in the name", _rename(0, "StateLife Device Installer.exe"),
     "the windows file name is not allowed"),
    ("too long a name", _rename(0, "S" * 97 + ".exe"), "the windows file name is not allowed"),
    ("wrong extension", _rename(1, "StateLife-Device-Installer-1.1.0-macOS-arm64.zip"),
     "the macos file name is not allowed"),
    ("TEST build", _rename(0, "StateLife-Device-Installer-Setup-1.1.0-TEST.exe"),
     "StateLife-Device-Installer-Setup-1.1.0-TEST.exe is a TEST build"),
    ("size as text", _set_file(0, "size", "4096"),
     "StateLife-Device-Installer-Setup-1.1.0.exe has no valid size"),
    ("size true", _set_file(0, "size", True),
     "StateLife-Device-Installer-Setup-1.1.0.exe has no valid size"),
    ("empty file", _set_file(0, "size", 0),
     "StateLife-Device-Installer-Setup-1.1.0.exe has no valid size"),
    ("other size", _set_file(1, "size", 1),
     "StateLife-Device-Installer-1.1.0-macOS-arm64.dmg does not have the listed size"),
    ("upper-case digest", _set_file(0, "sha256", "A" * 64),
     "StateLife-Device-Installer-Setup-1.1.0.exe has no SHA-256"),
    ("short digest", _set_file(0, "sha256", "a" * 63),
     "StateLife-Device-Installer-Setup-1.1.0.exe has no SHA-256"),
    ("changed bytes", _corrupt(1),
     "StateLife-Device-Installer-1.1.0-macOS-arm64.dmg does not match its SHA-256"),
    ("missing file", _remove(0), "StateLife-Device-Installer-Setup-1.1.0.exe is missing"),
    ("files null", _set("files", None), "the manifest lists no files"),
]


@pytest.mark.parametrize(("change", "reason"), [row[1:] for row in INVALID],
                         ids=[row[0] for row in INVALID])
def test_an_invalid_version_is_skipped_logged_once_and_never_served(apps, caplog, change, reason):
    publish(apps.store, "1.0.0")
    publish(apps.store, "1.1.0", mutate=change)
    apps.login()
    caplog.set_level(logging.WARNING)
    for _attempt in range(2):
        assert apps.latest()["version"] == "1.0.0"
    assert [record.getMessage() for record in caplog.records if "1.1.0" in record.getMessage()] \
        == [f"Installer app 1.1.0 skipped: {reason}"]
    for name in os.listdir(apps.store / "1.1.0"):
        if name != "manifest.json":
            assert_error(apps.get(f"/apps/1.1.0/{name}"), 404, "FILE_NOT_FOUND")
    assert apps.downloads() == []


RAW = [
    ("byte order mark", b"\xef\xbb\xbf{}", "manifest.json starts with a byte order mark"),
    ("not UTF-8", b'{"product": "\xff"}', "manifest.json is not UTF-8 JSON"),
    ("not JSON", b"schema: 1", "manifest.json is not UTF-8 JSON"),
    ("an array", b"[]", "the manifest is not an object"),
    ("a repeated key", b'{"schema": 1, "schema": 1}', "the manifest repeats a key"),
    ("too large", b" " * (64 * 1024 + 1), "manifest.json is too large"),
]


@pytest.mark.parametrize(("raw", "reason"), [row[1:] for row in RAW], ids=[row[0] for row in RAW])
def test_an_unreadable_manifest_is_skipped(apps, caplog, raw, reason):
    publish(apps.store, "1.1.0", raw=raw)
    apps.login()
    caplog.set_level(logging.WARNING)
    assert apps.latest() is None
    assert f"Installer app 1.1.0 skipped: {reason}" in caplog.text


def test_a_manifest_may_add_keys_and_fractions_of_a_second(apps):
    def extra(manifest, _directory):
        manifest["notes"] = "Adds the Mac app."
        manifest["files"][0]["signed"] = False
        manifest["published_at"] = "2026-11-04T09:30:00.125Z"
    publish(apps.store, "1.1.0", mutate=extra)
    apps.login()
    latest = apps.latest()
    assert (latest["version"], latest["published_at"]) == ("1.1.0", "2026-11-04T09:30:00.125Z")
    assert "notes" not in latest and "signed" not in latest["files"][0]


@pytest.mark.parametrize(
    "name", ["1.1", "1.1.0-rc1", "v1.1.0", "1.1.0.0", "01.1.0x", "1" * 31 + ".0.0"]
)
def test_a_directory_that_is_not_a_version_is_ignored(apps, caplog, name):
    publish(apps.store, "1.0.0")
    os.rename(publish(apps.store, "1.1.0"), apps.store / name)
    apps.login()
    caplog.set_level(logging.WARNING)
    assert apps.latest()["version"] == "1.0.0"
    assert "Installer app" not in caplog.text


# ---------------------------------------------------------------- symlinks


def test_a_version_directory_that_is_a_symlink_is_skipped(apps, tmp_path, caplog):
    publish(apps.store, "1.0.0")
    outside = tmp_path / "outside"
    publish(outside, "1.1.0")
    os.symlink(outside / "1.1.0", apps.store / "1.1.0")
    apps.login()
    caplog.set_level(logging.WARNING)
    assert apps.latest()["version"] == "1.0.0"
    assert_error(apps.get(f"/apps/1.1.0/{SETUP.format(version='1.1.0')}"), 404, "FILE_NOT_FOUND")
    assert "Installer app 1.1.0 skipped: it is not a directory inside the store" in caplog.text


@pytest.mark.parametrize("name", ["manifest.json", SETUP.format(version="1.1.0")])
def test_a_file_that_is_a_symlink_is_skipped(apps, tmp_path, caplog, name):
    publish(apps.store, "1.0.0")
    directory = publish(apps.store, "1.1.0")
    outside = tmp_path / "outside-file"
    os.rename(directory / name, outside)
    os.symlink(outside, directory / name)
    apps.login()
    caplog.set_level(logging.WARNING)
    assert apps.latest()["version"] == "1.0.0"
    assert f"Installer app 1.1.0 skipped: {name} is not a regular file" in caplog.text


def test_a_symlink_loop_is_skipped(apps, caplog):
    publish(apps.store, "1.0.0")
    os.symlink(apps.store / "1.1.0", apps.store / "1.1.0")
    apps.login()
    caplog.set_level(logging.WARNING)
    for _attempt in range(2):
        assert apps.latest()["version"] == "1.0.0"
    messages = [record.getMessage() for record in caplog.records]
    assert [message for message in messages if "Installer app" in message] \
        == ["Installer app 1.1.0 skipped: it is not a directory inside the store"]


# ----------------------------------------------------------------- hashing


def test_each_file_is_hashed_once_until_it_changes(apps, caplog):
    directory = publish(apps.store, "1.1.0")
    hashed = []
    original = installer_apps._sha256_file

    def counting(path):
        hashed.append(path.name)
        return original(path)

    apps.monkeypatch.setattr(installer_apps, "_sha256_file", counting)
    apps.login()
    setup = SETUP.format(version="1.1.0")
    for _attempt in range(3):
        assert apps.latest()["version"] == "1.1.0"
        assert apps.get(f"/apps/1.1.0/{setup}").status_code == 200
    assert sorted(hashed) == sorted([setup, DMG.format(version="1.1.0")])
    # Same size, new bytes and a new modification time: checked again, and refused.
    path = directory / setup
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))
    status = path.stat()
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))
    caplog.set_level(logging.WARNING)
    assert apps.latest() is None
    assert hashed.count(setup) == 2
    assert_error(apps.get(f"/apps/1.1.0/{setup}"), 404, "FILE_NOT_FOUND")
    assert f"Installer app 1.1.0 skipped: {setup} does not match its SHA-256" in caplog.text


def test_older_versions_are_hashed_only_when_needed(apps):
    for version in ("1.0.0", "1.1.0"):
        publish(apps.store, version)
    hashed = []
    original = installer_apps._sha256_file
    apps.monkeypatch.setattr(
        installer_apps, "_sha256_file",
        lambda path: hashed.append(path.parent.name) or original(path),
    )
    apps.login()
    assert apps.latest()["version"] == "1.1.0"
    assert set(hashed) == {"1.1.0"}


# --------------------------------------------------------------- downloads


@pytest.mark.parametrize(("filename", "media_type"), [
    (SETUP.format(version="1.1.0"), "application/vnd.microsoft.portable-executable"),
    (DMG.format(version="1.1.0"), "application/x-apple-diskimage"),
])
def test_a_download_is_streamed_as_an_attachment_and_audited(apps, filename, media_type):
    directory = publish(apps.store, "1.1.0")
    apps.login()
    response = apps.get(f"/apps/1.1.0/{filename}")
    assert response.status_code == 200, response.text
    data = (directory / filename).read_bytes()
    assert response.content == data
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert response.headers["content-length"] == str(len(data))
    assert response.headers["x-content-sha256"] == hashlib.sha256(data).hexdigest()
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    (row,) = apps.downloads()
    platform, arch = ("windows", "x64") if filename.endswith(".exe") else ("macos", "arm64")
    assert (row.actor, row.target_type, row.target_id, row.outcome) == (
        "StateHealthAdmin", "installer_app", "1.1.0", "SUCCESS")
    assert row.after == {
        "version": "1.1.0", "platform": platform, "arch": arch, "filename": filename,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_an_older_valid_version_can_still_be_downloaded(apps):
    for version in ("1.0.0", "1.1.0"):
        publish(apps.store, version)
    apps.login()
    response = apps.get(f"/apps/1.0.0/{DMG.format(version='1.0.0')}")
    assert response.status_code == 200, response.text
    assert [row.target_id for row in apps.downloads()] == ["1.0.0"]


@pytest.mark.parametrize("path", [
    "/apps/1.2.0/" + SETUP.format(version="1.2.0"),  # no such version
    "/apps/1.1.0/manifest.json",  # in the directory, but not listed
    "/apps/1.1.0/" + SETUP.format(version="1.0.0"),  # another version's name
    "/apps/1.1.0/" + SETUP.format(version="1.1.0").upper(),
    "/apps/latest/" + SETUP.format(version="1.1.0"),
    "/apps/1.1.0%2F..%2F1.0.0/" + SETUP.format(version="1.0.0"),
    "/apps/..%2F1.1.0/" + SETUP.format(version="1.1.0"),
    "/apps/1.1.0/..%2Fmanifest.json",
])
def test_anything_unlisted_is_not_found_and_not_audited(apps, path):
    publish(apps.store, "1.1.0")
    (apps.store / "1.0.0").mkdir()
    apps.login()
    response = apps.get(path)
    assert response.status_code == 404, response.text
    assert response.json()["code"] in {"FILE_NOT_FOUND", "NOT_FOUND"}
    assert apps.downloads() == []


def test_a_download_holds_no_database_transaction(apps):
    publish(apps.store, "1.1.0")
    apps.login()
    opened = []

    def tracked_session():
        opened.append(apps.sessions())
        return opened[-1]

    apps.monkeypatch.setattr(provisioning_api, "SessionLocal", tracked_session)
    during_transfer = []

    class WatchedFileResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            during_transfer.extend(session.in_transaction() for session in opened)
            await super().__call__(scope, receive, send)

    apps.monkeypatch.setattr(installer_api, "FileResponse", WatchedFileResponse)
    response = apps.get(f"/apps/1.1.0/{SETUP.format(version='1.1.0')}")
    assert response.status_code == 200, response.text
    assert during_transfer == [False]
    assert len(apps.downloads()) == 1
