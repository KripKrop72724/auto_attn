"""Employee check-in phone notifications: creation, worker, policy and API.

Every HTTP call here goes to an in-process ``httpx.MockTransport``. No test can
reach the real HRM notification service, and the key below is synthetic.
"""

from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import random
from threading import Barrier
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import MetaData, create_engine, func, inspect as sa_inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import checkin_notify as notify
from zk_add import worker
from zk_add.crypto import cnic_lookup, decrypt_cnic, encrypt_cnic
from zk_add.db import Base
from zk_add.models import (
    AttendanceCheckinNotification as Notification,
    AttendanceEvent,
    AuditEvent,
    CheckinNotifyControl,
    Connector,
    DeviceAlert,
    OrdsOutbox,
)
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.service import onboard_connector
from zk_add.settings import CHECKIN_NOTIFY_PATH, settings
from zk_add.time_utils import utc_now


SYNTHETIC_KEY = "synthetic-test-only-notify-credential"
CNIC = "3520212345671"
OTHER_CNIC = "3520299999991"
ROOT = Path(__file__).resolve().parents[2]
PRIMARY_URL = "https://ords.example.invalid/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"
FALLBACK_URL = "https://public.example.invalid/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "synthetic-notify-lookup-key")
    monkeypatch.setattr(settings, "fleet_root_secret", "synthetic-notify-fleet-root-secret")
    monkeypatch.setattr(
        settings, "ords_base_url", "https://ords.example.invalid/ords/slic_hrm/raw_attn_capture_event"
    )
    monkeypatch.setattr(settings, "checkin_notify_url", None)
    monkeypatch.setattr(settings, "checkin_notify_fallback_url", FALLBACK_URL)
    monkeypatch.setattr(settings, "checkin_notify_api_key", SYNTHETIC_KEY)
    monkeypatch.setattr(settings, "checkin_notify_mode", "ALL")
    monkeypatch.setattr(settings, "checkin_notify_pilot_zone_ids", "")
    monkeypatch.setattr(settings, "checkin_notify_rate_per_second", 50.0)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    monkeypatch.setattr("zk_add.db.SessionLocal", sessions)
    with sessions() as db:
        connector = make_connector(db, "e0:72:a1:00:00:41", "ZONE-PILOT")
        make_connector(db, "e0:72:a1:00:00:42", "ZONE-OTHER")
        notify.ensure_control(db)
        # The feature started a day ago, so punches in the tests are "new".
        db.get(CheckinNotifyControl, 1).created_at = utc_now() - timedelta(days=1)
        db.commit()
        connector_id = connector.id
    return sessions, connector_id


def make_connector(db, hardware_id: str, zone: str) -> Connector:
    connector, _token, _created = onboard_connector(
        db, hardware_id=hardware_id, zone_id=zone, zone_name=zone.title(),
        device_id=zone[-4:], firmware_version="2.7.6", expected_serial=f"SER{hardware_id[-2:]}",
        actor="test", ip_address="127.0.0.1",
    )
    db.flush()
    return connector


def punch(
    sessions, *, connector_id: int | None = None, minutes_ago: float = 1, cnic: str | None = CNIC,
    status: str = "PENDING", clock: str = "OK", zone: str | None = None, outbox: bool = True,
) -> int:
    with sessions() as db:
        if connector_id is None:
            connector_id = db.scalar(
                select(Connector.id).where(Connector.zone_id == (zone or "ZONE-PILOT"))
            )
        connector = db.get(Connector, connector_id)
        moment = utc_now() - timedelta(minutes=minutes_ago)
        event = AttendanceEvent(
            event_uid=hashlib.sha256(uuid4().bytes).hexdigest(),
            connector_id=connector.id,
            zkt_device_id=connector.zkt_device.id,
            device_serial=connector.zkt_device.serial,
            user_id="1007",
            display_name="Synthetic Employee",
            cnic_encrypted=encrypt_cnic(cnic) if cnic else None,
            cnic_lookup_hash=cnic_lookup(cnic) if cnic else None,
            cnic_last4=cnic[-4:] if cnic else None,
            device_event_time=moment,
            captured_at=moment,
            source="LIVE",
            status="0",
            punch="0",
            clock_quality=clock,
            identity_resolution_status="RESOLVED",
            manual_release_required=False,
            ords_status=status,
            raw_event={},
        )
        db.add(event)
        db.flush()
        if outbox:
            db.add(OrdsOutbox(attendance_event_id=event.id, status=status, delivery_type="LIVE"))
        db.commit()
        return event.id


def confirm(sessions, event_id: int, *, status="ACKED", path="ADD_DELIVERY", rollback=False):
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.ords_status = status
        event.oracle_confirmed_at = utc_now()
        event.oracle_confirmation_path = path
        db.flush()
        if rollback:
            db.rollback()
        else:
            db.commit()


def rows(sessions) -> list[Notification]:
    with sessions() as db:
        return db.scalars(select(Notification).order_by(Notification.id)).all()


def row_for(sessions, event_id: int) -> Notification | None:
    with sessions() as db:
        return db.scalar(select(Notification).where(Notification.attendance_event_id == event_id))


def outbox_id(sessions, event_id: int) -> int:
    with sessions() as db:
        return db.scalar(select(OrdsOutbox.id).where(OrdsOutbox.attendance_event_id == event_id))


class Recorder:
    """A mock HRM endpoint that records every request it would have received."""

    def __init__(self, *responses):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses) or [httpx.Response(200, json={"success": True})]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def deliver(recorder: Recorder) -> notify.Claim:
    return asyncio.run(notify.deliver_once(transport=recorder.transport))


