"""Database capacity: migration 0061 indexes, PostgreSQL sizing and the deploy plumbing.

The indexes serve the overview/recovery reads that scanned add_attendance_events
and add_ords_outbox end to end. The migration must never stall or fail a
deploy over them, and its partial predicates must stay the code's own status
sets, or PostgreSQL silently stops using them.
"""

from __future__ import annotations

import logging
import os
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, create_engine, func, make_url, select, text

import zk_add.models  # noqa: F401
from zk_add.attendance_repair import RELEASE_HELD_STATUSES
from zk_add.db import Base
from zk_add.models import AttendanceEvent, OrdsOutbox
from zk_add.ords_states import ORDS_ACKNOWLEDGED_STATUSES, ORDS_IDENTITY_HELD_STATUSES
from zk_add.settings import AddSettings

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "apps/add_backend/migrations/versions"
MIGRATION_FILE = VERSIONS / "20261105_0061_dashboard_indexes.py"
CAPACITY_SUFFIXES = (
    "POSTGRES_MEMORY_LIMIT",
    "POSTGRES_SHM_SIZE",
    "POSTGRES_SHARED_BUFFERS",
    "POSTGRES_EFFECTIVE_CACHE_SIZE",
    "POSTGRES_MAINTENANCE_WORK_MEM",
    "POSTGRES_WORK_MEM",
    "POSTGRES_MAX_CONNECTIONS",
    "API_MEMORY_LIMIT",
    "DATABASE_POOL_SIZE",
    "DATABASE_MAX_OVERFLOW",
    "DATABASE_POOL_TIMEOUT_SECONDS",
)


def _migration():
    spec = spec_from_file_location("dashboard_indexes_migration", MIGRATION_FILE)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.add.yml").read_text(encoding="utf-8"))


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)s", value)
    assert match, value
    return int(match.group(1))


def _postgres_flags() -> dict[str, str]:
    command = _compose()["services"]["postgres"]["command"]
    assert command[0] == "postgres"
    flags = {}
    for flag, setting in zip(command[1::2], command[2::2]):
        assert flag == "-c"
        name, value = setting.split("=", 1)
        flags[name] = value
    return flags


def _sql_literals(sql: str) -> frozenset[str]:
    return frozenset(re.findall(r"'([A-Z_]+)'", sql))


# Static contracts -----------------------------------------------------------


def test_migration_follows_0060_and_fits_alembic_version_column() -> None:
    migration = _migration()
    previous = (VERSIONS / "20261104_0060_tailed_record_uid_guard.py").read_text(encoding="utf-8")
    assert f'revision = "{migration.down_revision}"' in previous
    assert len(migration.revision) <= 32


def test_partial_predicates_are_the_codes_status_sets() -> None:
    migration = _migration()
    held = _sql_literals(migration.HELD_STATUSES_SQL)
    assert held == ORDS_IDENTITY_HELD_STATUSES == RELEASE_HELD_STATUSES
    assert _sql_literals(migration.ACKNOWLEDGED_STATUSES_SQL) == ORDS_ACKNOWLEDGED_STATUSES
    by_name = {index.name: index for index in migration.INDEXES}
    assert by_name["ix_add_attendance_held_received"].where == f"ords_status IN {migration.HELD_STATUSES_SQL}"
    assert by_name["ix_add_attendance_unacked_id"].where == (
        f"ords_status NOT IN {migration.ACKNOWLEDGED_STATUSES_SQL}"
    )
    for index in migration.INDEXES:
        assert len(index.name) <= 63
        assert index.create_sql.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS ")


