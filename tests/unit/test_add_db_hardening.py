from __future__ import annotations

import zk_add.db as add_db


def test_postgres_engine_has_bounded_pool_and_database_waits(monkeypatch):
    captured: dict = {}
    expected_engine = object()

    def fake_create_engine(url, **options):
        captured["url"] = url
        captured["options"] = options
        return expected_engine

    monkeypatch.setattr(add_db, "create_engine", fake_create_engine)

    engine = add_db.create_database_engine(
        "postgresql+psycopg://service:test@postgres:5432/attendance"
    )

    assert engine is expected_engine
    assert captured["options"]["pool_pre_ping"] is True
    assert captured["options"]["pool_size"] == add_db.settings.database_pool_size
    assert captured["options"]["max_overflow"] == add_db.settings.database_max_overflow
    assert captured["options"]["pool_timeout"] == add_db.settings.database_pool_timeout_seconds
    assert captured["options"]["pool_recycle"] == add_db.settings.database_pool_recycle_seconds
    assert captured["options"]["pool_use_lifo"] is True

    connect_args = captured["options"]["connect_args"]
    assert connect_args["connect_timeout"] == add_db.settings.database_connect_timeout_seconds
    assert f"statement_timeout={add_db.settings.database_statement_timeout_ms}" in connect_args[
        "options"
    ]
    assert f"lock_timeout={add_db.settings.database_lock_timeout_ms}" in connect_args["options"]
    assert "idle_in_transaction_session_timeout=60000" in connect_args["options"]
