from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from zk_hr_enrollment import __main__ as hr_main
from zk_hr_enrollment import zkt as zkt_module
from zk_hr_enrollment.app import AUTO_SCAN_INITIAL_DELAY_MS, AUTO_SCAN_INTERVAL_MS
from zk_hr_enrollment.config import DEFAULT_COMM_KEY, read_comm_key
from zk_hr_enrollment.identity import (
    build_machine_name,
    next_numeric_user_id,
    normalize_cnic,
    users_matching_cnic,
)
from zk_hr_enrollment.official_sdk import OfficialFaceEnrollmentResult, OfficialSdkUnavailable
from zk_hr_enrollment.service import EmployeeRecord, HREnrollmentService
from zk_hr_enrollment.zkt import (
    BiometricCounts,
    EnrollmentUser,
    FACE_TEMPLATE_ID,
    FingerTemplate,
    RuntimeDependencyError,
    RemoteEnrollmentResult,
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


def test_comm_key_is_fixed_to_1979_even_with_old_override_file(tmp_path):
    assert DEFAULT_COMM_KEY == 1979
    assert read_comm_key(tmp_path / "missing.txt") == DEFAULT_COMM_KEY

    old_override = tmp_path / "comm_key.txt"
    old_override.write_text("1234", encoding="utf-8")
    assert read_comm_key(old_override) == DEFAULT_COMM_KEY

    old_invalid_override = tmp_path / "invalid-comm-key.txt"
    old_invalid_override.write_text("not-a-number", encoding="utf-8")
    assert read_comm_key(old_invalid_override) == DEFAULT_COMM_KEY


def test_hr_app_auto_scan_defaults_are_enabled():
    assert AUTO_SCAN_INITIAL_DELAY_MS == 1000
    assert AUTO_SCAN_INTERVAL_MS == 30000


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


def test_zkt_enrollment_resets_capture_state_around_sdk_call():
    class FakeConnection:
        def __init__(self):
            self.calls = []

        def free_data(self):
            self.calls.append("free_data")

        def cancel_capture(self):
            self.calls.append("cancel_capture")

        def cancel_enroll(self):
            self.calls.append("cancel_enroll")

        def verify_user(self):
            self.calls.append("verify_user")

        def enable_device(self):
            self.calls.append("enable_device")

        def reg_event(self, flags):
            self.calls.append(("reg_event", flags))

        def refresh_data(self):
            self.calls.append("refresh_data")

        def enroll_user(self, **kwargs):
            self.calls.append(("enroll_user", kwargs))
            return True

    conn = FakeConnection()
    session = ZKDeviceSession(ip="192.168.110.137", port=4370, comm_key=1979)
    session.conn = conn

    assert session.enroll_finger(uid="7", user_id="7", finger_id=4) is True
    assert conn.calls == [
        "free_data",
        "cancel_capture",
        "cancel_enroll",
        "verify_user",
        "enable_device",
        ("enroll_user", {"uid": 7, "temp_id": 4, "user_id": "7"}),
        ("reg_event", 0),
        "cancel_capture",
        "cancel_enroll",
        "verify_user",
        "free_data",
        "refresh_data",
        "enable_device",
    ]


def test_zkt_fingerprint_enrollment_reads_three_tap_event_sequence():
    class FakeSocket:
        def __init__(self):
            self.timeouts = []
            self.packets = [
                _event_packet(0),
                _event_packet(0x64),
                _event_packet(0),
                _event_packet(0x64),
                _event_packet(0),
                _event_packet(0x64),
                _event_packet(0),
            ]

        def gettimeout(self):
            return 30

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def recv(self, _size):
            if not self.packets:
                raise socket.timeout()
            return self.packets.pop(0)

    class FakeConnection:
        tcp = True

        def __init__(self):
            self.calls = []
            self.sock = FakeSocket()
            setattr(self, "_ZK__sock", self.sock)
            setattr(self, "_ZK__send_command", self._send_command)
            setattr(self, "_ZK__ack_ok", self._ack_ok)

        def _send_command(self, command, command_string=b""):
            self.calls.append(("send", command, command_string))
            return {"status": True}

        def _ack_ok(self):
            self.calls.append("ack")

        def free_data(self):
            self.calls.append("free_data")

        def cancel_capture(self):
            self.calls.append("cancel_capture")

        def cancel_enroll(self):
            self.calls.append("cancel_enroll")

        def verify_user(self):
            self.calls.append("verify_user")

        def enable_device(self):
            self.calls.append("enable_device")

        def reg_event(self, flags):
            self.calls.append(("reg_event", flags))

        def refresh_data(self):
            self.calls.append("refresh_data")

    conn = FakeConnection()
    session = ZKDeviceSession(ip="192.168.110.137", port=4370, comm_key=1979, timeout=120)
    session.conn = conn

    result = session.enroll_finger(uid="7", user_id="7", finger_id=4)

    assert isinstance(result, RemoteEnrollmentResult)
    assert result.completed is True
    assert result.event_codes == (0, 0x64, 0, 0x64, 0, 0x64, 0)
    assert conn.calls.count("ack") == 7
    assert conn.sock.timeouts[-1] == 30


def test_zkt_face_enrollment_uses_face_template_id():
    class FakeConnection:
        def __init__(self):
            self.calls = []

        def free_data(self):
            self.calls.append("free_data")

        def cancel_capture(self):
            self.calls.append("cancel_capture")

        def cancel_enroll(self):
            self.calls.append("cancel_enroll")

        def verify_user(self):
            self.calls.append("verify_user")

        def enable_device(self):
            self.calls.append("enable_device")

        def reg_event(self, flags):
            self.calls.append(("reg_event", flags))

        def refresh_data(self):
            self.calls.append("refresh_data")

        def enroll_user(self, **kwargs):
            self.calls.append(("enroll_user", kwargs))
            return True

    conn = FakeConnection()
    session = ZKDeviceSession(ip="192.168.110.137", port=4370, comm_key=1979)
    session.conn = conn

    assert session.enroll_face(uid="7", user_id="7") is True
    assert ("enroll_user", {"uid": 7, "temp_id": FACE_TEMPLATE_ID, "user_id": "7"}) in conn.calls


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


def test_enrollment_timeout_retries_verification_until_device_is_idle():
    first = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
    )
    second = FakeSession(
        users=first.users,
        templates=[FingerTemplate(uid=7, fid=4, valid=1, size=1196)],
    )
    sessions = iter([first, BusySession(ZKCommunicationError("still loading")), second])
    seen_timeouts = []

    def session_factory(*_args, timeout: float):
        seen_timeouts.append(timeout)
        return next(sessions)

    service = HREnrollmentService(
        comm_key_provider=lambda: 1979,
        session_factory=session_factory,
        command_timeout=20,
        enrollment_timeout=120,
        verification_retry_delays=(0,),
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
    assert seen_timeouts == [120, 20, 20]


def test_enrollment_timeout_retries_alternate_protocol_when_template_not_saved():
    first_attempt = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
    )
    verify_after_first = FakeSession(users=first_attempt.users, templates=[])
    second_attempt = FakeSession(users=first_attempt.users, templates=[])
    sessions = iter([first_attempt, verify_after_first, second_attempt])
    seen_protocols = []
    seen_timeouts = []

    def session_factory(device, _comm_key, *, timeout: float):
        seen_protocols.append(device.force_udp)
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
    assert seen_protocols == [False, False, True]
    assert seen_timeouts == [120, 20, 120]
    assert "alternate ZKT protocol" in outcome.message