def test_migration_worst_case_fits_the_api_health_window() -> None:
    migration = _migration()
    worst = (
        migration.BUILD_BUDGET_SECONDS
        + len(migration.INDEXES) * migration.CLEANUP_TIMEOUT_SECONDS
        + len(migration.AUTOVACUUM_TABLES) * migration.ALTER_TIMEOUT_SECONDS
        + migration.EXTENSION_TIMEOUT_SECONDS
    )
    assert migration.BUILD_TIMEOUT_SECONDS <= migration.BUILD_BUDGET_SECONDS
    api = _compose()["services"]["add-api"]
    start_period = _seconds(api["healthcheck"]["start_period"])
    deploy = (ROOT / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    assert '"up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "240"' in deploy
    # Earlier migrations, interpreter start and Uvicorn start get 30 s or more.
    assert worst + 30 <= start_period <= 240


def test_postgres_is_sized_for_the_production_vm_and_overridable() -> None:
    flags = _postgres_flags()
    assert flags["shared_buffers"] == "${ADD_POSTGRES_SHARED_BUFFERS:-8GB}"
    assert flags["effective_cache_size"] == "${ADD_POSTGRES_EFFECTIVE_CACHE_SIZE:-24GB}"
    assert flags["maintenance_work_mem"] == "${ADD_POSTGRES_MAINTENANCE_WORK_MEM:-2GB}"
    assert flags["work_mem"] == "${ADD_POSTGRES_WORK_MEM:-32MB}"
    assert flags["max_connections"] == "${ADD_POSTGRES_MAX_CONNECTIONS:-200}"
    assert flags["shared_preload_libraries"] == "pg_stat_statements"
    assert flags["jit"] == "off"
    # Slow-statement logs must never carry bind parameters (names, lookups).
    assert flags["log_min_duration_statement"] == "2000"
    assert flags["log_parameter_max_length"] == "0"
    for kept in ("checkpoint_timeout", "wal_compression", "log_lock_waits",
                 "idle_in_transaction_session_timeout"):
        assert kept in flags
    postgres = _compose()["services"]["postgres"]
    assert postgres["mem_limit"] == "${ADD_POSTGRES_MEMORY_LIMIT:-32g}"
    assert postgres["shm_size"] == "${ADD_POSTGRES_SHM_SIZE:-4gb}"
    assert _seconds(postgres["stop_grace_period"]) >= 120
    assert _seconds(postgres["healthcheck"]["start_period"]) == 120
    deploy = (ROOT / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    # The dependency wait outlasts PostgreSQL's start period and retries.
    assert '"up", "-d", "--wait", "--wait-timeout", "300", "postgres", "redis"' in deploy
    assert "$minimumMemoryBytes = [int64](48GB)" in deploy


def test_api_pool_stays_inside_postgres_connections_with_one_worker() -> None:
    fields = AddSettings.model_fields
    pool = fields["database_pool_size"].default
    overflow = fields["database_max_overflow"].default
    assert (pool, overflow, fields["database_pool_timeout_seconds"].default) == (40, 40, 10)
    max_connections = int(re.search(r":-(\d+)\}", _postgres_flags()["max_connections"]).group(1))
    assert pool + overflow + 20 <= max_connections
    api = _compose()["services"]["add-api"]
    assert api["mem_limit"] == "${ADD_API_MEMORY_LIMIT:-16g}"
    assert "--workers 1 " in api["command"][-1]


def test_capacity_overrides_reach_deploy_only_under_add_deploy_names() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/add-deploy.yml").read_text(encoding="utf-8"))
    jobs = [job for job in workflow["jobs"].values() if job.get("environment") == "add-production"]
    deploy_jobs = [job for job in jobs if "ADD_DEPLOY_ORDS_PASSWORD" in job.get("env", {})]
    assert deploy_jobs
    deploy = (ROOT / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    for job in deploy_jobs:
        env = job["env"]
        for suffix in CAPACITY_SUFFIXES:
            assert env[f"ADD_DEPLOY_{suffix}"] == "${{ vars.ADD_" + suffix + " }}"
            assert f'"{suffix}" = ' in deploy
            # Compose prefers the process environment, even empty, to .env.add.
            assert f"ADD_{suffix}" not in env
        assert not [name for name in env if name.startswith("ADD_POSTGRES_")]
    assert '$environment["ADD_$capacitySuffix"] = $deployValue' in deploy
    assert "$deployValue -cnotmatch $capacityPatterns[$capacitySuffix]" in deploy


def test_only_the_previous_shipped_sizes_are_retired() -> None:
    deploy = (ROOT / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    block = re.search(r"\$retiredCapacityDefaults = @\{(.*?)\n\}", deploy, re.DOTALL).group(1)
    retired = dict(re.findall(r'"(ADD_[A-Z_]+)" = "([^"]+)"', block))
    assert retired == {
        "ADD_POSTGRES_MEMORY_LIMIT": "16g",
        "ADD_POSTGRES_SHM_SIZE": "2gb",
        "ADD_POSTGRES_SHARED_BUFFERS": "4GB",
        "ADD_POSTGRES_EFFECTIVE_CACHE_SIZE": "12GB",
        "ADD_POSTGRES_MAINTENANCE_WORK_MEM": "1GB",
        "ADD_API_MEMORY_LIMIT": "8g",
        "ADD_DATABASE_POOL_SIZE": "20",
        "ADD_DATABASE_MAX_OVERFLOW": "20",
        "ADD_DATABASE_POOL_TIMEOUT_SECONDS": "5",
    }
    # The example no longer pins sizes, so a copied environment follows the defaults.
    example = (ROOT / ".env.add.example").read_text(encoding="utf-8")
    for name in retired:
        assert not re.search(rf"(?m)^{name}=", example), name
        assert re.search(rf"(?m)^# {name}=", example), name


# PostgreSQL -----------------------------------------------------------------


@pytest.fixture()
def pg_engine():
    """A dedicated database: concurrent index builds wait only for snapshots in their own database."""
    url = os.environ.get("ADD_DASHBOARD_INDEX_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_DASHBOARD_INDEX_TEST_DATABASE_URL for PostgreSQL qualification")
    name = "dashboard_index_test_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = make_url(url).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
        migration = _migration()
        with engine.begin() as connection:
            for index in migration.INDEXES:
                connection.execute(text(f"DROP INDEX IF EXISTS {index.name}"))
        yield engine, database_url
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _run(engine, migration, step: str = "upgrade") -> None:
    with engine.connect() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, step)()
        connection.commit()


def _validity(engine, migration) -> dict[str, bool | None]:
    with engine.connect() as connection:
        return {index.name: migration._index_validity(connection, index.name)
                for index in migration.INDEXES}


def _reloptions(engine, table: str) -> set[str]:
    with engine.connect() as connection:
        options = connection.scalar(
            text("SELECT reloptions FROM pg_class WHERE oid = to_regclass(:name)"), {"name": table})
    return set(options or ())


class SnapshotHolder:
    """An open REPEATABLE READ transaction in the test database.

    It holds no lock on any ADD table, only an old snapshot, so a concurrent
    index build reaches its last phase and then waits for it.
    """

    def __init__(self, url: str) -> None:
        self.engine = create_engine(url)
        self.connection = self.engine.connect().execution_options(isolation_level="REPEATABLE READ")
        self.connection.execute(text("SELECT 1"))

    def release(self) -> None:
        self.connection.rollback()
        self.connection.close()
        self.engine.dispose()


def test_upgrade_builds_valid_indexes_tunes_autovacuum_and_is_repeatable(pg_engine) -> None:
    engine, _ = pg_engine
    migration = _migration()
    _run(engine, migration)
    assert set(_validity(engine, migration).values()) == {True}
    for table in migration.AUTOVACUUM_TABLES:
        assert _reloptions(engine, table) >= {
            "autovacuum_vacuum_scale_factor=0.02",
            "autovacuum_analyze_scale_factor=0.01",
            "autovacuum_vacuum_insert_scale_factor=0.02",
        }
    with engine.connect() as connection:
        definition = connection.scalar(text("SELECT pg_get_indexdef(to_regclass('ix_add_attendance_held_received'))"))
        # The session timeouts the migration used do not leak to later work.
        assert connection.scalar(text("SHOW statement_timeout")) == "0"
    assert "INCLUDE (ords_status, identity_content_status)" in definition
    _run(engine, migration)  # already present: nothing to do
    _run(engine, migration, "downgrade")
    assert set(_validity(engine, migration).values()) == {None}
    for table in migration.AUTOVACUUM_TABLES:
        assert not any(option.startswith("autovacuum_") for option in _reloptions(engine, table))
    _run(engine, migration)
    assert set(_validity(engine, migration).values()) == {True}


def test_dashboard_reads_can_use_the_partial_indexes(pg_engine) -> None:
    engine, _ = pg_engine
    migration = _migration()
    _run(engine, migration)
    queries = {
        # worker.ords_delivery_metrics
        "ix_add_ords_outbox_last_attempt_at": select(func.max(OrdsOutbox.last_attempt_at)),
        "ix_add_attendance_held_received": select(func.min(AttendanceEvent.received_at)).where(
            AttendanceEvent.ords_status == "BLOCKED_IDENTITY"),
        # ords_outbox_coverage: frozenset order varies between processes.
        "ix_add_attendance_unacked_id": select(AttendanceEvent.id, AttendanceEvent.ords_status).where(
            ~AttendanceEvent.ords_status.in_(tuple(sorted(ORDS_ACKNOWLEDGED_STATUSES, reverse=True)))),
    }
    # attendance_repair.repair_worker_metrics
    release_queue = select(func.min(AttendanceEvent.received_at)).where(
        AttendanceEvent.ords_status.in_(RELEASE_HELD_STATUSES),
        AttendanceEvent.identity_content_status != "VERIFIED",
    )
    with engine.connect() as connection:
        connection.execute(text("SET enable_seqscan = off"))
        connection.execute(text("SET enable_bitmapscan = off"))
        # Leave each read only the new index or a full scan to choose from.
        connection.execute(text("DROP INDEX ix_add_attendance_events_ords_status"))
        connection.execute(text("DROP INDEX ix_add_attendance_release_queue_status_date"))
        connection.execute(text("DROP INDEX ix_add_attendance_release_queue"))
        for name, query in [*queries.items(), ("ix_add_attendance_held_received", release_queue)]:
            # Literal values: what each execution's custom plan sees.
            sql = query.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True})
            plan = "\n".join(connection.exec_driver_sql(f"EXPLAIN {sql}").scalars())
            assert name in plan, plan
        connection.rollback()


def test_a_build_that_runs_out_of_time_is_removed_and_never_fails_the_deploy(
    pg_engine, monkeypatch, caplog
) -> None:
    engine, url = pg_engine
    migration = _migration()
    monkeypatch.setattr(migration, "BUILD_LOCK_TIMEOUT", "300ms")
    holder = SnapshotHolder(url)
    try:
        with caplog.at_level(logging.WARNING, logger="alembic"):
            _run(engine, migration)
    finally:
        holder.release()
    # Every build waited for the old snapshot, was cancelled and cleaned up.
    assert set(_validity(engine, migration).values()) == {None}
    messages = [record.getMessage() for record in caplog.records]
    for index in migration.INDEXES:
        assert any(index.name in message and "Run while ADD is up: " + index.create_sql in message
                   for message in messages), messages
    # Autovacuum and the extension do not wait for snapshots.
    for table in migration.AUTOVACUUM_TABLES:
        assert "autovacuum_vacuum_scale_factor=0.02" in _reloptions(engine, table)


def test_an_existing_invalid_index_is_left_to_the_operator(pg_engine, caplog) -> None:
    engine, url = pg_engine
    migration = _migration()
    outbox_index = migration.INDEXES[0]
    holder = SnapshotHolder(url)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(Exception):
                connection.execute(text(outbox_index.create_sql))
    finally:
        holder.release()
    assert _validity(engine, migration)[outbox_index.name] is False
    with caplog.at_level(logging.WARNING, logger="alembic"):
        _run(engine, migration)
    validity = _validity(engine, migration)
    assert validity.pop(outbox_index.name) is False
    assert set(validity.values()) == {True}
    assert any("exists but is not valid" in record.getMessage() and outbox_index.drop_sql in record.getMessage()
               for record in caplog.records)


def test_the_budget_skips_builds_once_spent(pg_engine, monkeypatch, caplog) -> None:
    engine, _ = pg_engine
    migration = _migration()
    monkeypatch.setattr(migration, "BUILD_BUDGET_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="alembic"):
        _run(engine, migration)
    assert set(_validity(engine, migration).values()) == {None}
    assert sum("budget used up" in record.getMessage() for record in caplog.records) == len(migration.INDEXES)

