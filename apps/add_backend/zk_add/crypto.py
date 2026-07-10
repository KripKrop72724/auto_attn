from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import Fernet

from zk_add.settings import settings


def normalize_cnic(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    return digits if len(digits) == 13 else None


def encrypt_cnic(value: str | None) -> str | None:
    normalized = normalize_cnic(value)
    if normalized is None:
        return None
    if not settings.pii_fernet_key:
        raise RuntimeError("ADD_PII_FERNET_KEY is required to store CNIC data.")
    return Fernet(settings.pii_fernet_key.encode()).encrypt(normalized.encode()).decode()


def decrypt_cnic(value: str | None) -> str | None:
    if not value:
        return None
    if not settings.pii_fernet_key:
        return None
    return Fernet(settings.pii_fernet_key.encode()).decrypt(value.encode()).decode()


def cnic_lookup(value: str | None) -> str | None:
    normalized = normalize_cnic(value)
    if normalized is None:
        return None
    if not settings.pii_lookup_key:
        raise RuntimeError("ADD_PII_LOOKUP_KEY is required to index CNIC data.")
    return hmac.new(
        settings.pii_lookup_key.encode(), normalized.encode(), hashlib.sha256
    ).hexdigest()


def mask_cnic(value: str | None) -> str | None:
    normalized = normalize_cnic(value)
    return None if normalized is None else f"*****-*******-{normalized[-1]}"

