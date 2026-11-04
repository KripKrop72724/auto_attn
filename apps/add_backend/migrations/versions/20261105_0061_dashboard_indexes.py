"""Index the dashboard's backlog reads and vacuum the busiest tables sooner.

Production (4 Nov 2026) showed the overview and recovery summaries reading
add_attendance_events and add_ords_outbox end to end on every refresh:

* ``max(add_ords_outbox.last_attempt_at)`` (worker.ords_delivery_metrics) had
  no index at all;
* ``min(received_at)`` of held punches (worker.ords_delivery_metrics with
  ``ords_status = 'BLOCKED_IDENTITY'``; attendance_repair.repair_worker_metrics
  with ``ords_status IN (BLOCKED_IDENTITY, QUARANTINED_IDENTITY_REUSE)`` and
  ``identity_content_status != 'VERIFIED'``) scanned the whole table;
* ords_outbox_coverage's anti-join starts from every event that is not
  ``ACKED``/``ACKED_CHECK``, which is a small part of the table.

Each index is partial or narrow, so it is small, and every predicate is the
code's own literal set (ords_states.ORDS_ACKNOWLEDGED_STATUSES,
ORDS_IDENTITY_HELD_STATUSES / attendance_repair.RELEASE_HELD_STATUSES).

add-api runs this migration before it serves (deploy.ps1 waits 240 s for
health). Unlike 20261021_ota_interrupted_boot_retry, which leaves large
tables to the operator, these tables are a few hundred thousand rows, so the
deploy builds them with CREATE INDEX CONCURRENTLY under a hard time budget:
no write is blocked, and a build that runs out of time is cancelled, its
half-built index removed, and the operator command logged. ADD works without
the indexes (those reads are only slower); this migration never fails a
deploy over them.

An index that already exists but is not valid is either an operator's
concurrent build still running or one that was interrupted. It is never
dropped or rebuilt here: the operator commands are logged instead.

The same budgeted, lock-bounded approach sets per-table autovacuum thresholds
(the 0.2 default lets 100k+ dead rows pile up before a vacuum, which also
breaks index-only scans) and creates pg_stat_statements, which only collects
once docker-compose.add.yml preloads it.
"""

import logging
import time
from typing import NamedTuple

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

revision = "20261105_0061_dashboard_indexes"
down_revision = "20261104_0060_tailed_record_uid"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic")

HELD_STATUSES_SQL = "('BLOCKED_IDENTITY','QUARANTINED_IDENTITY_REUSE')"
ACKNOWLEDGED_STATUSES_SQL = "('ACKED','ACKED_CHECK')"


class DashboardIndex(NamedTuple):
    # A NamedTuple, not a dataclass: Alembic loads this file without
    # registering it in sys.modules, which dataclasses need.
    name: str
    table: str
    columns: tuple[str, ...]
    include: tuple[str, ...] = ()
    where: str | None = None

    @property
    def create_sql(self) -> str:
        sql = (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {self.name} "
               f"ON {self.table} ({', '.join(self.columns)})")
        if self.include:
            sql += f" INCLUDE ({', '.join(self.include)})"
        if self.where:
            sql += f" WHERE {self.where}"
        return sql

    @property
    def drop_sql(self) -> str:
        return f"DROP INDEX CONCURRENTLY IF EXISTS {self.name}"


INDEXES = (
    DashboardIndex("ix_add_ords_outbox_last_attempt_at", "add_ords_outbox", ("last_attempt_at",)),
    DashboardIndex(
        "ix_add_attendance_held_received", "add_attendance_events", ("received_at",),
        include=("ords_status", "identity_content_status"),
        where=f"ords_status IN {HELD_STATUSES_SQL}",
    ),
    DashboardIndex(
        "ix_add_attendance_unacked_id", "add_attendance_events", ("id",),
        include=("ords_status",),
        where=f"ords_status NOT IN {ACKNOWLEDGED_STATUSES_SQL}",
    ),
)

AUTOVACUUM_TABLES = ("add_attendance_events", "add_ords_outbox", "add_terminal_record_manifest")
AUTOVACUUM_SETTINGS = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_analyze_scale_factor": "0.01",
    "autovacuum_vacuum_insert_scale_factor": "0.02",
}

# Time budget. deploy.ps1 waits 240 s for add-api health; docker-compose.add.yml
# gives add-api a 180 s start period plus six 10 s retries. Worst case here:
# 90 s of index builds + 3 x 10 s cleanup + 3 x 5 s autovacuum + 5 s extension
# = 140 s, leaving the earlier migrations and Uvicorn startup the rest.
BUILD_BUDGET_SECONDS = 90.0
BUILD_TIMEOUT_SECONDS = 60.0
MIN_BUILD_SECONDS = 5.0
BUILD_LOCK_TIMEOUT = "5s"
CLEANUP_TIMEOUT_SECONDS = 10.0
CLEANUP_LOCK_TIMEOUT = "5s"
ALTER_TIMEOUT_SECONDS = 5.0
ALTER_LOCK_TIMEOUT = "3s"
EXTENSION_TIMEOUT_SECONDS = 5.0


def _set_timeouts(conn: Connection, *, lock_timeout: str, statement_seconds: float) -> None:
    conn.execute(text("SELECT set_config('lock_timeout', :value, false)"), {"value": lock_timeout})
    conn.execute(
        text("SELECT set_config('statement_timeout', :value, false)"),
        {"value": f"{max(1, int(statement_seconds * 1000))}ms"},
    )


def _reset_timeouts(conn: Connection) -> None:
    conn.execute(text("RESET lock_timeout"))
    conn.execute(text("RESET statement_timeout"))


