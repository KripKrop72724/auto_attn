"""State Life Device Installer downloads: the app itself, served by ADD.

Release builds of the app (the Windows Setup and the Mac disk image) are
published into a read-only store, one directory per version (design in
docs/esp-installer-server.md, "App store"):

    <store>/<version>/manifest.json   plus every file the manifest lists

ADD offers the highest version whose manifest and files all check out. Each
file's SHA-256 is computed once and reused until the file's size or
modification time changes, so the catalogue never re-reads hundreds of
megabytes per request. A bad version is logged once and left out; it never
fails the others.
"""
from __future__ import annotations

import codecs
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from zk_add.settings import settings

logger = logging.getLogger(__name__)

PRODUCT = "state-life-device-installer"
SCHEMA = 1
MANIFEST_NAME = "manifest.json"
MANIFEST_MAX_BYTES = 64 * 1024
FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024
# Directory names and file names are the only parts of a path ever taken from
# the store or a request; ASCII classes only (\d would accept other digits).
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
VERSION_MAX_LENGTH = 32
FILENAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
FILENAME_MAX_LENGTH = 100
GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PUBLISHED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
)
# A TEST build (no field defaults, TEST BUILD banner) must never reach a site.
TEST_BUILD_PATTERN = re.compile(r"-TEST(?:\.|-)", re.IGNORECASE)
# Each platform has exactly one file per version, in this order.
PLATFORMS = {
    "windows": ("x64", "setup", ".exe"),
    "macos": ("arm64", "dmg", ".dmg"),
}
MEDIA_TYPES = {
    "setup": "application/vnd.microsoft.portable-executable",
    "dmg": "application/x-apple-diskimage",
}


class AppInvalid(Exception):
    """A skipped version; the reason is a fixed phrase, never file content."""


@dataclass(frozen=True)
class AppFile:
    platform: str
    arch: str
    kind: str
    filename: str
    size: int
    sha256: str
    path: Path

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.kind]


@dataclass(frozen=True)
class AppRelease:
    version: str
    git_sha: str
    published_at: str
    files: tuple[AppFile, ...]

    def file(self, filename: str) -> AppFile | None:
        return next((item for item in self.files if item.filename == filename), None)


def valid_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= VERSION_MAX_LENGTH
        and VERSION_PATTERN.fullmatch(value) is not None
    )


def valid_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= FILENAME_MAX_LENGTH
        and not value.startswith(".")
        and FILENAME_PATTERN.fullmatch(value) is not None
    )


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(directory: Path, name: str) -> os.stat_result:
    """A regular file directly in the version directory, never a symlink."""
    try:
        status = os.lstat(directory / name)
    except FileNotFoundError:
        raise AppInvalid(f"{name} is missing") from None
    if not stat.S_ISREG(status.st_mode):
        raise AppInvalid(f"{name} is not a regular file")
    return status


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise AppInvalid("the manifest repeats a key")
        result[key] = value
    return result


def _read_manifest(directory: Path) -> dict:
    if _regular_file(directory, MANIFEST_NAME).st_size > MANIFEST_MAX_BYTES:
        raise AppInvalid("manifest.json is too large")
    data = (directory / MANIFEST_NAME).read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        raise AppInvalid("manifest.json starts with a byte order mark")
    try:
        manifest = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, ValueError):
        raise AppInvalid("manifest.json is not UTF-8 JSON") from None
    if not isinstance(manifest, dict):
        raise AppInvalid("the manifest is not an object")
    return manifest


def _published_at(value: object) -> str:
    if not isinstance(value, str) or not PUBLISHED_AT_PATTERN.fullmatch(value):
        raise AppInvalid("published_at is not a UTC time")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise AppInvalid("published_at is not a UTC time") from None
    return value


def _listed_file(entry: object, seen: set[str]) -> tuple[str, str, str, str, int, str]:
    if not isinstance(entry, dict):
        raise AppInvalid("a file entry is not an object")
    platform = entry.get("platform")
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise AppInvalid("a file is for an unknown platform")
    if platform in seen:
        raise AppInvalid(f"{platform} is listed twice")
    seen.add(platform)
    arch, kind, extension = PLATFORMS[platform]
    if entry.get("arch") != arch or entry.get("kind") != kind:
        raise AppInvalid(f"the {platform} file is not {arch} {kind}")
    filename = entry.get("filename")
    if not valid_filename(filename) or not filename.endswith(extension):
        raise AppInvalid(f"the {platform} file name is not allowed")
    if TEST_BUILD_PATTERN.search(filename):
        raise AppInvalid(f"{filename} is a TEST build")
    size, digest = entry.get("size"), entry.get("sha256")
    if type(size) is not int or not 0 < size <= FILE_MAX_BYTES:
        raise AppInvalid(f"{filename} has no valid size")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise AppInvalid(f"{filename} has no SHA-256")
    return platform, arch, kind, filename, size, digest


