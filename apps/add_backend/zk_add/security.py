from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Header, HTTPException, Request
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from zk_add.models import AdminSession, Connector, ConnectorCredential, ConnectorNonce
from zk_add.settings import settings
from zk_add.protocol import body_sha256, sign_request, timestamp_within_skew, token_hash
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now


ADMIN_COOKIE = "add_admin"
_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


class LoginRateLimiter:
    def __init__(self, attempts: int = 5, window_seconds: int = 300, lock_seconds: int = 900) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._locked_until.get(key, 0) > now:
                return False
            events = [value for value in self._events.get(key, []) if value > now - self.window_seconds]
            self._events[key] = events
            return len(events) < self.attempts

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            events = [value for value in self._events.get(key, []) if value > now - self.window_seconds]
            events.append(now)
            self._events[key] = events
            if len(events) >= self.attempts:
                self._locked_until[key] = now + self.lock_seconds

    def success(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
            self._locked_until.pop(key, None)


login_rate_limiter = LoginRateLimiter()


@dataclass(frozen=True)
class AdminContext:
    row_id: int
    username: str
    csrf_token: str
    last_step_up_at: object | None


def hash_admin_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_admin_password(username: str, password: str) -> bool:
    if username != settings.admin_username or not settings.admin_password_hash:
        return False
    try:
        return _password_hasher.verify(settings.admin_password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def create_admin_session(
    session: Session, *, username: str, ip_address: str | None, user_agent: str | None
) -> tuple[str, AdminSession]:
    raw = secrets.token_urlsafe(48)
    now = utc_now()
    row = AdminSession(
        token_hash=token_hash(raw),
        username=username,
        csrf_token=secrets.token_urlsafe(32),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:500],
        last_seen_at=now,
        absolute_expires_at=now + timedelta(seconds=settings.admin_session_absolute_seconds),
        last_step_up_at=now,
    )
    session.add(row)
    session.flush()
    return raw, row


def admin_context(request: Request, session: Session) -> AdminContext:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="Authentication required.")
    now = utc_now()
    row = session.scalar(
        select(AdminSession).where(
            AdminSession.token_hash == token_hash(raw), AdminSession.revoked_at == None  # noqa: E711
        )
    )
    if row is None or ensure_utc(row.absolute_expires_at) <= now:
        raise HTTPException(status_code=401, detail="Session expired.")
    if ensure_utc(row.last_seen_at) + timedelta(seconds=settings.admin_session_idle_seconds) <= now:
        row.revoked_at = now
        raise HTTPException(status_code=401, detail="Session expired.")
    row.last_seen_at = now
    return AdminContext(row.id, row.username, row.csrf_token, row.last_step_up_at)


def require_csrf(context: AdminContext, submitted: str | None) -> None:
    if not submitted or not secrets.compare_digest(context.csrf_token, submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def require_step_up(password: str, session: Session, context: AdminContext) -> None:
    if not verify_admin_password(context.username, password):
        raise HTTPException(status_code=403, detail="Password confirmation failed.")
    row = session.get(AdminSession, context.row_id)
    if row is not None:
        row.last_step_up_at = utc_now()


def connector_token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def authenticate_connector_request(
    request: Request,
    session: Session,
    authorization: str | None = Header(default=None),
    connector_id: str | None = Header(default=None, alias="X-ADD-Connector-Id"),
    timestamp: str | None = Header(default=None, alias="X-ADD-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-ADD-Nonce"),
    supplied_body_hash: str | None = Header(default=None, alias="X-ADD-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-ADD-Signature"),
) -> Connector:
    if not authorization or not authorization.startswith("Bearer ") or not connector_id:
        raise HTTPException(status_code=401, detail="Missing connector credentials.")
    token = authorization.removeprefix("Bearer ").strip()
    connector = session.scalar(
        select(Connector).where(Connector.connector_id == connector_id, Connector.active == True)  # noqa: E712
    )
    if connector is None:
        raise HTTPException(status_code=401, detail="Invalid connector credentials.")
    credential = session.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.connector_id == connector.id,
            ConnectorCredential.active == True,  # noqa: E712
            ConnectorCredential.revoked_at == None,  # noqa: E711
            or_(
                ConnectorCredential.valid_until == None,  # noqa: E711
                ConnectorCredential.valid_until > utc_now(),
            ),
            ConnectorCredential.token_hash == connector_token_hash(token),
        )
    )
    if credential is None:
        raise HTTPException(status_code=401, detail="Invalid connector credentials.")
    if not all([timestamp, nonce, supplied_body_hash, signature]):
        raise HTTPException(status_code=401, detail="Missing signed connector headers.")
    try:
        parsed_timestamp = parse_datetime(timestamp)
        if not timestamp_within_skew(timestamp):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid connector timestamp.") from None
    body = await request.body()
    actual_body_hash = body_sha256(body)
    if not hmac.compare_digest(actual_body_hash, supplied_body_hash):
        raise HTTPException(status_code=401, detail="Connector body hash mismatch.")
    expected = sign_request(
        token=token,
        method=request.method,
        path=request.url.path,
        timestamp=timestamp,
        nonce=nonce,
        body_hash=supplied_body_hash,
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid connector signature.")
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:connector_key)"),
            {"connector_key": connector.id},
        )
    if session.scalar(
        select(ConnectorNonce).where(
            ConnectorNonce.connector_id == connector.id, ConnectorNonce.nonce == nonce
        )
    ):
        raise HTTPException(status_code=409, detail="Connector nonce replay rejected.")
    session.add(
        ConnectorNonce(
            connector_id=connector.id,
            nonce=nonce,
            request_timestamp=parsed_timestamp,
        )
    )
    credential.last_used_at = utc_now()
    return connector


def authenticate_websocket_token(session: Session, connector_id: str, token: str) -> Connector:
    connector = session.scalar(
        select(Connector).where(Connector.connector_id == connector_id, Connector.active == True)  # noqa: E712
    )
    if connector is None:
        raise ValueError("Unknown connector")
    credential = session.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.connector_id == connector.id,
            ConnectorCredential.active == True,  # noqa: E712
            ConnectorCredential.revoked_at == None,  # noqa: E711
            or_(
                ConnectorCredential.valid_until == None,  # noqa: E711
                ConnectorCredential.valid_until > utc_now(),
            ),
            ConnectorCredential.token_hash == connector_token_hash(token),
        )
    )
    if credential is None:
        raise ValueError("Invalid token")
    credential.last_used_at = utc_now()
    return connector
