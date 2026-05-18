from __future__ import annotations

from sqlalchemy.exc import OperationalError

from zk_zone_agent.supervisor import ZoneSupervisor


def _database_locked() -> OperationalError:
    return OperationalError("INSERT", {}, RuntimeError("database is locked"))


def test_time_loop_records_error_instead_of_crashing(monkeypatch):
    supervisor = ZoneSupervisor()
    recorded: list[tuple[str, Exception]] = []

    def fail_once() -> None:
        supervisor.stop_event.set()
        raise _database_locked()

    monkeypatch.setattr(supervisor, "_time_loop_tick", fail_once)
    monkeypatch.setattr(
        supervisor,
        "_record_background_error",
        lambda event_type, exc: recorded.append((event_type, exc)),
    )

    supervisor._time_loop()

    assert recorded
    assert recorded[0][0] == "TRUSTED_TIME_LOOP_ERROR"


def test_heartbeat_loop_records_error_instead_of_crashing(monkeypatch):
    supervisor = ZoneSupervisor()
    recorded: list[tuple[str, Exception]] = []

    def fail_once() -> None:
        supervisor.stop_event.set()
        raise _database_locked()

    monkeypatch.setattr(supervisor, "_heartbeat_loop_tick", fail_once)
    monkeypatch.setattr(
        supervisor,
        "_record_background_error",
        lambda event_type, exc: recorded.append((event_type, exc)),
    )

    supervisor._heartbeat_loop()

    assert recorded
    assert recorded[0][0] == "HEARTBEAT_LOOP_ERROR"