def test_enrollment_timeout_falls_back_to_face_after_two_failed_finger_attempts():
    first_attempt = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
    )
    verify_after_first = FakeSession(users=first_attempt.users, templates=[], faces=9)
    second_attempt = FakeSession(
        users=first_attempt.users,
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
    )
    verify_after_second = FakeSession(users=first_attempt.users, templates=[], faces=9)
    face_precheck = FakeSession(users=first_attempt.users, templates=[], faces=9)
    face_after = FakeSession(users=first_attempt.users, templates=[], faces=10)
    sessions = iter(
        [
            first_attempt,
            verify_after_first,
            second_attempt,
            verify_after_second,
            face_precheck,
            face_after,
        ]
    )
    seen_protocols = []
    seen_timeouts = []
    official_calls = []

    def session_factory(device, _comm_key, *, timeout: float):
        seen_protocols.append(device.force_udp)
        seen_timeouts.append(timeout)
        return next(sessions)

    service = HREnrollmentService(
        comm_key_provider=lambda: 1979,
        session_factory=session_factory,
        face_enroller=lambda **kwargs: official_calls.append(kwargs)
        or OfficialFaceEnrollmentResult(started=True, completed=True),
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
    assert outcome.modality == "face"
    assert outcome.face_count_before == 9
    assert outcome.face_count_after == 10
    assert seen_protocols == [False, False, True, True, False, False]
    assert seen_timeouts == [120, 20, 120, 20, 20, 20]
    assert official_calls[0]["user_id"] == "7"
    assert "app used face enrollment" in outcome.message


def test_direct_face_enrollment_reports_official_sdk_face_count_change():
    users = [EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")]
    sessions = iter(
        [
            FakeSession(users=users, templates=[], faces=4),
            FakeSession(users=users, templates=[], faces=5),
        ]
    )
    service = HREnrollmentService(
        comm_key_provider=lambda: 1979,
        session_factory=lambda *_, **__: next(sessions),
        face_enroller=lambda **_: OfficialFaceEnrollmentResult(started=True, completed=False),
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

    outcome = service.enroll_face(DEVICE, record=record)

    assert outcome.success is True
    assert outcome.modality == "face"
    assert outcome.face_count_before == 4
    assert outcome.face_count_after == 5


def test_direct_face_enrollment_requires_official_sdk_and_skips_pyzk_face():
    fake = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        faces=4,
    )
    service = HREnrollmentService(
        comm_key_provider=lambda: 1979,
        session_factory=lambda *_, **__: fake,
        face_enroller=lambda **_: (_ for _ in ()).throw(OfficialSdkUnavailable("not installed")),
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

    with pytest.raises(RuntimeError, match="requires the official ZKTeco Windows SDK"):
        service.enroll_face(DEVICE, record=record)

    assert fake.face_calls == 0
    assert fake.faces == 4


def test_direct_face_enrollment_prefers_official_sdk_when_available():
    users = [EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")]
    sessions = iter(
        [
            FakeSession(users=users, templates=[], faces=4),
            FakeSession(users=users, templates=[], faces=5),
        ]
    )
    official_calls = []

    def session_factory(*_args, **_kwargs):
        return next(sessions)

    def face_enroller(**kwargs):
        official_calls.append(kwargs)
        return OfficialFaceEnrollmentResult(started=True, completed=True)

    service = HREnrollmentService(
        comm_key_provider=lambda: 1979,
        session_factory=session_factory,
        face_enroller=face_enroller,
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

    outcome = service.enroll_face(DEVICE, record=record)

    assert outcome.success is True
    assert outcome.modality == "face"
    assert outcome.face_count_before == 4
    assert outcome.face_count_after == 5
    assert official_calls[0]["user_id"] == "7"
    assert "official ZKTeco SDK" in outcome.message


def test_enrollment_timeout_reports_face_fallback_failure():
    fake = FakeSession(
        users=[EnrollmentUser(uid="7", user_id="7", name="MAsad-6110112009989", privilege="0")],
        templates=[],
        enroll_error=ZKCommunicationError("timed out"),
        face_error=ZKCommunicationError("face timed out"),
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

    with pytest.raises(RuntimeError, match="face fallback also failed"):
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


def test_scan_zkt_devices_keeps_open_candidate_when_validation_fails():
    class FakeScanner:
        def scan(self):
            return [ScanCandidate(ip="192.168.110.137", port=4370, open=True)]

    def missing_dependency_opener(**_kwargs):
        raise RuntimeDependencyError("missing bundled dependency")

    devices = scan_zkt_devices(
        comm_key=1979,
        scanner=FakeScanner(),
        session_opener=missing_dependency_opener,
    )

    assert len(devices) == 1
    assert devices[0].ip == "192.168.110.137"
    assert devices[0].validated is False
    assert devices[0].force_udp is None
    assert "open ZKT port" in devices[0].label


def test_auto_protocol_session_tries_udp_after_tcp_failure(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.force_udp = kwargs["force_udp"]
            calls.append(self.force_udp)

        def __enter__(self):
            if not self.force_udp:
                raise RuntimeError("tcp failed")
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(zkt_module, "ZKDeviceSession", FakeSession)

    with zkt_module.open_zkt_session(
        ip="192.168.110.137",
        port=4370,
        comm_key=1979,
        force_udp=None,
    ) as session:
        assert session.force_udp is True

    assert calls == [False, True]


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
    face_result: object = True
    face_error: BaseException | None = None
    faces: int = 0
    face_calls: int = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_users(self):
        return self.users

    def get_templates(self):
        return self.templates

    def get_biometric_counts(self):
        return BiometricCounts(
            users=len(self.users),
            fingers=len(self.templates),
            faces=self.faces,
            users_capacity=10000,
            fingers_capacity=2000,
            faces_capacity=3000,
        )

    def create_user(self, *, uid: int, user_id: str, name: str):
        user = EnrollmentUser(uid=str(uid), user_id=user_id, name=name, privilege="0")
        self.users.append(user)
        return user

    def enroll_finger(self, *, uid: str | int, user_id: str, finger_id: int):
        if self.enroll_error is not None:
            raise self.enroll_error
        self.templates.append(FingerTemplate(uid=int(uid), fid=int(finger_id), valid=1, size=1196))
        return self.enroll_result

    def enroll_face(self, *, uid: str | int, user_id: str):
        self.face_calls += 1
        if self.face_error is not None:
            raise self.face_error
        self.faces += 1
        return self.face_result


@dataclass
class BusySession:
    error: BaseException

    def __enter__(self):
        raise self.error

    def __exit__(self, *_args):
        return None


def _event_packet(code: int, offset: int = 16) -> bytes:
    data = bytearray(24)
    struct.pack_into("H", data, offset, code)
    return bytes(data)
