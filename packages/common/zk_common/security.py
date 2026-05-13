from __future__ import annotations

import hmac

from zk_common.hashing import sha256_hex


def token_hash(token: str) -> str:
    return sha256_hex(token)


def verify_token(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(token_hash(token), expected_hash)
