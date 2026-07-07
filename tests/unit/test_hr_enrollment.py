from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from zk_hr_enrollment import __main__ as hr_main
from zk_hr_enrollment.config import DEFAULT_COMM_KEY, CommKeyConfigError, read_comm_key
from zk_hr_enrollment.identity import (
    build_machine_name,
    next_numeric_user_id,
    normalize_cnic,
    users_matching_cnic,
)
from zk_hr_enrollment.service import EmployeeRecord, HREnrollmentService
from zk_hr_enrollment.zkt import (
    EnrollmentUser,
    FingerTemplate,
    RuntimeDependencyError,
    ScannedDevice,
    ZKCommunicationError,
    ZKDeviceSession,
    probe_device,
    scan_zkt_devices,
)
from zk_zone_agent.network_scanner import ScanCandidate


DEVICE = ScannedDevice(
    ip="192.168.110.137",
    port=4370,
    serial="ADZV211860253",
    platform="ZLM60_TFT",
    device_name="MB20/ID",
    force_udp=False,
)


def test_comm_key_defaults_to_1979_and_rejects_invalid_file(tmp_path):
    assert read_comm_key(tmp_path / "missing.txt") == DEFAULT_COMM_KEY

    invalid = tmp_path / "comm_key.txt"
    invalid.write_text("not-a-number", encoding="utf-8")

    with pytest.raises(CommKeyConfigError, match="hidden comm-key file is invalid"):
        read_comm_key(invalid)


def test_machine_name_uses_alias_shift_marker_and_zkt_byte_limit():
    assert build_machine_name("Muhammad Asad Janjua", "61101-1200998-9", shift_worker=False) == (
        "MAsad-6110112009989"
    )
    assert build_machine_name("Muhammad Asad Janjua", "6110112009989", shift_worker=True) == (
        "MAsad-S-6110112009989"
    )

    truncated = build_machine_name("Muhammad Abdullah Khan", "3520212345671", shift_worker=True)

    assert len(truncated.encode("utf-8")) <= 24
    assert truncated.endswith("-S-3520212345671")
    assert " " not in truncated


def test_cnic_matching_and_next_hidden_numeric_id():
    users = [
        EnrollmentUser(uid="1", user_id="1", name="AOne-6110112009989", privilege="0"),
        EnrollmentUser(uid="8", user_id="20", name="ATwo-S-6110112009989", privilege="0"),
    ]

    assert normalize_cnic("61101-1200998-9") == "6110112009989"
    assert len(users_matching_cnic(users, "6110112009989")) == 2
    assert next_numeric_user_id(users) == 21


def test_zkt_create_user_always_writes_regular_privilege():
    class RawUser:
        uid = 7
        user_id = "7"
        name = "MAsad-6110112009989"
        privilege = 0
        password = ""
        group_id = ""
        card = 0

    class FakeConnection:
        def __init__(self):
            self.calls = []

        def set_user(self, **kwargs):
            self.calls.append(kwargs)

        def get_users(self):
            return [RawUser()]

    conn = FakeConnection()
    session = ZKDeviceSession(ip="192.168.110.137", port=4370, comm_key=1979)
    session.conn = conn

    created = session.create_user(uid=7, user_id="7", name="MAsad-6110112009989")

    assert created.uid == "7"
    assert conn.calls[0]["privilege"] == 0
    assert conn.calls[0]["password"] == ""
    assert conn.calls[0]["group_id"] == ""
    assert conn.calls[0]["card"] == 0


def test_zkt_session_skips_malformed_templates():
    session = ZKDeviceSession(ip="192.168.110.137", port=4370, comm_key=1979)
    session.conn = SimpleNamespace(
        get_templates=lambda: [
            SimpleNamespace(uid="bad", fid=4, valid=1, size=1196),
            SimpleNamespace(uid=7, fid=4, valid=1, size=1196),
        ]
    )

    assert session.get_templates() == [FingerTemplate(uid=7, fid=4, valid=1, size=1196)]


def test_service_creates_employee_with_auto_id_and_detects_duplicates():
    fake = FakeSession(
        users=[
            EnrollmentUser(uid="1", user_id="1", name="Existing-4220112345671", privilege="0"),
        ],
        templates=[],
    )
    service = HREnrollmentService(comm_key_provider=lambda: 1979, session_factory=lambda *_, **__: fake)

    record = service.create_employee(
        DEVICE,
        full_name="Muhammad Asad Janjua",
        cnic="6110112009989",
        shift_worker=True,
    )

    assert record.uid == "2"
    assert record.user_id == "2"
    assert record.machine_name == "MAsad-S-6110112009989"

    search = service.search_employee(DEVICE, "6110112009989")

    assert search.found is True
    assert search.record.machine_name == "MAsad-S-6110112009989"

    fake.users.append(EnrollmentUser(uid="3", user_id="3", name="Other-6110112009989", privilege="0"))

    duplicate = service.search_employee(DEVICE, "6110112009989")

    assert duplicate.found is False
    assert duplicate.duplicate_count == 2