def move_back(sessions, event_id: int, **fields) -> None:
    with sessions() as db:
        row = db.scalar(select(Notification).where(Notification.attendance_event_id == event_id))
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_fallback_defaults_to_the_public_host_and_is_dropped_when_it_equals_the_primary(monkeypatch):
    from zk_add.settings import AddSettings

    assert AddSettings.model_fields["checkin_notify_fallback_url"].default == (
        "https://eclaim2.slichealth.com/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"
    )
    monkeypatch.setattr(settings, "checkin_notify_url", None)
    monkeypatch.setattr(
        settings, "ords_base_url", "https://local.slichealth.com/ords/slic_hrm/raw_attn_capture_event"
    )
    monkeypatch.setattr(settings, "checkin_notify_fallback_url", FALLBACK_URL)
    assert settings.resolved_checkin_notify_fallback_url == FALLBACK_URL
    monkeypatch.setattr(
        settings, "checkin_notify_fallback_url",
        "https://local.slichealth.com/ords/slic_hrm/ESS_APIS/attendance_checkin_notify",
    )
    assert settings.resolved_checkin_notify_fallback_url is None
    for disabled in ("", "not a url"):
        monkeypatch.setattr(settings, "checkin_notify_fallback_url", disabled)
        assert settings.resolved_checkin_notify_fallback_url is None


def test_url_defaults_to_the_internal_ords_host_and_an_explicit_url_wins(monkeypatch):
    monkeypatch.setattr(settings, "checkin_notify_url", None)
    monkeypatch.setattr(
        settings, "ords_base_url", "https://local.slichealth.com/ords/slic_hrm/raw_attn_capture_event"
    )
    assert settings.resolved_checkin_notify_url == (
        "https://local.slichealth.com/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"
    )
    assert CHECKIN_NOTIFY_PATH == "/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"
    monkeypatch.setattr(settings, "checkin_notify_url", "https://notify.example.invalid/x")
    assert settings.resolved_checkin_notify_url == "https://notify.example.invalid/x"
    monkeypatch.setattr(settings, "checkin_notify_url", None)
    monkeypatch.setattr(settings, "ords_base_url", "")
    assert settings.resolved_checkin_notify_url is None


# --------------------------------------------------------------------------
# Transactional creation on every Oracle-confirm path
# --------------------------------------------------------------------------


def test_add_delivery_ack_records_one_row_with_the_cnic_oracle_received(store):
    sessions, connector_id = store
    event_id = punch(sessions)
    claim_id = outbox_id(sessions, event_id)
    with sessions() as db:
        db.get(OrdsOutbox, claim_id).status = "IN_FLIGHT"
        db.commit()
    # The claim payload is what Oracle received; it wins over later edits.
    claims = [(claim_id, {"event_uid": "x", "cnic": OTHER_CNIC}, connector_id, False)]
    results = [(claim_id, connector_id, 201, {"success": True}, None, True)]
    worker.apply_ords_batch_results(claims, [], None, results)
    (row,) = rows(sessions)
    assert row.attendance_event_id == event_id and row.status == "PENDING"
    assert row.confirmation_path == "ADD_DELIVERY"
    assert decrypt_cnic(row.cnic_encrypted) == OTHER_CNIC
    assert row.cnic_lookup_hash == cnic_lookup(OTHER_CNIC) and row.cnic_last4 == OTHER_CNIC[-4:]
    # A replayed ACK for the same punch never creates a second row.
    worker.apply_ords_batch_results(claims, [], None, results)
    assert len(rows(sessions)) == 1


def test_membership_and_firmware_receipt_confirmations_record_rows(store):
    sessions, connector_id = store
    checked = punch(sessions)
    firmware = punch(sessions, status="FIRMWARE_RECEIPT_VERIFYING")
    checked_outbox = outbox_id(sessions, checked)
    with sessions() as db:
        db.get(OrdsOutbox, checked_outbox).status = "IN_FLIGHT"
        db.commit()
    claims = [(checked_outbox, {"cnic": CNIC}, connector_id, False)]
    worker.apply_ords_batch_results(claims, [checked_outbox], None, [])
    worker.apply_firmware_receipt_audit_results(
        [(outbox_id(sessions, firmware), "f" * 64, connector_id)], set(),
        status=200, transport_error=None, response_parsed=True,
    )
    paths = {row.attendance_event_id: row.confirmation_path for row in rows(sessions)}
    assert paths == {
        checked: "ORDS_MEMBERSHIP_CHECK",
        firmware: "FIRMWARE_RECEIPT_MEMBERSHIP_CHECK",
    }


def test_a_terminal_delivered_punch_with_a_different_captured_cnic_is_not_notified(store):
    sessions, connector_id = store
    same = punch(sessions, status="FIRMWARE_RECEIPT_VERIFYING")
    changed = punch(sessions, status="FIRMWARE_RECEIPT_VERIFYING", cnic=OTHER_CNIC)
    with sessions() as db:
        db.get(AttendanceEvent, same).captured_cnic_lookup_hash = cnic_lookup(CNIC)
        # The terminal sent CNIC to Oracle; ADD's saved identity says OTHER_CNIC.
        db.get(AttendanceEvent, changed).captured_cnic_lookup_hash = cnic_lookup(CNIC)
        db.commit()
    outboxes = [(outbox_id(sessions, event_id), "f" * 64, connector_id) for event_id in (same, changed)]
    worker.apply_firmware_receipt_audit_results(
        outboxes, set(), status=200, transport_error=None, response_parsed=True,
    )
    states = {row.attendance_event_id: (row.status, row.reason) for row in rows(sessions)}
    assert states == {same: ("PENDING", None), changed: ("SKIPPED", "CNIC_UNCERTAIN")}


def test_a_rolled_back_confirmation_leaves_no_row(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id, rollback=True)
    assert rows(sessions) == []
    confirm(sessions, event_id)
    assert [row.attendance_event_id for row in rows(sessions)] == [event_id]


