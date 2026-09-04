from __future__ import annotations

import zk_add.db as add_db
import zk_add.models  # noqa: F401


def test_declared_schema_identifiers_fit_postgresql_limit():
    identifiers: list[tuple[str, str]] = []
    for table in add_db.Base.metadata.tables.values():
        identifiers.append((f"table {table.name}", table.name))
        identifiers.extend(
            (f"column {table.name}.{column.name}", column.name)
            for column in table.columns
        )
        identifiers.extend(
            (f"index on {table.name}", index.name)
            for index in table.indexes
            if index.name
        )
        identifiers.extend(
            (f"constraint on {table.name}", constraint.name)
            for constraint in table.constraints
            if constraint.name
        )

    over_limit = [
        f"{context}: {name} ({len(name)})"
        for context, name in identifiers
        if len(name) > 63
    ]
    assert over_limit == []


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
