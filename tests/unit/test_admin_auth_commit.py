"""Observe auth responses while the real dependency's commit is still blocked."""
import asyncio
from http.cookies import SimpleCookie
import json
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from zk_add import web
from zk_add.db import Base, create_database_engine
from zk_add.models import AdminSession, AuditChainHead, AuditEvent
from zk_add.protocol import token_hash
from zk_add.security import ADMIN_COOKIE, create_admin_session


@pytest.mark.parametrize("operation", ["login", "logout"])
@pytest.mark.parametrize("commit_fails", [False, True])
def test_auth_success_is_not_sent_before_durable_session_and_audit(
    tmp_path, monkeypatch, operation, commit_fails,
):
    # Distinct connections matter: a TestClient plus one overridden Session
    # sees its own uncommitted writes and hides the browser's immediate retry.
    engine = create_database_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine, tables=[
        AdminSession.__table__, AuditEvent.__table__, AuditChainHead.__table__,
    ])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    raw = csrf = None
    if operation == "logout":
        with sessions() as db:
            raw, row = create_admin_session(
                db, username="synthetic-admin", ip_address=None, user_agent="test",
            )
            csrf = row.csrf_token
            db.commit()

    entered, release = threading.Event(), threading.Event()

    class GatedSession(Session):
        def commit(self):
            if self.in_transaction():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test commit gate expired")
                if commit_fails:
                    raise RuntimeError("injected auth commit failure")
            super().commit()

    monkeypatch.setattr(web, "SessionLocal", sessionmaker(
        bind=engine, class_=GatedSession, expire_on_commit=False,
    ))
    monkeypatch.setattr(web, "verify_admin_password", lambda _name, _password: True)
    previous = web.app.dependency_overrides.copy()
    web.app.dependency_overrides.clear()

    async def exercise():
        body = json.dumps({"username": "synthetic-admin", "password": "test-password"}).encode()
        headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
        if raw:
            headers.extend([(b"cookie", f"{ADMIN_COOKIE}={raw}".encode()),
                            (b"x-csrf-token", csrf.encode())])
        scope = dict(type="http", asgi={"version": "3.0", "spec_version": "2.4"},
                     http_version="1.1", method="POST", scheme="https", root_path="",
                     path=f"/api/v1/auth/{operation}", query_string=b"", headers=headers,
                     client=("127.0.0.1", 1234), server=("testserver", 443))
        sent_body = False
        response_started = asyncio.Event()
        starts, observed, bodies = [], [], []

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                starts.append(message)
                cookie = SimpleCookie()
                for name, value in message["headers"]:
                    if name.lower() == b"set-cookie":
                        cookie.load(value.decode())
                response_token = cookie[ADMIN_COOKIE].value if ADMIN_COOKIE in cookie else None
                with sessions() as independent:
                    row = independent.scalar(select(AdminSession).where(
                        AdminSession.token_hash == token_hash(raw or response_token or "absent"),
                    ))
                    observed.append({
                        "exists": row is not None,
                        "revoked": row is not None and row.revoked_at is not None,
                        "audits": independent.scalar(select(func.count()).select_from(AuditEvent)),
                    })
                response_started.set()
            elif message["type"] == "http.response.body":
                bodies.append(message.get("body", b""))

        task = asyncio.create_task(web.app(scope, receive, send))
        try:
            assert await asyncio.to_thread(entered.wait, 5), "auth never reached its commit"
            # Hold the transaction across the response boundary, independent
            # of disk speed. No successful header may escape the commit gate.
            try:
                await asyncio.wait_for(response_started.wait(), 0.2)
            except asyncio.TimeoutError:
                pass
            premature = bool(starts)
        finally:
            release.set()
            try:
                await asyncio.wait_for(task, 5)
            except RuntimeError as error:
                assert commit_fails and str(error) == "injected auth commit failure"
        assert not premature, "authentication response escaped before the database commit"
        assert len(starts) == 1
        if commit_fails:
            assert starts[0]["status"] == 500
            assert not any(name.lower() == b"set-cookie" for name, _ in starts[0]["headers"])
            assert b"csrf_token" not in b"".join(bodies)
            assert observed == [{"exists": operation == "logout", "revoked": False, "audits": 0}]
        else:
            assert starts[0]["status"] == 200
            assert observed == [{"exists": True, "revoked": operation == "logout", "audits": 1}]
            assert json.loads(b"".join(bodies))["ok"] is True

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        web.app.dependency_overrides.clear()
        web.app.dependency_overrides.update(previous)
        engine.dispose()
