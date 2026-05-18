from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.security import password_hash, sign_request, verify_password
from zk_common.time_utils import utc_now
from zk_zone_agent.crypto import protect_secret, unprotect_secret
from zk_zone_agent.db import LocalAdmin, ServiceEvent


SESSION_COOKIE = "zk_zone_admin"
SESSION_SECONDS = 8 * 60 * 60
MIN_ADMIN_PASSWORD_LENGTH = 8
DISABLED_PASSWORD_HASH = "disabled$recovery-password-not-configured"


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
setup_rate_limiter = MemoryRateLimiter(max_attempts=10, window_seconds=300)
manual_action_rate_limiter = MemoryRateLimiter(max_attempts=30, window_seconds=60)


def admin_exists(session: Session) -> bool:
    return session.scalar(select(LocalAdmin).where(LocalAdmin.id == 1)) is not None


def get_admin(session: Session) -> LocalAdmin | None:
    return session.scalar(select(LocalAdmin).where(LocalAdmin.id == 1))


def recovery_password_configured(admin: LocalAdmin | None) -> bool:
    return bool(admin and admin.password_hash != DISABLED_PASSWORD_HASH)


def admin_has_recovery_password(session: Session) -> bool:
    return recovery_password_configured(get_admin(session))


def create_admin(session: Session, password: str | None = None) -> LocalAdmin:
    password = password or ""
    if password and len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise ValueError(f"Admin password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters.")
    existing = get_admin(session)
    if existing is not None:
        raise ValueError("Local admin is already configured.")
    row = LocalAdmin(
        id=1,
        password_hash=password_hash(password) if password else DISABLED_PASSWORD_HASH,
        session_secret_encrypted=protect_secret(secrets.token_urlsafe(32)),
    )
    session.add(row)
    session.flush()
    description = (
        "Local admin recovery password was created."
        if password
        else "Local admin was created without a recovery password."
    )
    record_security_event(session, "LOCAL_ADMIN_CREATED", description)
    return row


def set_recovery_password(session: Session, password: str) -> LocalAdmin:
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise ValueError(f"Recovery password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters.")
    admin = get_admin(session)
    if admin is None:
        raise ValueError("Local admin is not configured.")
    admin.password_hash = password_hash(password)
    admin.updated_at = utc_now()
    record_security_event(session, "LOCAL_RECOVERY_PASSWORD_SET", "Local admin recovery password was updated.")
    return admin


def verify_admin_password(session: Session, password: str) -> LocalAdmin | None:
    admin = get_admin(session)
    if admin is None or not recovery_password_configured(admin):
        return None
    if not verify_password(password, admin.password_hash):
        return None
    admin.failed_login_count = 0
    admin.locked_until = None
    admin.updated_at = utc_now()
    return admin


def make_session(admin: LocalAdmin) -> tuple[str, AdminSession]:
    secret = _admin_secret(admin)
    session_id = secrets.token_urlsafe(24)
    expires_at = int(time.time()) + SESSION_SECONDS
    signature = _session_signature(secret, session_id, expires_at)
    cookie_value = f"{session_id}.{expires_at}.{signature}"
    return cookie_value, AdminSession(session_id, expires_at, csrf_token=_csrf_token(secret, cookie_value))


def parse_session(session: Session, cookie_value: str | None) -> AdminSession | None:
    admin = get_admin(session)
    if admin is None or not cookie_value:
        return None
    try:
        session_id, expires_text, signature = cookie_value.split(".", 2)
        expires_at = int(expires_text)
    except ValueError:
        return None
    if expires_at < int(time.time()):
        return None
    secret = _admin_secret(admin)
    expected = _session_signature(secret, session_id, expires_at)
    if not hmac.compare_digest(signature, expected):
        return None
    return AdminSession(session_id, expires_at, csrf_token=_csrf_token(secret, cookie_value))


def valid_csrf(session: Session, cookie_value: str | None, submitted_token: str | None) -> bool:
    admin_session = parse_session(session, cookie_value)
    if admin_session is None or not submitted_token:
        return False
    return hmac.compare_digest(admin_session.csrf_token, submitted_token)


def record_security_event(session: Session, event_type: str, description: str | None = None) -> None:
    session.add(ServiceEvent(event_type=event_type, description=description))


def _admin_secret(admin: LocalAdmin) -> str:
    return unprotect_secret(admin.session_secret_encrypted)


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
