from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_hil_runs import ready  # noqa: F401
from test_hil_scope import hil_session  # noqa: F401
from zk_add import hil_transport
from zk_add.hil_runs import _release_identity
from zk_add.hil_transport import KEY, TransportInterrupted, enforce_transport, start_interruption
from zk_add.ota import FirmwareEvent, FirmwareHilRun
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import TARGETS


@pytest.fixture
def observed(ready, monkeypatch):  # noqa: F811
    session, release, device, deployment, job, coverage, telemetry = ready
    target = TARGETS[0].identity
    device.connector_id, device.hardware_id = target.connector_id, target.mac
    device.zkt_custody_enabled = True
    device.zkt_device.online = True
    device.zkt_device.serial = device.zkt_device.expected_serial = (
        device.zkt_device.confirmed_serial
    ) = target.terminal_serial
    device.firmware_family = "zkt"
    release.version, release.release_id = "2.7.0", "zone-lite-2.7.0"
    release.manifest = {**release.manifest, "runtime_profile": "ZKT_JOURNAL_V1"}
    now = utc_now()
    payload = deepcopy(telemetry.payload)
    payload["_trusted_envelope_sent_at"] = now.isoformat()
    payload["ota"]["running_version"] = release.version
    payload["zkt"].update(serial=target.terminal_serial, online=True)
    diagnostics = payload["diagnostics"]
    diagnostics.update(
        schema_version=2,
        runtime_profile="ZKT_JOURNAL_V1",
        journal_format=1,
        delivery_authority="ADD",
        boot_id=device.boot_id,
        sampled_uptime_ms=100000,
    )
    diagnostics["workers"] = [
        {"name": name, "state": "RUNNING", "last_activity_uptime_ms": 100000}
        for name in ("capture", "storage_owner", "add_delivery")
    ]
    diagnostics["queues"] = [
        {"name": name, "count_known": True, "records": 0}
        for name in ("journal", "legacy_migration")
    ]
    diagnostics["journal_storage"] = {
        "observed": True,
        "fresh": True,
        "ready": True,
        "durability": "HEALTHY",
        "checkpoint_recovery_pending": False,
        "sampled_uptime_ms": 100000,
    }
    telemetry.payload, telemetry.created_at = payload, now
    run = FirmwareHilRun(
        run_id="11111111-2222-4333-8444-555555555555",
        deployment_id=deployment.id,
        connector_id=device.id,
        release_id=release.id,
        actor="admin",
        idempotency_key="observation",
        status="OBSERVING",
        target=target.model_dump(),
        release_identity=_release_identity(release).model_dump(mode="json"),
        started_at=now - timedelta(seconds=180),
        ends_at=now + timedelta(seconds=720),
        result={},
        baseline={
            "profile": "FULL_REMOTE_HIL_V1",
            "boot_id": device.boot_id,
            "source_generation": 1,
            "source_cursor": 100,
        },
    )
    session.add(run)
    session.commit()
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now)
    monkeypatch.setattr(
        hil_transport,
        "server_clock",
        lambda: (
            "synthetic-kernel-boot",
            60000 + int((hil_transport.utc_now() - now).total_seconds() * 1000),
        ),
    )
    return session, release, device, deployment, telemetry, run, now


def begin(observed, **kwargs):
    return start_interruption(
        observed[0],
        observed[5].run_id,
        **{"actor": "admin", "idempotency_key": "outage-unique", **kwargs},
    )


def test_fixed_deadline_durable_replay_and_server_restart(observed, monkeypatch):
    session, _, device, _, _, run, now = observed
    value, created = begin(observed)
    assert created and datetime.fromisoformat(value["expires_at"]) - now == timedelta(seconds=30)
    session.commit()
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(seconds=10))
    repeated, created = begin(observed)
    assert repeated == value and not created
    session.commit()
    # A new session has no timer or process-local memory of the interruption.
    with Session(session.get_bind()) as restarted:
        target = restarted.get(type(device), device.id)
        with pytest.raises(TransportInterrupted) as error:
            enforce_transport(restarted, target, transport="HTTP")
        assert error.value.retry_after == 20
    session.expire_all()
    assert run.result[KEY]["rejected_at"]
    assert run.result[KEY]["outcome"] == "NOT_EVALUATED"
    assert run.status == "OBSERVING"
    assert [
        event.state for event in session.scalars(select(FirmwareEvent).order_by(FirmwareEvent.id))
    ] == ["HIL_ADD_INTERRUPT_STARTED", "HIL_ADD_INTERRUPT_OBSERVED"]