def test_enrollment_success_uses_post_read_verification_even_when_sdk_returns_false():
    fake = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_result=False,
    )
    service = HREnrollmentService(comm_key_provider=lambda: 1979, session_factory=lambda *_, **__: fake)
    record = EmployeeRecord(
        uid="7",
        user_id="7",
        machine_name="MAsad-6110112009989",
        cnic="6110112009989",
        shift_worker=False,
        privilege="0",
        enrolled_fingers=[],
    )

    outcome = service.enroll_finger(DEVICE, record=record, finger_id=4)

    assert outcome.success is True
    assert outcome.sdk_result is False
    assert outcome.before_fingers == []
    assert outcome.after_fingers == [4]


def test_enrollment_timeout_reconnects_and_verifies_saved_template():
    first = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
    )
    second = FakeSession(
        users=first.users,
        templates=[FingerTemplate(uid=7, fid=4, valid=1, size=1196)],
    )
    sessions = iter([first, second])
    seen_timeouts = []

    def session_factory(*_args, timeout: float):
        seen_timeouts.append(timeout)
        return next(sessions)

    service = HREnrollmentService(
        comm_key_provider=lambda: 1979,
        session_factory=session_factory,
        command_timeout=20,
        enrollment_timeout=120,
    )
    record = EmployeeRecord(
        uid="7",
        user_id="7",
        machine_name="MAsad-6110112009989",
        cnic="6110112009989",
        shift_worker=False,
        privilege="0",
        enrolled_fingers=[],
    )

    outcome = service.enroll_finger(DEVICE, record=record, finger_id=4)

    assert outcome.success is True
    assert outcome.after_fingers == [4]
    assert outcome.sdk_result is None
    assert seen_timeouts == [120, 20]
    assert "reconnected and verified" in outcome.message


def test_enrollment_timeout_reports_unverified_interrupted_enrollment():
    fake = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
    )
    service = HREnrollmentService(comm_key_provider=lambda: 1979, session_factory=lambda *_, **__: fake)
    record = EmployeeRecord(
        uid="7",
        user_id="7",
        machine_name="MAsad-6110112009989",
        cnic="6110112009989",
        shift_worker=False,
        privilege="0",
        enrolled_fingers=[],
    )

    with pytest.raises(RuntimeError, match="interrupted by a device communication timeout"):
        service.enroll_finger(DEVICE, record=record, finger_id=4)


def test_probe_device_falls_back_to_udp_after_tcp_failure():
    calls = []

    class FakeOpener:
        def __init__(self, **kwargs):
            self.force_udp = kwargs["force_udp"]
            calls.append(self.force_udp)

        def __enter__(self):
            if not self.force_udp:
                raise RuntimeError("tcp failed")
            return self

        def __exit__(self, *_args):
            return None

        def get_info(self):
            return "SERIAL", "ZLM60_TFT", "MB20/ID"

    device = probe_device(
        ScanCandidate(ip="192.168.110.137", port=4370, open=True),
        comm_key=1979,
        session_opener=FakeOpener,
    )

    assert calls == [False, True]
    assert device.force_udp is True
    assert device.serial == "SERIAL"


def test_scan_zkt_devices_surfaces_missing_runtime_dependency():
    class FakeScanner:
        def scan(self):
            return [ScanCandidate(ip="192.168.110.137", port=4370, open=True)]

    def missing_dependency_opener(**_kwargs):
        raise RuntimeDependencyError("missing bundled dependency")

    with pytest.raises(RuntimeDependencyError, match="missing bundled dependency"):
        scan_zkt_devices(
            comm_key=1979,
            scanner=FakeScanner(),
            session_opener=missing_dependency_opener,
        )


def test_health_check_fails_when_required_dependency_is_missing(monkeypatch):
    imported = []

    def fake_import(module_name):
        imported.append(module_name)
        if module_name == "psutil":
            raise ModuleNotFoundError("No module named 'psutil'", name="psutil")

    monkeypatch.setattr(hr_main.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hr_main.importlib, "import_module", fake_import)
    monkeypatch.setattr(hr_main, "log_exception", lambda *_args, **_kwargs: None)

    assert hr_main._run_health_check() == 1
    assert "psutil" in imported


@dataclass
class FakeSession:
    users: list[EnrollmentUser]
    templates: list[FingerTemplate]
    enroll_result: object = True
    enroll_error: BaseException | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_users(self):
        return self.users

    def get_templates(self):
        return self.templates

    def create_user(self, *, uid: int, user_id: str, name: str):
        user = EnrollmentUser(uid=str(uid), user_id=user_id, name=name, privilege="0")
        self.users.append(user)
        return user

    def enroll_finger(self, *, uid: str | int, user_id: str, finger_id: int):
        if self.enroll_error is not None:
            raise self.enroll_error
        self.templates.append(FingerTemplate(uid=int(uid), fid=int(finger_id), valid=1, size=1196))
        return self.enroll_result