def check_release(
    directory: Path, version: str, digest_of: Callable[[Path, os.stat_result], str]
) -> AppRelease:
    """Validate one version directory; digest_of hashes a file (with caching)."""
    manifest = _read_manifest(directory)
    schema = manifest.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        raise AppInvalid("the manifest schema is not 1")
    if manifest.get("product") != PRODUCT:
        raise AppInvalid("the manifest is for another product")
    if manifest.get("version") != version:
        raise AppInvalid("version does not match its directory")
    git_sha = manifest.get("git_sha")
    if not isinstance(git_sha, str) or not GIT_SHA_PATTERN.fullmatch(git_sha):
        raise AppInvalid("git_sha is not a full commit")
    published_at = _published_at(manifest.get("published_at"))
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise AppInvalid("the manifest lists no files")
    seen: set[str] = set()
    listed = [_listed_file(entry, seen) for entry in entries]
    # Every cheap check passes before any file is hashed.
    statuses = []
    for _platform, _arch, _kind, filename, size, _digest in listed:
        status = _regular_file(directory, filename)
        if status.st_size != size:
            raise AppInvalid(f"{filename} does not have the listed size")
        statuses.append(status)
    files = []
    for (platform, arch, kind, filename, size, digest), status in zip(listed, statuses):
        path = directory / filename
        if not hmac.compare_digest(digest_of(path, status), digest):
            raise AppInvalid(f"{filename} does not match its SHA-256")
        files.append(AppFile(platform, arch, kind, filename, size, digest, path))
    order = list(PLATFORMS)
    files.sort(key=lambda item: order.index(item.platform))
    return AppRelease(version, git_sha, published_at, tuple(files))


class AppStore:
    """Installer app releases under settings.installer_app_store_path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # path -> (size, mtime_ns, sha256): a file is hashed again only when it changes.
        self._digests: dict[str, tuple[int, int, str]] = {}
        self._skipped: dict[str, str] = {}  # version -> the reason already logged

    @staticmethod
    def _root() -> Path | None:
        try:
            root = Path(settings.installer_app_store_path).resolve()
        except (OSError, RuntimeError):
            return None
        return root if root.is_dir() else None

    def _digest(self, path: Path, status: os.stat_result) -> str:
        cached = self._digests.get(str(path))
        if cached is not None and cached[:2] == (status.st_size, status.st_mtime_ns):
            return cached[2]
        digest = _sha256_file(path)
        after = os.lstat(path)
        if (after.st_size, after.st_mtime_ns) != (status.st_size, status.st_mtime_ns):
            raise AppInvalid(f"{path.name} changed while it was checked")
        self._digests[str(path)] = (status.st_size, status.st_mtime_ns, digest)
        return digest

    def _load_locked(self, root: Path, version: str) -> AppRelease | None:
        directory = root / version
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise AppInvalid("it is not a directory inside the store")
            release = check_release(directory, version, self._digest)
        except FileNotFoundError:
            if not os.path.lexists(directory):
                return None  # it left the store, or never was there
            reason = "it cannot be read"
        except AppInvalid as error:
            reason = str(error)
        except OSError:
            reason = "it cannot be read"
        except Exception as error:  # one unreadable version never fails the others
            reason = type(error).__name__
        else:
            self._skipped.pop(version, None)
            return release
        if self._skipped.get(version) != reason:
            self._skipped[version] = reason
            logger.warning("Installer app %s skipped: %s", version, reason)
        return None

    def latest(self) -> AppRelease | None:
        """The highest valid version; a store that is missing or empty has none."""
        root = self._root()
        if root is None:
            return None
        with self._lock:
            try:
                versions = [name for name in os.listdir(root) if valid_version(name)]
            except OSError:
                logger.warning("Installer app store is unreadable")
                return None
            present = set(versions)
            self._skipped = {key: value for key, value in self._skipped.items() if key in present}
            self._digests = {
                key: value for key, value in self._digests.items()
                if Path(key).parent.parent == root and Path(key).parent.name in present
            }
            # Newest first; an older version is hashed only when every newer one fails.
            newest_first = sorted(
                versions, key=lambda name: (version_key(name), name), reverse=True
            )
            for version in newest_first:
                release = self._load_locked(root, version)
                if release is not None:
                    return release
        return None

    def get(self, version: str) -> AppRelease | None:
        if not valid_version(version):
            return None
        root = self._root()
        if root is None:
            return None
        with self._lock:
            return self._load_locked(root, version)

    def clear(self) -> None:
        with self._lock:
            self._digests.clear()
            self._skipped.clear()


app_store = AppStore()