@pytest.mark.parametrize("transport", ["HTTP", "WEBSOCKET"])
def test_boundary_expiry_restores_without_operator_or_worker(observed, monkeypatch, transport):
    session, _, device, _, _, run, now = observed
    begin(observed)
    session.commit()
    monkeypatch.setattr(
        hil_transport, "utc_now", lambda: now + timedelta(seconds=29, microseconds=999999)
    )
    with pytest.raises(TransportInterrupted) as error:
        enforce_transport(session, device, transport=transport)
    assert error.value.retry_after == 1
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(seconds=30))
    assert enforce_transport(session, device, transport=transport) is None
    session.commit()
    assert run.result[KEY]["transport_restored_at"] == (now + timedelta(seconds=30)).isoformat()
    assert run.result[KEY]["outcome"] == "NOT_EVALUATED"
    first = deepcopy(run.result)
    assert enforce_transport(session, device, transport=transport) is None
    assert run.result == first
    assert not list(
        session.scalars(select(FirmwareEvent).where(FirmwareEvent.state == "HIL_ACCEPTED"))
    )


@pytest.mark.parametrize(
    "change",
    [
        "early",
        "late",
        "stale",
        "stale-envelope",
        "wrong-boot",
        "wrong-image",
        "wrong-mac",
        "bridge",
        "profile",
        "revoked",
        "paused",
        "pending-deployment",
        "custody-disabled",
        "storage",
        "storage-owner",
        "busy-queue",
        "unknown-queue",
        "stale-worker",
        "missing-worker",
        "duplicate-worker",
        "terminal-offline",
        "source-pending",
        "changed-release",
    ],
)
def test_unsafe_interruption_never_creates_control(observed, monkeypatch, change):
    session, release, device, deployment, telemetry, run, now = observed
    payload = deepcopy(telemetry.payload)
    diagnostics = payload["diagnostics"]
    if change == "early":
        monkeypatch.setattr(hil_transport, "utc_now", lambda: now - timedelta(microseconds=1))
    if change == "late":
        monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(seconds=60))
    if change == "stale":
        telemetry.created_at = now - timedelta(seconds=46)
    if change == "stale-envelope":
        payload["_trusted_envelope_sent_at"] = (now - timedelta(seconds=46)).isoformat()
    if change == "wrong-boot":
        device.boot_id = "different"
    if change == "wrong-image":
        payload["ota"]["image_sha256"] = "f" * 64
    if change == "wrong-mac":
        device.hardware_id = "11:22:33:44:55:66"
    if change == "bridge":
        release.version = "2.6.18"
    if change == "profile":
        run.baseline = {**run.baseline, "profile": "BRIDGE_READINESS_V1"}
    if change == "revoked":
        release.state = "REVOKED"
    if change == "paused":
        from zk_add.ota import FirmwareCampaign

        session.get(FirmwareCampaign, deployment.campaign_id).status = "PAUSED"
    if change == "pending-deployment":
        deployment.status = "RECONCILING"
    if change == "custody-disabled":
        device.zkt_custody_enabled = False
    if change == "storage":
        diagnostics["storage"]["persistence_verified"] = False
    if change == "storage-owner":
        diagnostics["journal_storage"]["ready"] = False
    if change == "busy-queue":
        diagnostics["queues"][0]["records"] = 1
    if change == "unknown-queue":
        diagnostics["queues"][0]["count_known"] = False
    if change == "stale-worker":
        diagnostics["workers"][0]["last_activity_uptime_ms"] = 0
    if change == "missing-worker":
        diagnostics["workers"].pop()
    if change == "duplicate-worker":
        diagnostics["workers"].append(diagnostics["workers"][0])
    if change == "terminal-offline":
        payload["zkt"]["online"] = False
    if change == "source-pending":
        diagnostics["committed_source_cursor"] = 99
    if change == "changed-release":
        release.image_sha256 = "f" * 64
    telemetry.payload = payload
    session.flush()
    with pytest.raises(ValueError):
        begin(observed)
    assert run.result == {} and not list(session.scalars(select(FirmwareEvent)))


def test_no_repeated_or_retargeted_fault_and_cancel_immediately_restores(observed, monkeypatch):
    session, _, device, _, _, run, now = observed
    begin(observed)
    session.commit()
    for kwargs in ({"actor": "another"}, {"idempotency_key": "different-key"}):
        with pytest.raises(ValueError, match="already has"):
            begin(observed, **kwargs)
    # Binding/family changes cannot transfer an active fault to a new target.
    for name, value in (("firmware_family", "hikvision"), ("hardware_id", "11:22:33:44:55:66")):
        previous = getattr(device, name)
        setattr(device, name, value)
        assert enforce_transport(session, device, transport="HTTP") is None
        setattr(device, name, previous)
    run.status = "CANCELLED"
    session.commit()
    assert enforce_transport(session, device, transport="HTTP") is None
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(hours=1))
    _, created = begin(observed)
    assert not created and run.status == "CANCELLED"


