"""``--self-test``: checks the installed app can do its job, without touching a board.

Each problem is one line of text; an empty list means the app is healthy. The
Windows release smoke test runs this after a silent install.
"""
from __future__ import annotations

import gzip
import hashlib
import sys
import tempfile
from typing import List, Optional

from ..config import AppConfig, load_config, load_field_defaults
from .bundles import STORAGE_EMPTY_SHA256, load_public_key
from .esptool_runner import SubprocessRunner, ToolRunner, tool_version_problems


def run(cfg: Optional[AppConfig] = None, runner: Optional[ToolRunner] = None) -> List[str]:
    problems: List[str] = []
    if sys.version_info < (3, 9):
        problems.append(f"Python {sys.version.split()[0]} is older than 3.9")
    config = cfg if cfg is not None else load_config()

    try:
        load_public_key(config.signing_key_path)
    except Exception as error:
        problems.append(f"pinned signing key unusable: {error}")

    try:
        digest = hashlib.sha256()
        size = 0
        with gzip.open(config.storage_image_path, "rb") as image:
            for chunk in iter(lambda: image.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != 0x800000 or digest.hexdigest() != STORAGE_EMPTY_SHA256:
            problems.append("embedded empty storage image does not match its pinned hash")
    except OSError as error:
        problems.append(f"embedded empty storage image unreadable: {error}")

    try:
        defaults = load_field_defaults(config.field_defaults_path)
        if not defaults.is_test_build and not (defaults.wifi_password
                                               and defaults.setup_ap_password):
            problems.append("release field defaults lack the Wi-Fi or hotspot password")
    except ValueError as error:
        problems.append(f"field defaults: {error}")

    try:
        config.ensure_dirs()
        with tempfile.NamedTemporaryFile(dir=str(config.work_dir)):
            pass
    except OSError as error:
        problems.append(f"data folder not writable: {error}")

    problems.extend(_library_problems())
    problems.extend(tool_problems(config, runner))
    return problems


def _library_problems() -> List[str]:
    problems: List[str] = []
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        left, right = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        if left.exchange(right.public_key()) != right.exchange(left.public_key()):
            problems.append("X25519 key exchange is broken")
        key = AESGCM.generate_key(bit_length=256)
        if AESGCM(key).decrypt(b"\0" * 12, AESGCM(key).encrypt(b"\0" * 12, b"x", b""), b"") \
                != b"x":
            problems.append("AES-GCM is broken")
    except Exception as error:
        problems.append(f"cryptography unavailable: {error}")
    try:
        import httpx  # noqa: F401

        from .add_client import tls_context
        tls_context()
    except Exception as error:
        problems.append(f"HTTPS support unavailable: {error}")
    try:
        from serial.tools import list_ports
        list_ports.comports()
    except Exception as error:
        problems.append(f"USB serial support unavailable: {error}")
    return problems


def tool_problems(config: AppConfig, runner: Optional[ToolRunner] = None) -> List[str]:
    """The tools the engine would run, checked the way it runs them (no port is opened)."""
    tools = runner or SubprocessRunner(config.work_dir / "selftest", frozen=config.frozen,
                                       executable=config.tool_python)
    return [f"bundled {problem}" for problem in tool_version_problems(tools)]