def test_reverification_and_repeat_confirmation_never_create_another_row(store):
    sessions, connector_id = store
    # Confirmed before the feature existed: no row, and re-verification must
    # not create one.
    before = punch(sessions, status="ACKED_CHECK")
    # Look the outbox row up first: the in-memory test database shares one
    # connection, so a nested session would roll back the delete below.
    before_outbox = outbox_id(sessions, before)
    with sessions() as db:
        db.execute(text("DELETE FROM add_attendance_checkin_notifications"))
        event = db.get(AttendanceEvent, before)
        event.ords_status = "MEMBERSHIP_REVERIFYING"
        db.get(OrdsOutbox, before_outbox).status = "MEMBERSHIP_REVERIFYING"
        db.commit()
    assert rows(sessions) == []
    worker.apply_confirmed_membership_audit_results(
        [(before_outbox, "a" * 64, connector_id)], set(),
        status=200, transport_error=None, response_parsed=True,
    )
    with sessions() as db:
        assert db.get(AttendanceEvent, before).ords_status == "ACKED_CHECK"
    assert rows(sessions) == []
    # A confirmed punch that drops out of Oracle and is confirmed again keeps
    # its single row.
    event_id = punch(sessions)
    confirm(sessions, event_id)
    with sessions() as db:
        db.get(AttendanceEvent, event_id).ords_status = "PENDING"
        db.commit()
    confirm(sessions, event_id, status="ACKED_CHECK", path="ORDS_MEMBERSHIP_CHECK")
    assert [row.attendance_event_id for row in rows(sessions)] == [event_id]


def test_new_attendance_inserted_already_confirmed_is_recorded(store):
    sessions, _ = store
    event_id = punch(sessions, status="ACKED")
    assert row_for(sessions, event_id) is not None