@pytest.mark.parametrize(
    "fault", ["naive", "too-long", "wrong-run", "future", "wrong-kind", "missing-end"]
)
def test_malformed_control_cannot_extend_an_outage(observed, fault):
    session, _, device, _, _, run, now = observed
    value, _ = begin(observed)
    value = deepcopy(value)
    if fault == "naive":
        value["expires_at"] = now.replace(tzinfo=None).isoformat()
    if fault == "too-long":
        value["expires_at"] = (now + timedelta(hours=1)).isoformat()
    if fault == "wrong-run":
        value["run_id"] = "another-run"
    if fault == "future":
        value["started_at"] = (now + timedelta(seconds=1)).isoformat()
        value["expires_at"] = (now + timedelta(seconds=31)).isoformat()
    if fault == "wrong-kind":
        value["kind"] = "ESP_REBOOT"
    if fault == "missing-end":
        value.pop("expires_at")
    run.result = {KEY: value}
    session.commit()
    assert enforce_transport(session, device, transport="WEBSOCKET") is None


def test_http_guard_returns_retry_after_before_endpoint_processing(observed):
    from fastapi import HTTPException
    from zk_add.web import _guard_hil_http

    session, _, device, _, _, run, _ = observed
    begin(observed)
    session.commit()
    with pytest.raises(HTTPException) as error:
        _guard_hil_http(session, device)
    assert error.value.status_code == 503 and error.value.headers == {"Retry-After": "30"}
    assert run.result[KEY]["rejected_transport"] == "HTTP"


def test_expired_replay_does_not_restart_or_close_transport(observed, monkeypatch):
    _, _, _, _, _, _, now = observed
    value, _ = begin(observed)
    observed[0].commit()
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(days=1))
    repeated, created = begin(observed)
    assert repeated == value and not created
    assert ensure_utc(datetime.fromisoformat(repeated["expires_at"])) == now + timedelta(seconds=30)


def test_stream_receipt_path_is_stopped_before_any_sequence_or_custody_change(
    observed, monkeypatch
):
    from contextlib import contextmanager
    from sqlalchemy import func
    from zk_add import web
    from zk_add.models import AttendanceEvent, DeviceTelemetry
    from zk_add.schemas import Envelope

    session, _, device, _, _, run, now = observed
    begin(observed)
    session.commit()
    connector_id, sequence = device.id, device.last_sequence
    original_counts = [
        session.scalar(select(func.count()).select_from(table))
        for table in (AttendanceEvent, DeviceTelemetry)
    ]

    @contextmanager
    def isolated_session():
        with Session(session.get_bind()) as current:
            yield current
            current.commit()

    monkeypatch.setattr(web, "session_scope", isolated_session)
    envelope = Envelope(
        message_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        type="heartbeat",
        connector_id=device.connector_id,
        boot_id=device.boot_id,
        seq=sequence + 1,
        sent_at=now,
        payload={},
    )
    with pytest.raises(TransportInterrupted):
        web.persist_envelope(connector_id, envelope)
    session.expire_all()
    assert device.last_sequence == sequence
    assert [
        session.scalar(select(func.count()).select_from(table))
        for table in (AttendanceEvent, DeviceTelemetry)
    ] == original_counts
    assert run.result[KEY]["rejected_transport"] == "WEBSOCKET"


def test_background_close_requires_same_active_run_and_deadline(observed, monkeypatch):
    from zk_add.hil_transport import pending_stream_close

    session, _, device, _, _, run, now = observed
    value, _ = begin(observed)
    assert pending_stream_close(session, run.run_id, value["control_id"]) == (
        device.connector_id,
        now + timedelta(seconds=30),
    )
    assert pending_stream_close(session, run.run_id, "different-id") is None
    run.status = "CANCELLED"
    session.flush()
    assert pending_stream_close(session, run.run_id, value["control_id"]) is None
    run.status = "OBSERVING"
    session.flush()
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(seconds=30))
    assert pending_stream_close(session, run.run_id, value["control_id"]) is None


def test_socket_close_is_targeted_and_cannot_run_after_expiry(monkeypatch):
    import asyncio
    from zk_add import realtime

    now = utc_now()
    monkeypatch.setattr(realtime, "utc_now", lambda: now)
    closed = []

    class Socket:
        async def close(self, code, reason):
            closed.append(self)
            assert code == 1013

    async def exercise():
        hub = realtime.ConnectorHub()
        one, other = Socket(), Socket()
        hub._connections.update(one=one, other=other)
        assert not await hub.interrupt_until("one", now)
        assert not closed
        assert await hub.interrupt_until("one", now + timedelta(seconds=30))
        assert closed == [one]
        assert not await hub.interrupt_until("absent", now + timedelta(seconds=30))
        assert closed == [one]

    asyncio.run(exercise())


