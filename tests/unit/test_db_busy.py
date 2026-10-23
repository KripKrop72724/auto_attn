"""A busy row lock is recognised, and a direct send waits out one worker tick."""
import os
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zk_add.attendance_force_release import _lock_scheduler
from zk_add.db import Base, database_busy, wait_longer_for_locks
from zk_add.models import AttendanceForceReleaseScheduler as Scheduler


class _Original(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(("sqlstate", "busy"), [("55P03", True), ("57014", True), ("40P01", False), (None, False)])
def test_only_lock_and_statement_timeouts_count_as_busy(sqlstate, busy):
    assert database_busy(OperationalError("SELECT 1", {}, _Original(sqlstate))) is busy
    assert database_busy(ValueError("55P03")) is False


@pytest.fixture()
def postgres():
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "db_busy_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=500"})
    Base.metadata.create_all(engine, tables=[Scheduler.__table__])
    with Session(engine) as session:
        session.add(Scheduler(id=1))
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _hold_scheduler(engine, seconds):
    held = threading.Event()

    def run():
        with Session(engine) as holder:
            _lock_scheduler(holder)  # a worker release tick
            held.set()
            time.sleep(seconds)
            holder.commit()

    thread = threading.Thread(target=run)
    thread.start()
    assert held.wait(5)
    return thread


def test_the_default_lock_timeout_fails_busy_while_a_tick_holds_the_scheduler(postgres):
    thread = _hold_scheduler(postgres, 1.5)
    try:
        with Session(postgres) as session, pytest.raises(OperationalError) as raised:
            _lock_scheduler(session)
        assert database_busy(raised.value)
    finally:
        thread.join()


def test_a_direct_send_waits_out_one_tick_for_the_scheduler(postgres):
    thread = _hold_scheduler(postgres, 1.5)
    try:
        with Session(postgres) as session:
            started = time.monotonic()
            wait_longer_for_locks(session, 5000)
            assert _lock_scheduler(session) is not None
            assert time.monotonic() - started >= 1.0
            assert session.execute(text("SELECT current_setting('lock_timeout')")).scalar() == "5s"
            session.commit()
            # SET LOCAL ended with the transaction.
            assert session.execute(text("SELECT current_setting('lock_timeout')")).scalar() == "500ms"
    finally:
        thread.join()
