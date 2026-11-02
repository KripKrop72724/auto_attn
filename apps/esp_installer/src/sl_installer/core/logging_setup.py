"""Logging with secrets removed before anything reaches a file.

Passwords, comm keys, security keys, tokens, cookies and encoded blobs must never
be written to disk (design section 4.8). Redaction works on three levels:

* values that the engine registered as secret at run time (the sign-in password,
  Wi-Fi and attendance-machine passwords) are removed wherever they appear;
* ``name=value`` / ``"name": "value"`` / ``Name: value`` pairs whose name looks
  secret are blanked;
* long base64-looking blobs (envelopes, keys) are blanked.

Hex digests (64 hex characters and shorter) are kept: they identify firmware and
are not secret.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from pathlib import Path
from typing import Iterable, List, Optional

LOGGER_NAME = "sl_installer"
LOG_FILE_NAME = "installer.log"
REDACTED = "[redacted]"

_SECRET_NAMES = (
    r"password|passwd|pass|pwd|secret|token|cookie|csrf|x-csrf-token|authorization|"
    r"comm[_ -]?key|zkt_comm_key|zkt_key|commkey|hmac[_ -]?key|private[_ -]?key|api[_ -]?key|"
    r"wifi_password|wifi_pass|hik_password|hik_pass|setup_ap_password|boot_secret|"
    r"dev_token|device_token|ords_pass|add_admin|set-cookie|ciphertext|"
    r"provisioning_ciphertext_b64|hmac_ciphertext_b64|recipient_public_key"
)
# "name": "value"  (JSON), with the value in quotes.
_JSON_PAIR = re.compile(
    r'("(?:[A-Za-z0-9_-]*?)(?:' + _SECRET_NAMES + r')(?:[A-Za-z0-9_-]*?)"\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.IGNORECASE)
# name=value, name: value, "name": 1234 and header lines; the value stops at a space,
# ; , & or quote.
_PLAIN_PAIR = re.compile(
    r"(\b(?:[A-Za-z0-9_-]*?)(?:" + _SECRET_NAMES + r")(?:[A-Za-z0-9_-]*?)\b\"?\s*[=:]\s*)"
    r"(?:Bearer\s+)?[^\s;,&'\"]+",
    re.IGNORECASE)
# Base64 / base64url runs of 40+ characters. Hex digests, upper-case codes and file
# paths look similar but are kept (see _blob).
_BLOB = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_=-])")
_HEX_ONLY = re.compile(r"[0-9a-fA-F]+")
_CHAR_CLASSES = (re.compile("[a-z]"), re.compile("[A-Z]"), re.compile("[0-9]"))

_lock = threading.Lock()
_registered: List[str] = []


def register_secret(value: Optional[str]) -> None:
    """Remember a secret value so it is removed from every later log line.

    Short values are ignored: blanking every "1979" or "admin" would make logs
    unreadable, and those are caught by the name=value rules instead.
    """
    if not value or len(value) < 6:
        return
    with _lock:
        if value not in _registered:
            _registered.append(value)
            _registered.sort(key=len, reverse=True)


def forget_secrets() -> None:
    with _lock:
        _registered.clear()


def _blob(match: "re.Match[str]") -> str:
    text = match.group(0)
    core = text.rstrip("=")
    if _HEX_ONLY.fullmatch(core) and len(core) <= 64:
        return text
    # Random base64 mixes character classes and has a "/" only every ~64 characters;
    # paths have one every few characters and codes such as
    # STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED use one class.
    classes = sum(1 for pattern in _CHAR_CLASSES if pattern.search(core))
    if classes < 2 or core.count("/") * 20 > len(core):
        return text
    return REDACTED


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with every secret-looking part replaced by [redacted]."""
    if not text:
        return text
    with _lock:
        secrets = list(_registered)
    for value in list(extra_secrets) + secrets:
        if value and len(value) >= 6:
            text = text.replace(value, REDACTED)
    text = _JSON_PAIR.sub(lambda m: m.group(1) + '"' + REDACTED + '"', text)
    text = _PLAIN_PAIR.sub(lambda m: m.group(1) + REDACTED, text)
    return _BLOB.sub(_blob, text)


class RedactingFilter(logging.Filter):
    """Formats each record once and stores only the redacted text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not lose the record
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.msg = f"{record.msg}\n{redact(formatted)}"
            record.exc_info = None
            record.exc_text = None
        return True


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def setup_logging(log_dir: Path, level: int = logging.INFO,
                  max_bytes: int = 2 * 1024 * 1024, backups: int = 5) -> Path:
    """Send the app's logs to a rotating, redacted file in ``log_dir``."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_FILE_NAME
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if getattr(handler, "_sl_installer", False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    handler._sl_installer = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.propagate = False
    return path


def log_files(log_dir: Path) -> List[Path]:
    """The current log and its rotated copies, newest first."""
    if not log_dir.is_dir():
        return []
    return sorted(log_dir.glob(LOG_FILE_NAME + "*"), key=lambda p: p.name)
