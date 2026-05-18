from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass

from zk_common.security import sign_request, verify_password
from zk_head_office.settings import settings


SESSION_COOKIE = "zk_head_admin"
SESSION_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class AdminSession:
    session_id: str
    expires_at: int
    csrf_token: str


class MemoryRateLimiter:
    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        attempts = [item for item in self._attempts.get(key, []) if item >= window_start]
        if len(attempts) >= self.max_attempts:
            self._attempts[key] = attempts
            return False
        attempts.append(now)
        self._attempts[key] = attempts
        return True


login_rate_limiter = MemoryRateLimiter(max_attempts=5, window_seconds=300)
manual_action_rate_limiter = MemoryRateLimiter(max_attempts=30, window_seconds=60)


def admin_auth_enabled() -> bool:
    return settings.require_admin_auth


def require_auth_config() -> None:
    if not settings.require_admin_auth:
        return
    if not settings.admin_password_hash:
        raise RuntimeError("ZK_HEAD_ADMIN_PASSWORD_HASH is required when admin auth is enabled.")
    if not settings.session_secret:
        raise RuntimeError("ZK_HEAD_SESSION_SECRET is required when admin auth is enabled.")


def verify_admin_login(password: str) -> bool:
    require_auth_config()
    return verify_password(password, settings.admin_password_hash or "")


def make_session() -> tuple[str, AdminSession]:
    secret = _session_secret()
    session_id = secrets.token_urlsafe(24)
    expires_at = int(time.time()) + SESSION_SECONDS
    signature = _session_signature(secret, session_id, expires_at)
    cookie_value = f"{session_id}.{expires_at}.{signature}"
    return cookie_value, AdminSession(session_id, expires_at, csrf_token=_csrf_token(secret, cookie_value))


def parse_session(cookie_value: str | None) -> AdminSession | None:
    if not settings.require_admin_auth or not cookie_value:
        return None
    try:
        session_id, expires_text, signature = cookie_value.split(".", 2)
        expires_at = int(expires_text)
    except ValueError:
        return None
    if expires_at < int(time.time()):
        return None
    secret = _session_secret()
    expected = _session_signature(secret, session_id, expires_at)
    if not hmac.compare_digest(signature, expected):
        return None
    return AdminSession(session_id, expires_at, csrf_token=_csrf_token(secret, cookie_value))


def valid_csrf(cookie_value: str | None, submitted_token: str | None) -> bool:
    admin_session = parse_session(cookie_value)
    if admin_session is None or not submitted_token:
        return False
    return hmac.compare_digest(admin_session.csrf_token, submitted_token)


def _session_secret() -> str:
    require_auth_config()
    return settings.session_secret or ""


def _session_signature(secret: str, session_id: str, expires_at: int) -> str:
    return sign_request(
        token=secret,
        method="SESSION",
        path=session_id,
        timestamp=str(expires_at),
        nonce="admin",
        body_hash="",
    )


def _csrf_token(secret: str, cookie_value: str) -> str:
    return sign_request(
        token=secret,
        method="CSRF",
        path="/",
        timestamp="0",
        nonce=cookie_value,
        body_hash="",
    )