def test_no_code_path_confirms_attendance_with_a_bulk_update():
    """The capture hook sees ORM writes; a bulk UPDATE would bypass it."""
    offenders = []
    for path in sorted((ROOT / "apps/add_backend/zk_add").glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "update":
                if node.args and getattr(node.args[0], "id", None) == "AttendanceEvent":
                    offenders.append(f"{path.name}:{node.lineno}")
        lowered = source.lower()
        if "update add_attendance_events" in lowered and "ords_status" in lowered:
            offenders.append(path.name)
    assert offenders == []


def test_old_backlog_replays_invalid_clock_and_missing_cnic_are_not_sent(store):
    sessions, _ = store
    old = punch(sessions, minutes_ago=13 * 60)
    future = punch(sessions, minutes_ago=-30)
    invalid = punch(sessions, clock="INVALID")
    no_cnic = punch(sessions, cnic=None)
    for event_id in (old, future, invalid, no_cnic):
        confirm(sessions, event_id)
    reasons = {row.attendance_event_id: (row.status, row.reason) for row in rows(sessions)}
    assert reasons == {
        old: ("SKIPPED", "OLD_PUNCH"),
        future: ("SKIPPED", "INVALID_CLOCK"),
        invalid: ("SKIPPED", "INVALID_CLOCK"),
        no_cnic: ("SKIPPED", "NO_CNIC"),
    }
    recorder = Recorder()
    deliver(recorder)
    assert recorder.requests == []


def test_sweep_recreates_missing_recent_rows_only(store):
    sessions, _ = store
    recent = punch(sessions, status="ACKED")
    old = punch(sessions, minutes_ago=20 * 60, status="ACKED")
    with sessions() as db:
        db.execute(text("DELETE FROM add_attendance_checkin_notifications"))
        for event_id in (recent, old):
            db.get(AttendanceEvent, event_id).oracle_confirmed_at = utc_now()
        db.commit()
    assert notify.sweep_missing_rows() == 1
    assert [row.attendance_event_id for row in rows(sessions)] == [recent]
    assert notify.sweep_missing_rows() == 0


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


def test_worker_posts_cnic_as_a_json_number_with_the_key_header(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    recorder = Recorder(httpx.Response(200, json={"success": True, "message": "sent"}))
    deliver(recorder)
    (request,) = recorder.requests
    assert str(request.url) == (
        "https://ords.example.invalid/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"
    )
    assert request.method == "POST"
    assert request.headers["X-Api-Key"] == SYNTHETIC_KEY
    assert request.headers["Content-Type"] == "application/json"
    assert request.content == b'{"cnic":3520212345671}'
    assert json.loads(request.content) == {"cnic": 3520212345671}
    row = row_for(sessions, event_id)
    assert row.status == "SENT" and row.sent_at and row.last_http_status == 200
    # Nothing is sent twice.
    deliver(recorder)
    assert len(recorder.requests) == 1


def test_string_cnic_body_is_a_setting(store, monkeypatch):
    sessions, _ = store
    monkeypatch.setattr(settings, "checkin_notify_cnic_json_type", "string")
    confirm(sessions, punch(sessions))
    recorder = Recorder()
    deliver(recorder)
    assert recorder.requests[0].content == b'{"cnic":"3520212345671"}'


def test_retryable_failures_back_off_with_jitter_then_give_up(store, monkeypatch):
    sessions, _ = store
    monkeypatch.setattr(settings, "checkin_notify_fallback_url", None)  # One host only.
    event_id = punch(sessions)
    confirm(sessions, event_id)
    recorder = Recorder(httpx.Response(503), httpx.Response(429), httpx.ConnectError("down"))
    before = utc_now()
    deliver(recorder)
    row = row_for(sessions, event_id)
    assert (row.status, row.reason, row.attempt_count) == ("RETRYING", "HTTP_503", 1)
    first_delay = (row.next_attempt_at.replace(tzinfo=before.tzinfo) - before).total_seconds()
    assert 7 <= first_delay <= 16  # 15 s base, equal jitter.
    move_back(sessions, event_id, next_attempt_at=utc_now() - timedelta(seconds=1))
    deliver(recorder)
    assert row_for(sessions, event_id).reason == "HTTP_429"
    # Past the bounded retry window, the next failure is final.
    move_back(
        sessions, event_id, next_attempt_at=utc_now() - timedelta(seconds=1),
        retry_deadline_at=utc_now() + timedelta(seconds=1),
    )
    deliver(recorder)
    row = row_for(sessions, event_id)
    assert (row.status, row.reason, row.detail) == (
        "FAILED", "RETRY_WINDOW_EXHAUSTED", "TRANSPORT_CONNECT_ERROR"
    )
    assert len(recorder.requests) == 3


def test_a_row_that_waited_past_the_retry_window_fails_without_sending(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    # For example the circuit stayed open for the whole retry window.
    move_back(sessions, event_id, retry_deadline_at=utc_now() - timedelta(seconds=1))
    recorder = Recorder()
    deliver(recorder)
    assert recorder.requests == []
    row = row_for(sessions, event_id)
    assert (row.status, row.reason, row.attempt_count) == ("FAILED", "RETRY_WINDOW_EXHAUSTED", 0)


def test_backoff_is_exponential_bounded_and_jittered(monkeypatch):
    rng = random.Random(7)
    delays = [notify.backoff_seconds(attempt, rng=rng) for attempt in range(1, 12)]
    for attempt, delay in enumerate(delays, start=1):
        full = min(settings.checkin_notify_backoff_max_seconds, 15 * 2 ** (attempt - 1))
        assert full / 2 <= delay <= full
    assert max(delays) <= settings.checkin_notify_backoff_max_seconds


def test_timeouts_are_retried(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    deliver(Recorder(httpx.ReadTimeout("slow")))
    row = row_for(sessions, event_id)
    assert (row.status, row.reason) == ("RETRYING", "TRANSPORT_TIMEOUT")


# --------------------------------------------------------------------------
# Two hosts: fall back ONLY when the request provably never reached a server
# --------------------------------------------------------------------------


class Hosts:
    """A mock transport answering per host from a scripted list of outcomes."""

    def __init__(self, primary=(), fallback=()):
        self.script = {"ords.example.invalid": list(primary), "public.example.invalid": list(fallback)}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcomes = self.script[request.url.host]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else (
            outcomes[0] if outcomes else httpx.Response(200, json={"success": True})
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


def deliver_hosts(hosts: Hosts) -> notify.Claim:
    return asyncio.run(notify.deliver_once(transport=httpx.MockTransport(hosts)))


NEVER_SENT = {
    "dns_failure": httpx.ConnectError("[Errno -2] Name or service not known"),
    "connection_refused": httpx.ConnectError("[Errno 111] Connection refused"),
    "connect_timeout": httpx.ConnectTimeout("timed out while connecting"),
    "tls_handshake_failure": httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] handshake failed"),
}
MAYBE_DELIVERED = {
    "read_timeout": httpx.ReadTimeout("no response in time"),
    "write_timeout": httpx.WriteTimeout("request body stalled"),
    "reset_after_send": httpx.ReadError("[Errno 104] Connection reset by peer"),
    "disconnect_without_response": httpx.RemoteProtocolError(
        "Server disconnected without sending a response."
    ),
    "http_503": httpx.Response(503),
    "http_500": httpx.Response(500),
    "http_404": httpx.Response(404),
    "http_401": httpx.Response(401),
}


@pytest.mark.parametrize("error", NEVER_SENT.values(), ids=NEVER_SENT.keys())
def test_a_request_that_never_left_falls_back_to_the_public_host(store, error):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    hosts = Hosts(primary=[error])
    deliver_hosts(hosts)
    assert hosts.hosts == ["ords.example.invalid", "public.example.invalid"]
    fallback = hosts.requests[1]
    assert str(fallback.url) == FALLBACK_URL
    assert fallback.headers["X-Api-Key"] == SYNTHETIC_KEY  # Same key on both hosts.
    assert fallback.content == hosts.requests[0].content == b'{"cnic":3520212345671}'
    row = row_for(sessions, event_id)
    assert (row.status, row.host_used, row.attempt_count) == ("SENT", "FALLBACK", 1)
    assert "primary unreachable" in row.detail


@pytest.mark.parametrize("outcome", MAYBE_DELIVERED.values(), ids=MAYBE_DELIVERED.keys())
def test_a_request_that_may_have_been_delivered_never_falls_back(store, outcome):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    hosts = Hosts(primary=[outcome])
    deliver_hosts(hosts)
    assert hosts.hosts == ["ords.example.invalid"]  # Never repeated on the public host.
    row = row_for(sessions, event_id)
    assert row.host_used == "PRIMARY" and row.status in {"RETRYING", "FAILED"}


def test_an_overall_attempt_timeout_never_falls_back(store, monkeypatch):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    monkeypatch.setattr(notify, "_host_budget_seconds", lambda: 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return await slow(request)

    asyncio.run(notify.deliver_once(transport=httpx.MockTransport(handler)))
    assert seen == ["ords.example.invalid"]
    row = row_for(sessions, event_id)
    assert (row.status, row.reason, row.host_used) == ("RETRYING", "TRANSPORT_TIMEOUT", "PRIMARY")


def test_both_hosts_unreachable_is_retried_later(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    hosts = Hosts(primary=[NEVER_SENT["dns_failure"]], fallback=[NEVER_SENT["connection_refused"]])
    deliver_hosts(hosts)
    row = row_for(sessions, event_id)
    assert (row.status, row.reason) == ("RETRYING", "TRANSPORT_ALL_HOSTS_UNREACHABLE")
    assert hosts.hosts == ["ords.example.invalid", "public.example.invalid"]


def test_the_host_that_answered_is_sticky_and_the_primary_is_reprobed(store):
    sessions, _ = store
    first = punch(sessions, cnic=CNIC)
    confirm(sessions, first)
    hosts = Hosts(primary=[NEVER_SENT["connection_refused"]])
    deliver_hosts(hosts)
    with sessions() as db:
        control = db.get(CheckinNotifyControl, 1)
        assert control.preferred_host == "FALLBACK" and control.primary_reprobe_at is not None
    # While the primary is out, the public host is used first: no wasted attempt.
    second = punch(sessions, cnic=OTHER_CNIC)
    confirm(sessions, second)
    hosts = Hosts(primary=[httpx.Response(500)])
    deliver_hosts(hosts)
    assert hosts.hosts == ["public.example.invalid"]
    assert row_for(sessions, second).host_used == "FALLBACK"
    # At the re-probe time the primary is tried first again and becomes preferred.
    with sessions() as db:
        db.get(CheckinNotifyControl, 1).primary_reprobe_at = utc_now() - timedelta(seconds=1)
        db.commit()
    third = punch(sessions, cnic="3520211111111")
    confirm(sessions, third)
    hosts = Hosts()
    deliver_hosts(hosts)
    assert hosts.hosts == ["ords.example.invalid"]
    with sessions() as db:
        control = db.get(CheckinNotifyControl, 1)
        assert control.preferred_host == "PRIMARY" and control.primary_reprobe_at is None
    assert row_for(sessions, third).host_used == "PRIMARY"


def test_without_a_fallback_only_the_primary_is_used(store, monkeypatch):
    sessions, _ = store
    monkeypatch.setattr(settings, "checkin_notify_fallback_url", None)
    event_id = punch(sessions)
    confirm(sessions, event_id)
    hosts = Hosts(primary=[NEVER_SENT["dns_failure"]])
    deliver_hosts(hosts)
    assert hosts.hosts == ["ords.example.invalid"]
    assert row_for(sessions, event_id).reason == "TRANSPORT_CONNECT_ERROR"


async def _local_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _real_post(url: str, *, timeout: float = 2.0) -> notify.SendResult:
    """One attempt through httpx's real network stack against 127.0.0.1 only."""
    job = notify.SendJob(1, 1, CNIC, None)

    async def run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=timeout)) as client:
            return await notify.post_notification(client, url, job, host="PRIMARY")

    return asyncio.run(run())


def test_real_connection_refused_and_tls_failures_are_classified_as_never_sent(monkeypatch):
    import socket

    monkeypatch.setattr(settings, "checkin_notify_api_key", SYNTHETIC_KEY)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    refused = _real_post(f"http://127.0.0.1:{closed_port}/notify")
    assert refused.not_sent and refused.category == "TRANSPORT_CONNECT_ERROR"

    async def tls_against_plain_server():
        async def plain(reader, writer):
            await reader.read(64)  # The TLS ClientHello.
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server, port = await _local_server(plain)
        async with server:
            async with httpx.AsyncClient(timeout=2.0) as client:
                return await notify.post_notification(
                    client, f"https://127.0.0.1:{port}/notify",
                    notify.SendJob(1, 1, CNIC, None), host="PRIMARY",
                )

    tls = asyncio.run(tls_against_plain_server())
    assert tls.not_sent and tls.category == "TRANSPORT_CONNECT_ERROR"


def test_real_reset_after_send_and_read_timeout_are_classified_as_maybe_delivered(monkeypatch):
    monkeypatch.setattr(settings, "checkin_notify_api_key", SYNTHETIC_KEY)

    async def run(behaviour):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")  # The request reached the server.
            if behaviour == "close":
                writer.close()
                return
            await asyncio.sleep(1.5)
            writer.close()

        server, port = await _local_server(handler)
        async with server:
            async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=2.0)) as client:
                return await notify.post_notification(
                    client, f"http://127.0.0.1:{port}/notify",
                    notify.SendJob(1, 1, CNIC, None), host="PRIMARY",
                )

    closed = asyncio.run(run("close"))
    assert not closed.not_sent and closed.outcome == "RETRY"
    slow = asyncio.run(run("sleep"))
    assert not slow.not_sent and slow.category == "TRANSPORT_TIMEOUT"


def test_other_client_errors_fail_without_retry_or_pausing(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    recorder = Recorder(httpx.Response(404, json={"message": f"No employee {CNIC}"}))
    deliver(recorder)
    row = row_for(sessions, event_id)
    assert (row.status, row.reason, row.can_retry if hasattr(row, "can_retry") else None) == (
        "FAILED", "HTTP_404", None
    )
    assert CNIC not in (row.detail or "") and "#####" in row.detail
    with sessions() as db:
        assert db.get(CheckinNotifyControl, 1).consecutive_failures == 0
    move_back(sessions, event_id, next_attempt_at=utc_now() - timedelta(seconds=1))
    deliver(recorder)
    assert len(recorder.requests) == 1


def test_api_reported_failure_in_a_success_response_is_not_counted_as_sent(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    deliver(Recorder(httpx.Response(200, json={"success": False, "message": "inactive"})))
    row = row_for(sessions, event_id)
    assert (row.status, row.reason, row.detail) == ("FAILED", "API_REPORTED_FAILURE", "inactive")


def test_circuit_breaker_pauses_then_one_probe_resumes(store, monkeypatch):
    sessions, _ = store
    monkeypatch.setattr(settings, "checkin_notify_circuit_failure_threshold", 2)
    first, second, third = (punch(sessions, cnic=c) for c in (CNIC, OTHER_CNIC, "3520211111111"))
    for event_id in (first, second, third):
        confirm(sessions, event_id)
    monkeypatch.setattr(settings, "checkin_notify_batch_size", 2)
    failing = Recorder(httpx.Response(502))
    deliver(failing)
    with sessions() as db:
        control = db.get(CheckinNotifyControl, 1)
        assert control.consecutive_failures == 2 and control.paused_until is not None
        assert control.last_failure_category == "HTTP_502"
    claim = deliver(failing)
    assert claim.paused and len(failing.requests) == 2
    with sessions() as db:
        assert notify.health(db)["state"] == "PAUSED"
        db.get(CheckinNotifyControl, 1).paused_until = utc_now() - timedelta(seconds=1)
        db.commit()
    healthy = Recorder()
    claim = deliver(healthy)
    assert len(claim.jobs) == 1 and len(healthy.requests) == 1  # Half-open probe.
    with sessions() as db:
        control = db.get(CheckinNotifyControl, 1)
        assert control.consecutive_failures == 0 and control.paused_until is None
        assert control.last_success_at is not None


def test_authentication_failure_pauses_without_failing_the_punch(store, monkeypatch):
    sessions, _ = store
    monkeypatch.setattr(settings, "checkin_notify_circuit_failure_threshold", 1)
    event_id = punch(sessions)
    confirm(sessions, event_id)
    deliver(Recorder(httpx.Response(401)))
    row = row_for(sessions, event_id)
    assert (row.status, row.reason) == ("RETRYING", "HTTP_401")
    with sessions() as db:
        assert db.get(CheckinNotifyControl, 1).paused_until is not None


def test_an_expired_lease_is_reclaimed_and_a_stale_result_is_ignored(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    crashed = notify.claim_batch()  # The process dies after this claim.
    assert len(crashed.jobs) == 1
    assert notify.claim_batch().jobs == []  # The lease protects the row.
    move_back(sessions, event_id, lease_expires_at=utc_now() - timedelta(seconds=1))
    recovered = notify.claim_batch()
    assert len(recovered.jobs) == 1 and recovered.jobs[0].token == crashed.jobs[0].token + 1
    notify.apply_results([(crashed.jobs[0], notify.SendResult("FAILED", "HTTP_400", 400))])
    assert row_for(sessions, event_id).status == "SENDING"
    notify.apply_results([(recovered.jobs[0], notify.SendResult("SENT", "HTTP_200", 200))])
    row = row_for(sessions, event_id)
    assert (row.status, row.attempt_count) == ("SENT", 2)


def test_rapid_repeat_punches_are_grouped_into_one_notification(store):
    sessions, _ = store
    first = punch(sessions, minutes_ago=3)
    repeat = punch(sessions, minutes_ago=2.5)
    later = punch(sessions, minutes_ago=0.5)
    other_person = punch(sessions, minutes_ago=2.5, cnic=OTHER_CNIC)
    for event_id in (first, repeat, later, other_person):
        confirm(sessions, event_id)
    recorder = Recorder()
    deliver(recorder)
    by_event = {row.attendance_event_id: row for row in rows(sessions)}
    assert by_event[first].status == "SENT"
    assert (by_event[repeat].status, by_event[repeat].reason) == ("SKIPPED", "GROUPED")
    assert by_event[repeat].grouped_into_id == by_event[first].id
    assert by_event[later].status == "SENT"  # 2.5 minutes later is a new notification.
    assert by_event[other_person].status == "SENT"
    assert len(recorder.requests) == 3


def test_first_punch_per_day_policy(store):
    sessions, _ = store
    with sessions() as db:
        db.get(CheckinNotifyControl, 1).punch_policy = "FIRST_PER_DAY"
        db.commit()
    first = punch(sessions, minutes_ago=10)
    second = punch(sessions, minutes_ago=4)
    for event_id in (first, second):
        confirm(sessions, event_id)
    deliver(Recorder())
    by_event = {row.attendance_event_id: row for row in rows(sessions)}
    assert by_event[first].status == "SENT"
    assert (by_event[second].status, by_event[second].reason) == ("SKIPPED", "NOT_FIRST_OF_DAY")


def test_pilot_allowlist_by_zone_or_cnic_and_off_mode(store):
    sessions, _ = store
    with sessions() as db:
        control = db.get(CheckinNotifyControl, 1)
        control.mode = "PILOT"
        control.pilot_zone_ids = ["ZONE-PILOT"]
        control.pilot_cnics = [{"hash": cnic_lookup(OTHER_CNIC), "masked": "x"}]
        db.commit()
    in_zone = punch(sessions, zone="ZONE-PILOT", cnic="3520211111111")
    allowed_cnic = punch(sessions, zone="ZONE-OTHER", cnic=OTHER_CNIC)
    outside = punch(sessions, zone="ZONE-OTHER", cnic="3520222222222")
    for event_id in (in_zone, allowed_cnic, outside):
        confirm(sessions, event_id)
    recorder = Recorder()
    deliver(recorder)
    by_event = {row.attendance_event_id: (row.status, row.reason) for row in rows(sessions)}
    assert by_event == {
        in_zone: ("SENT", None),
        allowed_cnic: ("SENT", None),
        outside: ("SKIPPED", "NOT_IN_PILOT"),
    }
    with sessions() as db:
        db.get(CheckinNotifyControl, 1).mode = "OFF"
        db.commit()
    off = punch(sessions, zone="ZONE-PILOT", cnic="3520233333333")
    confirm(sessions, off)
    deliver(recorder)
    assert (row_for(sessions, off).status, row_for(sessions, off).reason) == ("SKIPPED", "DISABLED")
    assert len(recorder.requests) == 2


def test_empty_pilot_allowlist_sends_nothing(store):
    sessions, _ = store
    with sessions() as db:
        db.get(CheckinNotifyControl, 1).mode = "PILOT"
        db.commit()
    event_id = punch(sessions)
    confirm(sessions, event_id)
    recorder = Recorder()
    deliver(recorder)
    assert recorder.requests == [] and row_for(sessions, event_id).reason == "NOT_IN_PILOT"


def test_missing_key_reports_not_configured_and_sends_nothing(store, monkeypatch):
    sessions, _ = store
    monkeypatch.setattr(settings, "checkin_notify_api_key", None)
    event_id = punch(sessions)
    confirm(sessions, event_id)
    recorder = Recorder()
    deliver(recorder)
    assert recorder.requests == []
    assert (row_for(sessions, event_id).status, row_for(sessions, event_id).reason) == (
        "SKIPPED", "NOT_CONFIGURED"
    )
    with sessions() as db:
        assert notify.health(db)["state"] == "NOT_CONFIGURED"


def test_a_withdrawn_confirmation_waits_then_expires(store):
    sessions, _ = store
    event_id = punch(sessions)
    confirm(sessions, event_id)
    with sessions() as db:
        db.get(AttendanceEvent, event_id).ords_status = "PENDING"
        db.commit()
    recorder = Recorder()
    deliver(recorder)
    row = row_for(sessions, event_id)
    assert row.status == "PENDING" and row.next_attempt_at is not None
    with sessions() as db:
        db.get(AttendanceEvent, event_id).device_event_time = utc_now() - timedelta(hours=13)
        db.commit()
    move_back(sessions, event_id, next_attempt_at=utc_now() - timedelta(seconds=1),
              punch_time=utc_now() - timedelta(hours=13))
    deliver(recorder)
    assert row_for(sessions, event_id).reason == "CONFIRMATION_WITHDRAWN"
    assert recorder.requests == []


def test_logs_never_contain_the_key_or_a_full_cnic(store, caplog):
    sessions, _ = store
    caplog.set_level(logging.DEBUG)
    for response in (httpx.Response(200), httpx.Response(503), httpx.Response(400)):
        event_id = punch(sessions, cnic=CNIC)
        confirm(sessions, event_id)
        deliver(Recorder(response))
        with sessions() as db:
            db.execute(text("UPDATE add_attendance_checkin_notifications SET punch_time = :t"),
                       {"t": utc_now() - timedelta(days=1)})
            db.commit()
    rendered = caplog.text
    assert "Check-in notification" in rendered
    assert SYNTHETIC_KEY not in rendered and CNIC not in rendered
    assert "*****-****567-1" in rendered


def test_alerts_raise_for_failures_and_backlog_then_clear(store, monkeypatch):
    sessions, connector_id = store
    monkeypatch.setattr(settings, "checkin_notify_failure_alert_count", 1)
    event_id = punch(sessions)
    confirm(sessions, event_id)
    deliver(Recorder(httpx.Response(400)))
    notify.reconcile_alerts()
    with sessions() as db:
        alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == notify.ALERT_CODE))
        assert alert.state == "OPEN" and alert.connector_id == connector_id
        assert alert.details["failed_last_hour"] == 1
        db.execute(text("UPDATE add_attendance_checkin_notifications SET updated_at = :t"),
                   {"t": utc_now() - timedelta(hours=2)})
        db.commit()
    assert notify.reconcile_alerts() == 1
    with sessions() as db:
        assert db.scalar(select(DeviceAlert.state).where(DeviceAlert.code == notify.ALERT_CODE)) == "RESOLVED"
    waiting = punch(sessions, cnic=OTHER_CNIC)
    confirm(sessions, waiting)
    move_back(sessions, waiting, created_at=utc_now() - timedelta(hours=1))
    notify.reconcile_alerts()
    with sessions() as db:
        open_alert = db.scalar(select(DeviceAlert).where(
            DeviceAlert.code == notify.ALERT_CODE, DeviceAlert.state == "OPEN"))
        assert open_alert.details["waiting"] == 1
        assert notify.health(db)["state"] == "DEGRADED"


# --------------------------------------------------------------------------
# API: pills, filter, runtime switch, retry
# --------------------------------------------------------------------------


@pytest.fixture()
def admin(store, monkeypatch):
    from zk_add.web import app, get_db

    sessions, _ = store
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("correct-password"))
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    with sessions() as db:
        raw, row = create_admin_session(
            db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
        )
        db.commit()
        csrf = row.csrf_token

    def override_db():
        db = sessions()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw)
    yield client, {"X-CSRF-Token": csrf}
    app.dependency_overrides.clear()


def test_attendance_api_serializes_pills_for_old_and_new_rows_and_filters(store, admin):
    sessions, _ = store
    client, _headers = admin
    old = punch(sessions, minutes_ago=3 * 24 * 60, status="ACKED_CHECK")
    with sessions() as db:
        db.execute(text("DELETE FROM add_attendance_checkin_notifications"))
        db.commit()
    waiting = punch(sessions, cnic=OTHER_CNIC)
    sent = punch(sessions)
    confirm(sessions, sent)
    deliver(Recorder())
    response = client.get("/api/v1/attendance")
    assert response.status_code == 200, response.text
    payload = response.json()
    pills = {row["id"]: row["checkin_notification"] for row in payload["rows"]}
    assert pills[old]["status"] == "NOT_SENT" and pills[old]["reason"] == "BEFORE_FEATURE"
    assert pills[waiting]["status"] == "NOT_SENT" and pills[waiting]["reason"] == "AWAITING_ORACLE"
    assert pills[sent]["status"] == "NOTIFIED" and pills[sent]["sent_at"]
    assert payload["checkin_notifications"]["last_24h"]["notified"] == 1
    assert payload["checkin_notifications"]["state"] == "OK"
    notified = client.get("/api/v1/attendance", params={"notify_status": "NOTIFIED"}).json()
    assert [row["id"] for row in notified["rows"]] == [sent]
    not_sent = client.get("/api/v1/attendance", params={"notify_status": "NOT_SENT"}).json()
    assert {row["id"] for row in not_sent["rows"]} == {old, waiting}
    both = client.get(
        "/api/v1/attendance", params=[("notify_status", "NOT_SENT"), ("notify_status", "NOTIFIED")]
    ).json()
    assert {row["id"] for row in both["rows"]} == {old, waiting, sent}
    assert client.get("/api/v1/attendance", params={"notify_status": "BOGUS"}).status_code == 422
    older_page = client.get("/api/v1/attendance", params={"cursor": sent}).json()
    assert older_page["checkin_notifications"] is None
    assert CNIC not in response.text and SYNTHETIC_KEY not in response.text
    overview = client.get("/api/v1/overview").json()["checkin_notifications"]
    assert overview["mode"] == "ALL" and overview["configured"] is True


def test_runtime_mode_switch_requires_step_up_and_is_audited(store, admin):
    sessions, _ = store
    client, headers = admin
    current = client.get("/api/v1/checkin-notifications").json()
    assert current["mode"] == "ALL" and current["endpoint_host"] == "ords.example.invalid"
    body = {
        "mode": "PILOT", "punch_policy": "FIRST_PER_DAY", "pilot_zone_ids": ["ZONE-PILOT"],
        "add_pilot_cnics": [CNIC], "reason": "Pilot with the owner's own CNIC",
        "password": "wrong-password", "expected_revision": current["revision"],
    }
    assert client.put("/api/v1/checkin-notifications/settings", json=body).status_code == 403  # CSRF
    refused = client.put("/api/v1/checkin-notifications/settings", json=body, headers=headers)
    assert refused.status_code == 403
    body["password"] = "correct-password"
    accepted = client.put("/api/v1/checkin-notifications/settings", json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert (result["mode"], result["punch_policy"], result["pilot_zone_ids"]) == (
        "PILOT", "FIRST_PER_DAY", ["ZONE-PILOT"]
    )
    assert [item["masked"] for item in result["pilot_cnics"]] == ["*****-****567-1"]
    assert CNIC not in accepted.text
    stale = client.put("/api/v1/checkin-notifications/settings", json=body, headers=headers)
    assert stale.status_code == 409
    body.update(
        mode="ALL", add_pilot_cnics=[], remove_pilot_cnic_ids=[result["pilot_cnics"][0]["id"]],
        expected_revision=result["revision"],
    )
    switched = client.put("/api/v1/checkin-notifications/settings", json=body, headers=headers)
    assert switched.json()["mode"] == "ALL" and switched.json()["pilot_cnics"] == []
    with sessions() as db:
        audits = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "CHECKIN_NOTIFY_SETTINGS_UPDATED").order_by(AuditEvent.id)).all()
        assert [(a.before["mode"], a.after["mode"]) for a in audits] == [("ALL", "PILOT"), ("PILOT", "ALL")]
        assert CNIC not in json.dumps([a.after for a in audits] + [a.before for a in audits])
        assert db.get(CheckinNotifyControl, 1).mode == "ALL"