def _sqlstate(error: DBAPIError) -> str:
    original = getattr(error, "orig", None)
    return str(getattr(original, "sqlstate", None) or getattr(original, "pgcode", None) or "unknown")


def _index_validity(conn: Connection, name: str) -> bool | None:
    """True/False for an existing index in this schema, None when absent."""
    row = conn.execute(
        text("SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"),
        {"name": name},
    ).first()
    return None if row is None else bool(row[0])


def _remove_own_failed_build(conn: Connection, index: DashboardIndex) -> None:
    """Drop the INVALID index this migration's own cancelled build left behind.

    Only called right after this connection's CREATE INDEX CONCURRENTLY
    failed, so no build of it is running. An invalid index is still updated by
    every write, so leaving it would cost inserts and help no read.
    """
    if _index_validity(conn, index.name) is not False:
        return
    try:
        _set_timeouts(conn, lock_timeout=CLEANUP_LOCK_TIMEOUT, statement_seconds=CLEANUP_TIMEOUT_SECONDS)
        conn.execute(text(index.drop_sql))
    except DBAPIError as error:
        log.warning(
            "Could not remove the interrupted build of %s (SQLSTATE %s). Once "
            "pg_stat_progress_create_index shows no build of it, run while ADD is up: %s; then %s",
            index.name, _sqlstate(error), index.drop_sql, index.create_sql)


def build_index(conn: Connection, index: DashboardIndex, deadline: float) -> str:
    """Build one index concurrently within the remaining budget; never raise.

    Returns what happened: present, invalid_left, skipped, built or failed.
    """
    validity = _index_validity(conn, index.name)
    if validity is True:
        return "present"
    if validity is False:
        log.warning(
            "%s exists but is not valid: a concurrent build is still running or was interrupted. Not "
            "touching it during the deploy. Once pg_stat_progress_create_index shows no build of it, run "
            "while ADD is up: %s; then %s", index.name, index.drop_sql, index.create_sql)
        return "invalid_left"
    remaining = deadline - time.monotonic()
    if remaining < MIN_BUILD_SECONDS:
        log.warning("Deploy index budget used up; not building %s now. Run while ADD is up: %s",
                    index.name, index.create_sql)
        return "skipped"
    try:
        _set_timeouts(conn, lock_timeout=BUILD_LOCK_TIMEOUT,
                      statement_seconds=min(BUILD_TIMEOUT_SECONDS, remaining))
        conn.execute(text(index.create_sql))
    except DBAPIError as error:
        log.warning("Building %s did not finish during the deploy (SQLSTATE %s); ADD runs without it. "
                    "Run while ADD is up: %s", index.name, _sqlstate(error), index.create_sql)
        _remove_own_failed_build(conn, index)
        return "failed"
    if _index_validity(conn, index.name) is not True:
        # IF NOT EXISTS met an index another session created meanwhile.
        log.warning("%s is not valid yet; leaving it to its own build. Check pg_stat_progress_create_index; "
                    "if no build of it runs: %s; then %s", index.name, index.drop_sql, index.create_sql)
        return "invalid_left"
    return "built"


def _autovacuum_options_sql(table: str) -> str:
    options = ", ".join(f"{name} = {value}" for name, value in AUTOVACUUM_SETTINGS.items())
    return f"ALTER TABLE {table} SET ({options})"


def tune_autovacuum(conn: Connection, table: str) -> bool:
    statement = _autovacuum_options_sql(table)
    try:
        _set_timeouts(conn, lock_timeout=ALTER_LOCK_TIMEOUT, statement_seconds=ALTER_TIMEOUT_SECONDS)
        conn.execute(text(statement))
    except DBAPIError as error:
        log.warning("Could not set autovacuum thresholds on %s during the deploy (SQLSTATE %s). "
                    "Run while ADD is up: %s", table, _sqlstate(error), statement)
        return False
    return True


def enable_pg_stat_statements(conn: Connection) -> bool:
    statement = "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
    try:
        _set_timeouts(conn, lock_timeout=ALTER_LOCK_TIMEOUT, statement_seconds=EXTENSION_TIMEOUT_SECONDS)
        conn.execute(text(statement))
    except DBAPIError as error:
        log.warning("pg_stat_statements is unavailable (SQLSTATE %s); query statistics stay off. "
                    "A superuser can run: %s", _sqlstate(error), statement)
        return False
    return True


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for index in INDEXES:
            options = {"sqlite_where": text(index.where)} if index.where else {}
            op.create_index(index.name, index.table, list(index.columns), if_not_exists=True, **options)
        return
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction; each statement
    # below commits on its own, so a cancelled one leaves no aborted transaction.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        deadline = time.monotonic() + BUILD_BUDGET_SECONDS
        try:
            for index in INDEXES:
                build_index(conn, index, deadline)
            for table in AUTOVACUUM_TABLES:
                tune_autovacuum(conn, table)
            enable_pg_stat_statements(conn)
        finally:
            _reset_timeouts(conn)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for index in reversed(INDEXES):
            op.drop_index(index.name, table_name=index.table, if_exists=True)
        return
    # pg_stat_statements stays: an operator may have created it before 0061,
    # and it holds no ADD data.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        try:
            _set_timeouts(conn, lock_timeout="10s", statement_seconds=120)
            reset = ", ".join(AUTOVACUUM_SETTINGS)
            for table in AUTOVACUUM_TABLES:
                conn.execute(text(f"ALTER TABLE {table} RESET ({reset})"))
            for index in reversed(INDEXES):
                conn.execute(text(index.drop_sql))
        finally:
            _reset_timeouts(conn)