def test_blocked_socket_close_is_bounded_by_test_deadline(monkeypatch):
    import asyncio
    from zk_add import realtime

    now = utc_now()
    monkeypatch.setattr(realtime, "utc_now", lambda: now)
    cancelled = []

    class Socket:
        async def close(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    async def exercise():
        hub = realtime.ConnectorHub()
        hub._connections["one"] = Socket()
        assert not await hub.interrupt_until("one", now + timedelta(milliseconds=20))
        assert cancelled == [True]

    asyncio.run(exercise())


def test_wall_clock_step_cannot_extend_monotonic_deadline(observed, monkeypatch):
    session, _, device, _, _, run, now = observed
    begin(observed)
    session.commit()
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(seconds=5))
    monkeypatch.setattr(hil_transport, "server_clock", lambda: ("synthetic-kernel-boot", 89999))
    with pytest.raises(TransportInterrupted) as error:
        enforce_transport(session, device, transport="HTTP")
    assert error.value.retry_after == 1
    monkeypatch.setattr(hil_transport, "server_clock", lambda: ("synthetic-kernel-boot", 90000))
    assert enforce_transport(session, device, transport="HTTP") is None
    assert run.result[KEY]["expiry_reason"] == "DEADLINE"
    session.commit()
    # The wall clock is still inside the 30-second window; restoration holds.
    assert enforce_transport(session, device, transport="HTTP") is None


@pytest.mark.parametrize(
    "fault", ["host-reboot", "clock-unavailable", "monotonic-regression", "wall-regression"]
)
def test_clock_discontinuity_aborts_and_cannot_reactivate(observed, monkeypatch, fault):
    from zk_add.hil_transport import pending_stream_close

    session, _, device, _, _, run, now = observed
    value, _ = begin(observed)
    session.commit()
    if fault == "host-reboot":
        monkeypatch.setattr(hil_transport, "server_clock", lambda: ("different-kernel-boot", 60001))
    elif fault == "clock-unavailable":
        monkeypatch.setattr(hil_transport, "server_clock", lambda: None)
    elif fault == "monotonic-regression":
        monkeypatch.setattr(hil_transport, "server_clock", lambda: ("synthetic-kernel-boot", 59999))
    else:
        monkeypatch.setattr(hil_transport, "utc_now", lambda: now - timedelta(seconds=1))
        monkeypatch.setattr(hil_transport, "server_clock", lambda: ("synthetic-kernel-boot", 60001))
    assert enforce_transport(session, device, transport="HTTP") is None
    session.commit()
    assert run.result[KEY]["expiry_reason"] in {"SERVER_CLOCK_CHANGED", "WALL_CLOCK_REGRESSED"}
    assert run.result[KEY]["outcome"] == "NOT_EVALUATED"
    monkeypatch.setattr(hil_transport, "server_clock", lambda: ("synthetic-kernel-boot", 60002))
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now + timedelta(seconds=1))
    assert enforce_transport(session, device, transport="HTTP") is None
    assert pending_stream_close(session, run.run_id, value["control_id"]) is None


def test_missing_server_clock_cannot_start_fault(observed, monkeypatch):
    monkeypatch.setattr(hil_transport, "server_clock", lambda: None)
    with pytest.raises(ValueError, match="monotonic"):
        begin(observed)
    assert observed[5].result == {}


@pytest.mark.parametrize("target", [{}, {"connector_id": "broken"}, []])
def test_invalid_stored_target_cannot_block_transport(observed, target):
    session, _, device, _, _, run, _ = observed
    value, _ = begin(observed)
    run.target = target
    session.commit()
    assert enforce_transport(session, device, transport="HTTP") is None
    assert hil_transport.pending_stream_close(session, run.run_id, value["control_id"]) is None


def test_linux_clock_is_shared_across_worker_processes():
    import inspect
    import json
    import subprocess
    import sys

    before = hil_transport.server_clock()
    if before is None:
        pytest.skip("Linux kernel boot identity is unavailable on this host")
    source = (
        "from pathlib import Path\nfrom uuid import UUID\nimport time, json\n"
        + inspect.getsource(hil_transport.server_clock)
        + "\nprint(json.dumps(server_clock()))\n"
    )
    other = json.loads(subprocess.check_output([sys.executable, "-c", source], text=True, timeout=10))
    after = hil_transport.server_clock()
    assert before[0] == other[0] == after[0]
    assert before[1] <= other[1] <= after[1]


@pytest.mark.parametrize("value", ["", "not-a-uuid", "1234"])
def test_malformed_kernel_identity_cannot_supply_a_deadline(monkeypatch, value):
    monkeypatch.setattr(hil_transport.Path, "read_text", lambda _: value)
    assert hil_transport.server_clock() is None
