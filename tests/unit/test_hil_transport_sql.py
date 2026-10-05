"""Actual PostgreSQL/SQLite concurrency; the disposable source-store fixture isolates every run."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier

from sqlalchemy import func, select

from test_hil_transport import observed  # noqa: F401
from test_hil_runs import ready  # noqa: F401
from test_hil_scope import hil_session  # noqa: F401
from test_zkt_source_load import store  # noqa: F401
from zk_add import hil_transport
from zk_add.models import Connector, DeviceTelemetry, ZKTDevice
from zk_add.ota import FirmwareCampaign, FirmwareEvent, FirmwareHilRun, FirmwareRelease


def copy_row(session, original, **overrides):
    model = type(original)
    values = {column.name: deepcopy(getattr(original, column.name))
              for column in model.__table__.columns if column.name != "id"}
    row = model(**{**values, **overrides})
    session.add(row)
    session.flush()
    return row


def prepare(store, observed):  # noqa: F811
    original, release, connector, deployment, telemetry, run, _ = observed
    with store() as session:
        device = copy_row(session, connector)
        terminal = copy_row(session, connector.zkt_device, connector_id=device.id)
        assert isinstance(terminal, ZKTDevice)
        image = copy_row(session, release)
        campaign = copy_row(session, original.get(FirmwareCampaign, deployment.campaign_id), release_id=image.id)
        attempt = copy_row(session, deployment, connector_id=device.id, release_id=image.id, campaign_id=campaign.id)
        sample = copy_row(session, telemetry, connector_id=device.id)
        observation = copy_row(session, run, connector_id=device.id, release_id=image.id, deployment_id=attempt.id)
        session.commit()
        assert isinstance(image, FirmwareRelease) and isinstance(sample, DeviceTelemetry)
        assert isinstance(observation, FirmwareHilRun)
        return device.id, observation.run_id


def test_concurrent_fault_start_has_one_durable_identity(store, observed):  # noqa: F811
    connector_id, run_id = prepare(store, observed)
    start = Barrier(2)

    def request():
        with store() as session:
            start.wait(timeout=10)
            result = hil_transport.start_interruption(session, run_id, actor="admin", idempotency_key="same-fault-key")
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(request) for _ in range(2)]
        results = [future.result(timeout=20) for future in pending]
    assert sum(created for _, created in results) == 1
    assert results[0][0] == results[1][0]
    with store() as session:
        assert session.scalar(select(func.count()).select_from(FirmwareEvent)
            .where(FirmwareEvent.state == "HIL_ADD_INTERRUPT_STARTED")) == 1
        assert session.get(Connector, connector_id).connected


def test_expiry_and_admin_replay_share_ingestion_lock_order(store, observed, monkeypatch):  # noqa: F811
    connector_id, run_id = prepare(store, observed)
    with store() as session:
        control, _ = hil_transport.start_interruption(session, run_id, actor="admin", idempotency_key="same-fault-key")
        session.commit()
    now = observed[-1] + timedelta(seconds=30)
    monkeypatch.setattr(hil_transport, "utc_now", lambda: now)
    start = Barrier(2)

    def request(replay):
        with store() as session:
            start.wait(timeout=10)
            if replay:
                value, created = hil_transport.start_interruption(session, run_id, actor="admin", idempotency_key="same-fault-key")
                assert not created and value["control_id"] == control["control_id"]
            else:
                hil_transport.enforce_transport(session, session.get(Connector, connector_id), transport="HTTP")
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(request, replay) for replay in (False, True)]
        for future in pending:
            future.result(timeout=20)
    with store() as session:
        run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id))
        assert run.result[hil_transport.KEY]["transport_restored_at"] == now.isoformat()
        assert run.result[hil_transport.KEY]["expires_at"] == control["expires_at"]


def test_actual_admin_route_commits_before_targeted_close_and_replay_is_inert(store, observed, monkeypatch):  # noqa: F811
    from contextlib import contextmanager
    from fastapi.testclient import TestClient
    from zk_add import web
    from zk_add.security import ADMIN_COOKIE, create_admin_session

    connector_id, run_id = prepare(store, observed)
    with store() as session:
        token, admin = create_admin_session(session, username="StateHealthAdmin", ip_address="127.0.0.1",
                                             user_agent="isolated-hil-test")
        session.commit()

    def get_session():
        with store() as session:
            yield session

    @contextmanager
    def scope():
        with store() as session:
            yield session
            session.commit()

    closed = []

    async def close(target_id, expires_at):
        with store() as session:
            run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id))
            assert run.result[hil_transport.KEY]["control_id"]
            assert target_id == session.get(Connector, connector_id).connector_id
            closed.append(target_id)
        return True

    def step_up(password, *_):
        assert password == "synthetic-test-password"

    monkeypatch.setattr(web, "session_scope", scope)
    monkeypatch.setattr(web, "require_step_up", step_up)
    monkeypatch.setattr(web.connector_hub, "interrupt_until", close)
    monkeypatch.setitem(web.app.dependency_overrides, web.get_db, get_session)
    client = TestClient(web.app)
    client.cookies.set(ADMIN_COOKIE, token)
    path = f"/api/v1/firmware/hil-runs/{run_id}/interrupt-add"
    body = {"password": "synthetic-test-password", "idempotency_key": "api-fault-key"}
    headers = {"X-CSRF-Token": admin.csrf_token}
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 200 and first.json()["created"]
    second = client.post(path, json=body, headers=headers)
    assert second.status_code == 200 and not second.json()["created"]
    assert first.json()["control"] == second.json()["control"]
    assert len(closed) == 1
