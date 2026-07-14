from __future__ import annotations

import hashlib
import hmac
import json

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
    if normalized is None:
        return None
    # An authenticated operator needs enough information to distinguish two
    # terminal identities without receiving the full CNIC.  Showing only the
    # check digit produced ten visually identical values and made unrelated
    # users look duplicated.  Keep nine digits hidden and expose the final
    # four in the familiar 5-7-1 CNIC layout.
    return f"*****-****{normalized[-4:-1]}-{normalized[-1]}"


def encrypt_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not settings.pii_fernet_key:
        raise RuntimeError("ADD_PII_FERNET_KEY is required to store protected data.")
    return Fernet(settings.pii_fernet_key.encode()).encrypt(value.encode("utf-8")).decode()


def decrypt_text(value: str | None) -> str | None:
    if not value or not settings.pii_fernet_key:
        return None
    return Fernet(settings.pii_fernet_key.encode()).decrypt(value.encode()).decode("utf-8")


def encrypt_json(value: dict | None) -> str:
    material = json.dumps(value or {}, separators=(",", ":"), sort_keys=True)
    return encrypt_text(material) or ""


def decrypt_json(value: str | None) -> dict:
    material = decrypt_text(value)
    if not material:
        return {}
    decoded = json.loads(material)
    return decoded if isinstance(decoded, dict) else {}
