from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from zk_hr_enrollment.config import read_comm_key
from zk_hr_enrollment.identity import (
    build_machine_name,
    format_finger_list,
    next_numeric_user_id,
    normalize_cnic,
    parse_machine_identity,
    users_matching_cnic,
)
from zk_hr_enrollment.zkt import (
    EnrollmentUser,
    FingerTemplate,
    ScannedDevice,
    ZKDeviceSession,
    open_zkt_session,
    scan_zkt_devices,
)


@dataclass(frozen=True)
class EmployeeRecord:
    uid: str
    user_id: str
    machine_name: str
    cnic: str
    shift_worker: bool
    privilege: str | None
    enrolled_fingers: list[int]

    @property
    def finger_summary(self) -> str:
        return format_finger_list(self.enrolled_fingers)


@dataclass(frozen=True)
class EmployeeSearchResult:
    found: bool
    record: EmployeeRecord | None
    duplicate_count: int
    message: str


@dataclass(frozen=True)
class EnrollmentOutcome:
    success: bool
    record: EmployeeRecord
    sdk_result: object
    before_fingers: list[int]
    after_fingers: list[int]
    message: str


SessionFactory = Callable[[ScannedDevice, int], AbstractContextManager]


class HREnrollmentService:
    def __init__(
        self,
        *,
        comm_key_provider: Callable[[], int] = read_comm_key,
        session_factory: SessionFactory | None = None,
        device_scanner: Callable[[int], list[ScannedDevice]] | None = None,
    ) -> None:
        self.comm_key_provider = comm_key_provider
        self.session_factory = session_factory or _default_session_factory
        self.device_scanner = device_scanner or (lambda comm_key: scan_zkt_devices(comm_key=comm_key))

    def scan_devices(self) -> list[ScannedDevice]:
        return self.device_scanner(self.comm_key_provider())

    def search_employee(self, device: ScannedDevice, cnic: str) -> EmployeeSearchResult:
        cnic = normalize_cnic(cnic)
        with self._session(device) as session:
            users = session.get_users()
            templates = session.get_templates()
        matches = users_matching_cnic(users, cnic)
        if len(matches) > 1:
            return EmployeeSearchResult(
                found=False,
                record=None,
                duplicate_count=len(matches),
                message="This CNIC exists more than once on the selected device. Ask IT to fix it.",
            )
        if not matches:
            return EmployeeSearchResult(
                found=False,
                record=None,
                duplicate_count=0,
                message="Employee was not found on this device. Create the employee before enrollment.",
            )
        record = _record_from_user(matches[0], templates)
        return EmployeeSearchResult(
            found=True,
            record=record,
            duplicate_count=0,
            message=f"Employee found. {record.finger_summary}.",
        )

    def create_employee(
        self,
        device: ScannedDevice,
        *,
        full_name: str,
        cnic: str,
        shift_worker: bool,
    ) -> EmployeeRecord:
        cnic = normalize_cnic(cnic)
        machine_name = build_machine_name(full_name, cnic, shift_worker=shift_worker)
        with self._session(device) as session:
            users = session.get_users()
            if users_matching_cnic(users, cnic):
                raise ValueError("This CNIC already exists on the selected device. Search again.")
            numeric_id = next_numeric_user_id(users)
            created = session.create_user(uid=numeric_id, user_id=str(numeric_id), name=machine_name)
            templates = session.get_templates()
        return _record_from_user(created, templates)

    def enroll_finger(
        self,
        device: ScannedDevice,
        *,
        record: EmployeeRecord,
        finger_id: int,
    ) -> EnrollmentOutcome:
        finger_id = int(finger_id)
        if finger_id < 0 or finger_id > 9:
            raise ValueError("Finger selection is invalid.")
        if str(record.privilege or "0") not in {"", "0", "None"}:
            raise ValueError("This device user is not a regular employee. Ask IT to review it.")
        with self._session(device) as session:
            users = session.get_users()
            user = next((item for item in users if str(item.uid) == str(record.uid)), None)
            if user is None:
                raise ValueError("Employee was not found on the selected device. Search again.")
            before_templates = session.get_templates()
            before = _template_fids(before_templates, record.uid)
            if finger_id in before:
                raise ValueError("Selected finger is already enrolled. Choose an empty finger slot.")
            sdk_result = session.enroll_finger(uid=record.uid, user_id=record.user_id, finger_id=finger_id)
            after_templates = session.get_templates()
            after = _template_fids(after_templates, record.uid)
        success = finger_id in after
        updated = EmployeeRecord(
            uid=record.uid,
            user_id=record.user_id,
            machine_name=record.machine_name,
            cnic=record.cnic,
            shift_worker=record.shift_worker,
            privilege=record.privilege,
            enrolled_fingers=after,
        )
        if not success:
            raise RuntimeError("Enrollment did not complete. The selected finger was not saved.")
        return EnrollmentOutcome(
            success=True,
            record=updated,
            sdk_result=sdk_result,
            before_fingers=before,
            after_fingers=after,
            message="Fingerprint enrollment completed and was verified on the device.",
        )

    def _session(self, device: ScannedDevice) -> AbstractContextManager:
        return self.session_factory(device, self.comm_key_provider())


def _default_session_factory(device: ScannedDevice, comm_key: int) -> ZKDeviceSession:
    return open_zkt_session(
        ip=device.ip,
        port=device.port,
        comm_key=comm_key,
        timeout=10,
        force_udp=device.force_udp,
    )


def _record_from_user(user: EnrollmentUser, templates: list[FingerTemplate]) -> EmployeeRecord:
    identity = parse_machine_identity(user.name)
    return EmployeeRecord(
        uid=str(user.uid),
        user_id=str(user.user_id),
        machine_name=user.name or "",
        cnic=identity.cnic,
        shift_worker=identity.shift_worker,
        privilege=user.privilege,
        enrolled_fingers=_template_fids(templates, user.uid),
    )


def _template_fids(templates: list[FingerTemplate], uid: str | int) -> list[int]:
    uid_text = str(uid)
    return sorted(
        {
            int(template.fid)
            for template in templates
            if str(template.uid) == uid_text and int(template.valid) == 1
        }
    )

