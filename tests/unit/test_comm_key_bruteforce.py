import threading
import time

import pytest
from sqlalchemy.orm import sessionmaker

from zk_zone_agent.bruteforce import BruteForceStart, CommKeyBruteforceManager
from zk_zone_agent.crypto import unprotect_secret
from zk_zone_agent.db import Base, Device, DeviceDiscoveryResult, CommKeyBruteforceJob, create_sqlite_engine
from zk_zone_agent.settings import settings
from zk_zone_agent.zk_client import ZKDeviceInfo


class _FakeZKClient:
    def __init__(self, *, ip, port=4370, comm_key=0, timeout=0.75):
        self.ip = ip
        self.port = port
        self.comm_key = comm_key
        self.timeout = timeout
        self.connected = False

    def connect(self):
        if self.comm_key != 1979:
            raise RuntimeError("bad comm key")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def get_info(self):
        return ZKDeviceInfo("ADZV211860253", "ZLM60_TFT", "MB20/0")

    def get_users(self):
        return []

    def get_time(self):
        raise NotImplementedError

    def get_attendance(self):
        return []

    def live_capture(self, new_timeout=5):
        yield from ()


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def test_bruteforce_finds_comm_key_and_updates_candidate(monkeypatch, session_factory):
    monkeypatch.setattr(settings, "bruteforce_enabled", True)
    monkeypatch.setattr(settings, "bruteforce_chunk_size", 4)
    stop_event = threading.Event()
    manager = CommKeyBruteforceManager(
        session_factory=session_factory,
        client_factory=_FakeZKClient,
        stop_event=stop_event,
    )
    with session_factory() as session:
        candidate = DeviceDiscoveryResult(ip="192.168.110.137", port=4370, status="NEEDS_COMM_KEY")
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    job = manager.start_job(
        BruteForceStart(
            candidate_id=candidate_id,
            ip="192.168.110.137",
            port=4370,
            mode="AGGRESSIVE",
            range_start=1900,
            range_end=2000,
            worker_count=2,
            timeout_seconds=0.01,
        )
    )

    _wait_for_status(session_factory, job.id, "SUCCEEDED")
    stop_event.set()

    with session_factory() as session:
        finished = session.get(CommKeyBruteforceJob, job.id)
        candidate = session.get(DeviceDiscoveryResult, candidate_id)
        assert finished is not None
        assert candidate is not None
        assert unprotect_secret(finished.found_key_encrypted) == "1979"
        assert candidate.status == "VALIDATED_ZK"
        assert candidate.serial == "ADZV211860253"


def test_bruteforce_clamps_aggressive_worker_count(monkeypatch, session_factory):
    monkeypatch.setattr(settings, "bruteforce_enabled", True)
    monkeypatch.setattr(settings, "bruteforce_hard_per_device_workers", 8)
    manager = CommKeyBruteforceManager(
        session_factory=session_factory,
        client_factory=_FakeZKClient,
        stop_event=threading.Event(),
    )

    job = manager.start_job(
        BruteForceStart(
            candidate_id=None,
            ip="192.168.1.50",
            port=4370,
            mode="AGGRESSIVE",
            range_start=1979,
            range_end=1979,
            worker_count=99,
            timeout_seconds=0.01,
        )
    )

    assert job.worker_count == 8


def test_bruteforce_rejects_configured_live_device(monkeypatch, session_factory):
    monkeypatch.setattr(settings, "bruteforce_enabled", True)
    manager = CommKeyBruteforceManager(session_factory=session_factory, client_factory=_FakeZKClient)
    with session_factory() as session:
        session.add(
            Device(
                device_id="MAIN-GATE",
                label="Main Gate",
                ip="192.168.1.60",
                port=4370,
                comm_key_encrypted="base64:MA==",
                enabled=True,
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="already configured"):
        manager.start_job(
            BruteForceStart(
                candidate_id=None,
                ip="192.168.1.60",
                port=4370,
                range_start=0,
                range_end=10,
            )
        )


def test_bruteforce_requires_enabled_setting(monkeypatch, session_factory):
    monkeypatch.setattr(settings, "bruteforce_enabled", False)
    manager = CommKeyBruteforceManager(session_factory=session_factory, client_factory=_FakeZKClient)

    with pytest.raises(PermissionError):
        manager.start_job(
            BruteForceStart(
                candidate_id=None,
                ip="192.168.1.70",
                port=4370,
                range_start=0,
                range_end=10,
            )
        )


def test_bruteforce_job_pause_resume_cancel_transitions(session_factory):
    manager = CommKeyBruteforceManager(session_factory=session_factory, client_factory=_FakeZKClient)
    manager._ensure_thread = lambda _job_id: None
    with session_factory() as session:
        job = CommKeyBruteforceJob(
            ip="192.168.1.80",
            port=4370,
            mode="SAFE_FAST",
            status="RUNNING",
            range_start=0,
            range_end=10,
            current_key=0,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    manager.pause(job_id)
    with session_factory() as session:
        assert session.get(CommKeyBruteforceJob, job_id).status == "PAUSED"

    manager.resume(job_id)
    with session_factory() as session:
        assert session.get(CommKeyBruteforceJob, job_id).status == "RUNNING"

    manager.cancel(job_id)
    with session_factory() as session:
        job = session.get(CommKeyBruteforceJob, job_id)
        assert job.status == "CANCELED"
        assert job.ended_at is not None


def _wait_for_status(session_factory, job_id: int, expected: str) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        with session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job and job.status == expected:
                return
            if job and job.status in {"FAILED", "CANCELED"}:
                raise AssertionError(f"Job finished as {job.status}: {job.last_error}")
        time.sleep(0.05)
    raise AssertionError(f"Job did not reach {expected}")
