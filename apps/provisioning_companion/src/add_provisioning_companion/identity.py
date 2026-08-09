from __future__ import annotations

import base64
import ctypes
import json
import os
import platform
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SERVICE = "StateLife.ADD.ProvisioningCompanion"


def application_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = base / "StateLife" / "ADDProvisioningCompanion"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class InstallationIdentity:
    installation_id: str
    private_key: Ed25519PrivateKey  # gitleaks:allow -- type annotation, not key material

    @property
    def public_key_b64(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _windows_protect(value: bytes, *, decrypt: bool = False) -> bytes:
    source_buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
    destination = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    if decrypt:
        ok = operation(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(destination)
        )
    else:
        ok = operation(
            ctypes.byref(source),
            "State Life ADD",
            None,
            None,
            None,
            0,
            ctypes.byref(destination),
        )
    if not ok:
        raise OSError("Windows DPAPI operation failed")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def _keychain_read(installation_id: str) -> bytes | None:
    security = ctypes.CDLL(
        "/System/Library/Frameworks/Security.framework/Security"
    )
    find = security.SecKeychainFindGenericPassword
    find.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    find.restype = ctypes.c_int32
    service = SERVICE.encode("utf-8")
    account = installation_id.encode("utf-8")
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    status = find(
        None,
        len(service),
        service,
        len(account),
        account,
        ctypes.byref(length),
        ctypes.byref(data),
        None,
    )
    if status == -25300:  # errSecItemNotFound
        return None
    if status != 0:
        raise OSError(f"macOS Keychain read failed with status {status}")
    try:
        return ctypes.string_at(data, length.value)
    finally:
        free_content = security.SecKeychainItemFreeContent
        free_content.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        free_content.restype = ctypes.c_int32
        free_content(None, data)


def _keychain_write(installation_id: str, value: bytes) -> None:
    security = ctypes.CDLL(
        "/System/Library/Frameworks/Security.framework/Security"
    )
    add = security.SecKeychainAddGenericPassword
    add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    add.restype = ctypes.c_int32
    service = SERVICE.encode("utf-8")
    account = installation_id.encode("utf-8")
    secret = ctypes.create_string_buffer(value)
    status = add(
        None,
        len(service),
        service,
        len(account),
        account,
        len(value),
        ctypes.cast(secret, ctypes.c_void_p),
        None,
    )
    if status != 0:
        raise OSError(f"macOS Keychain write failed with status {status}")


def load_or_create_identity() -> InstallationIdentity:
    directory = application_data_dir()
    metadata_path = directory / "installation.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        installation_id = str(metadata["installation_id"])
    else:
        installation_id = str(uuid.uuid4())
        metadata_path.write_text(
            json.dumps({"installation_id": installation_id}), encoding="utf-8"
        )
        metadata_path.chmod(0o600)
    system = platform.system()
    if system == "Windows":
        protected_path = directory / "installation-key.dpapi"
        if protected_path.is_file():
            raw = _windows_protect(protected_path.read_bytes(), decrypt=True)
        else:
            raw = Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            protected_path.write_bytes(_windows_protect(raw))
    elif system == "Darwin":
        raw = _keychain_read(installation_id)
        if raw is None:
            raw = Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            _keychain_write(installation_id, raw)
    else:
        if os.environ.get("ADD_COMPANION_ALLOW_INSECURE_DEV_KEY") != "1":
            raise RuntimeError("This companion supports Windows x64 and macOS Apple Silicon only.")
        path = directory / "installation-key.dev"
        if not path.is_file():
            path.write_bytes(
                Ed25519PrivateKey.generate().private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            )
            path.chmod(0o600)
        raw = path.read_bytes()
    return InstallationIdentity(installation_id, Ed25519PrivateKey.from_private_bytes(raw))