def test_admin_can_retry_only_a_failed_notification(store, admin):
    sessions, _ = store
    client, headers = admin
    event_id = punch(sessions)
    confirm(sessions, event_id)
    deliver(Recorder(httpx.Response(400)))
    path = f"/api/v1/attendance/{event_id}/checkin-notification/retry"
    body = {"reason": "HRM fixed the employee record", "password": "correct-password"}
    assert client.post(path, json=body).status_code == 403
    retried = client.post(path, json=body, headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["checkin_notification"]["status"] == "PENDING"
    assert client.post(path, json=body, headers=headers).status_code == 409
    # An administrator retry is allowed after the automatic age window.
    move_back(sessions, event_id, punch_time=utc_now() - timedelta(hours=20))
    recorder = Recorder()
    deliver(recorder)
    assert len(recorder.requests) == 1 and row_for(sessions, event_id).status == "SENT"
    with sessions() as db:
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "CHECKIN_NOTIFY_RETRY"))
        assert audit.target_id == str(event_id)
    missing = client.post("/api/v1/attendance/999999/checkin-notification/retry", json=body,
                          headers=headers)
    assert missing.status_code == 404


# --------------------------------------------------------------------------
# Migration and PostgreSQL concurrency
# --------------------------------------------------------------------------


def load_migration():
    path = ROOT / "apps/add_backend/migrations/versions/20261022_0054_checkin_notifications.py"
    spec = importlib.util.spec_from_file_location("checkin_notify_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NEW_TABLES = {"add_attendance_checkin_notifications", "add_checkin_notify_control"}


def check_migration(engine, monkeypatch):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = load_migration()
    assert migration.down_revision == "20261012_0053"
    with engine.begin() as connection:
        for table in NEW_TABLES:
            connection.execute(text(f"DROP TABLE {table}"))
        before = set(sa_inspect(connection).get_table_names())
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()  # Idempotent on a partially applied install.
        assert set(sa_inspect(connection).get_table_names()) == before | NEW_TABLES
        drift = [
            diff for diff in compare_metadata(context, Base.metadata)
            if any(name in repr(diff) for name in NEW_TABLES | {"checkin_notify"})
        ]
        assert drift == []
        migration.downgrade()
        assert set(sa_inspect(connection).get_table_names()) == before
        migration.upgrade()


def test_migration_only_creates_new_tables_sqlite(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    check_migration(engine, monkeypatch)
    source = (ROOT / "apps/add_backend/migrations/versions/20261022_0054_checkin_notifications.py").read_text()
    for forbidden in ("alter_column", "add_column", "execute(", "ForeignKey", "bulk_insert"):
        assert forbidden not in source


@pytest.fixture()
def pg(store, monkeypatch):
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "checkin_notify_test_" + uuid4().hex
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000"},
        pool_size=10,
    )
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    monkeypatch.setattr("zk_add.db.SessionLocal", sessions)
    with sessions() as db:
        make_connector(db, "e0:72:a1:00:00:51", "ZONE-PILOT")
        notify.ensure_control(db)
        db.get(CheckinNotifyControl, 1).created_at = utc_now() - timedelta(days=1)
        db.commit()
    try:
        yield sessions, engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def test_postgres_concurrent_confirmations_create_exactly_one_row(pg):
    sessions, _engine = pg
    event_id = punch(sessions)
    barrier = Barrier(4)

    def confirm_once(path):
        barrier.wait()
        confirm(sessions, event_id, path=path)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(confirm_once, ["ADD_DELIVERY", "ORDS_MEMBERSHIP_CHECK"] * 2))
    with sessions() as db:
        assert db.scalar(select(func.count(Notification.id))) == 1


def test_postgres_concurrent_claimers_never_send_one_cnic_twice(pg):
    sessions, _engine = pg
    first = punch(sessions, minutes_ago=2)
    repeat = punch(sessions, minutes_ago=1.5)
    other = punch(sessions, cnic=OTHER_CNIC)
    for event_id in (first, repeat, other):
        confirm(sessions, event_id)
    barrier = Barrier(3)

    def claim_one(_):
        barrier.wait()
        return notify.claim_batch(1).jobs

    with ThreadPoolExecutor(3) as pool:
        claimed = [job for jobs in pool.map(claim_one, range(3)) for job in jobs]
    assert len({job.row_id for job in claimed}) == len(claimed)
    with sessions() as db:
        states = dict(db.execute(select(Notification.attendance_event_id, Notification.status)).all())
    assert sorted(states.values()) == ["SENDING", "SENDING", "SKIPPED"] or (
        sorted(states.values()).count("SENDING") <= 2 and states[repeat] != "SENDING"
        or states[first] != "SENDING"
    )
    assert not (states[first] == "SENDING" and states[repeat] == "SENDING")


def test_postgres_migration_up_and_down(pg, monkeypatch):
    _sessions, engine = pg
    check_migration(engine, monkeypatch)
