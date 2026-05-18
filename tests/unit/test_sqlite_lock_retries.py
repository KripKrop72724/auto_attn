from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from zk_zone_agent.db import (
    SQLITE_BUSY_TIMEOUT_MS,
    create_sqlite_engine,
    is_sqlite_lock_error,
    run_session_with_retries,
)


def _operational_error(message: str) -> OperationalError:
    return OperationalError("INSERT", {}, RuntimeError(message))


def test_sqlite_engine_uses_extended_busy_timeout(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")

    with engine.connect() as connection:
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert busy_timeout == SQLITE_BUSY_TIMEOUT_MS


def test_run_session_with_retries_reopens_after_locked_commit(monkeypatch):
    import zk_zone_agent.db as db_module

    sessions: list[object] = []

    @contextmanager
    def fake_session_scope():
        session = object()
        sessions.append(session)
        yield session
        if len(sessions) == 1:
            raise _operational_error("database is locked")

    monkeypatch.setattr(db_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(db_module.time, "sleep", lambda _seconds: None)

    seen_sessions: list[object] = []

    def operation(session: object) -> str:
        seen_sessions.append(session)
        return "ok"

    assert run_session_with_retries(operation, attempts=2, base_delay_seconds=0) == "ok"
    assert len(sessions) == 2
    assert seen_sessions == sessions
    assert sessions[0] is not sessions[1]


def test_run_session_with_retries_does_not_retry_non_lock_errors(monkeypatch):
    import zk_zone_agent.db as db_module

    attempts = 0

    @contextmanager
    def fake_session_scope():
        nonlocal attempts
        attempts += 1
        yield object()
        raise _operational_error("no such table: missing")

    monkeypatch.setattr(db_module, "session_scope", fake_session_scope)

    with pytest.raises(OperationalError):
        run_session_with_retries(lambda _session: None, attempts=3, base_delay_seconds=0)

    assert attempts == 1
    assert is_sqlite_lock_error(_operational_error("database schema is locked"))
    assert not is_sqlite_lock_error(_operational_error("no such table: missing"))
