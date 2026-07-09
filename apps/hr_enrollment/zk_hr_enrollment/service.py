from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace

from zk_hr_enrollment.config import read_comm_key
from zk_hr_enrollment.identity import (
    build_machine_name,
    format_finger_list,
    next_numeric_user_id,
    normalize_cnic,
    parse_machine_identity,
    users_matching_cnic,
)
from zk_hr_enrollment.official_sdk import (
    OfficialFaceEnrollmentResult,
    OfficialSdkUnavailable,
    enroll_face_with_official_sdk,
)
from zk_hr_enrollment.zkt import (
    BiometricCounts,
    EnrollmentUser,
    FingerTemplate,
    ScannedDevice,
    is_device_communication_error,
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
    modality: str = "fingerprint"
    face_count_before: int | None = None
    face_count_after: int | None = None


SessionFactory = Callable[..., AbstractContextManager]
FaceEnroller = Callable[..., OfficialFaceEnrollmentResult]
DEFAULT_COMMAND_TIMEOUT = 20
DEFAULT_ENROLLMENT_TIMEOUT = 120
DEFAULT_VERIFICATION_RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0)


class HREnrollmentService:
    def __init__(
        self,
        *,
        comm_key_provider: Callable[[], int] = read_comm_key,
        session_factory: SessionFactory | None = None,
        device_scanner: Callable[[int], list[ScannedDevice]] | None = None,
        face_enroller: FaceEnroller | None = None,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        enrollment_timeout: float = DEFAULT_ENROLLMENT_TIMEOUT,
        verification_retry_delays: tuple[float, ...] = DEFAULT_VERIFICATION_RETRY_DELAYS,
    ) -> None:
        self.comm_key_provider = comm_key_provider
        self.session_factory = session_factory or _default_session_factory
        self.device_scanner = device_scanner or (lambda comm_key: scan_zkt_devices(comm_key=comm_key))
        self.face_enroller = face_enroller or enroll_face_with_official_sdk
        self.command_timeout = command_timeout
        self.enrollment_timeout = enrollment_timeout
        self.verification_retry_delays = tuple(max(0.0, float(delay)) for delay in verification_retry_delays)

    def scan_devices(self) -> list[ScannedDevice]:
        return self.device_scanner(self.comm_key_provider())

    def search_employee(self, device: ScannedDevice, cnic: str) -> EmployeeSearchResult:
        cnic = normalize_cnic(cnic)
        with self._session(device, timeout=self.command_timeout) as session:
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
        with self._session(device, timeout=self.command_timeout) as session:
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

        attempts = _enrollment_attempt_devices(device)
        first_before: list[int] | None = None
        last_error: BaseException | None = None
        for attempt_number, attempt_device in enumerate(attempts, start=1):
            enrollment_error: BaseException | None = None
            sdk_result: object = None
            after: list[int] = []
            before: list[int] = []
            with self._session(attempt_device, timeout=self.enrollment_timeout) as session:
                users = session.get_users()
                user = next((item for item in users if str(item.uid) == str(record.uid)), None)
                if user is None:
                    raise ValueError("Employee was not found on the selected device. Search again.")
                before_templates = session.get_templates()
                before = _template_fids(before_templates, record.uid)
                if first_before is None:
                    first_before = before
                if finger_id in before:
                    raise ValueError("Selected finger is already enrolled. Choose an empty finger slot.")
                try:
                    sdk_result = session.enroll_finger(
                        uid=record.uid,
                        user_id=record.user_id,
                        finger_id=finger_id,
                    )
                    after_templates = session.get_templates()
                    after = _template_fids(after_templates, record.uid)
                except Exception as exc:
                    if not is_device_communication_error(exc):
                        raise
                    enrollment_error = exc
            if enrollment_error is not None:
                try:
                    after = self._verify_fingers_after_interrupted_enrollment(
                        attempt_device,
                        record,
                        enrollment_error,
                    )
                except RuntimeError as exc:
                    last_error = exc
                    if attempt_number < len(attempts):
                        continue
                    return self._enroll_face_after_finger_failure(
                        device,
                        record,
                        before_fingers=first_before or [],
                    )
            if finger_id not in after:
                last_error = enrollment_error or RuntimeError(
                    "Enrollment did not complete. The selected finger was not saved."
                )
                if attempt_number < len(attempts):
                    continue
                return self._enroll_face_after_finger_failure(
                    device,
                    record,
                    before_fingers=first_before or [],
                )

            updated = EmployeeRecord(
                uid=record.uid,
                user_id=record.user_id,
                machine_name=record.machine_name,
                cnic=record.cnic,
                shift_worker=record.shift_worker,
                privilege=record.privilege,
                enrolled_fingers=after,
            )
            message = "Fingerprint enrollment completed and was verified on the device."
            if enrollment_error is not None:
                message = (
                    "Fingerprint enrollment was saved on the device, but the device did not send the "
                    "final confirmation. The app reconnected and verified the saved finger."
                )
            elif attempt_number > 1:
                message = (
                    "Fingerprint enrollment completed after retrying the alternate ZKT protocol, "
                    "and was verified on the device."
                )
            return EnrollmentOutcome(
                success=True,
                record=updated,
                sdk_result=sdk_result,
                before_fingers=first_before or [],
                after_fingers=after,
                message=message,
            )
        raise RuntimeError(
            "Enrollment did not complete after trying both ZKT protocols. Search the employee again "
            "before retrying."
        ) from last_error

    def enroll_face(
        self,
        device: ScannedDevice,
        *,
        record: EmployeeRecord,
    ) -> EnrollmentOutcome:
        if str(record.privilege or "0") not in {"", "0", "None"}:
            raise ValueError("This device user is not a regular employee. Ask IT to review it.")
        first_before_fingers, first_counts = self._read_face_state(device, record)
        first_face_before = first_counts.faces if first_counts is not None else None

        try:
            official_result = self.face_enroller(
                ip=device.ip,
                port=device.port,
                comm_key=self.comm_key_provider(),
                user_id=record.user_id,
                timeout=self.enrollment_timeout,
            )
        except OfficialSdkUnavailable as exc:
            raise RuntimeError(
                "Face enrollment on this uFace device requires the official ZKTeco Windows SDK "
                "(zkemkeeper.dll). Install/register the ZKTeco Standalone SDK on this Windows PC, "
                "or place the full official SDK DLL folder beside StateLifeHREnrollment.exe and run "
                "the app once as Administrator so it can register the COM class. The pyzk face "
                "fallback is disabled because this firmware opens a stuck Remote Enroll Fingerprint "
                "screen for face index 111 instead of the face enrollment workflow."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "Official ZKTeco face enrollment failed before the device saved a face. "
                "Cancel on the device, search the employee again, and retry face enrollment."
            ) from exc
        else:
            after_fingers, after_counts = self._read_face_state(device, record)
            face_after = after_counts.faces if after_counts is not None else None
            if _face_completed(official_result) or _count_increased(first_face_before, face_after):
                return _face_outcome(
                    record,
                    sdk_result=official_result,
                    before_fingers=first_before_fingers,
                    after_fingers=after_fingers,
                    face_before=first_face_before,
                    face_after=face_after,
                    message="Face enrollment completed through the official ZKTeco SDK.",
                )
            raise RuntimeError(
                "Official ZKTeco face enrollment started, but the device did not save a face before "
                "the timeout. Cancel on the device, search the employee again, and retry face enrollment."
            )

    def _enroll_face_after_finger_failure(
        self,
        device: ScannedDevice,
        record: EmployeeRecord,
        *,
        before_fingers: list[int],
    ) -> EnrollmentOutcome:
        try:
            outcome = self.enroll_face(device, record=record)
        except Exception as exc:
            raise RuntimeError(
                "Fingerprint enrollment failed after two attempts, and the face fallback also failed. "
                "Cancel on the device, search the employee again, and retry face enrollment."
            ) from exc
        return replace(
            outcome,
            before_fingers=before_fingers,
            message=(
                "Fingerprint enrollment failed after two attempts, so the app used face enrollment. "
                f"{outcome.message}"
            ),
        )

    def _session(self, device: ScannedDevice, *, timeout: float) -> AbstractContextManager:
        return self.session_factory(device, self.comm_key_provider(), timeout=timeout)

    def _verify_fingers_after_interrupted_enrollment(
        self,
        device: ScannedDevice,
        record: EmployeeRecord,
        enrollment_error: BaseException,
    ) -> list[int]:
        last_error: BaseException | None = None
        for delay in (0.0, *self.verification_retry_delays):
            if delay:
                time.sleep(delay)
            try:
                with self._session(device, timeout=self.command_timeout) as session:
                    reset = getattr(session, "reset_enrollment_state", None)
                    if callable(reset):
                        reset()
                    return _template_fids(session.get_templates(), record.uid)
            except Exception as exc:
                if not is_device_communication_error(exc):
                    raise
                last_error = exc
        raise RuntimeError(
            "The device stopped responding during enrollment and did not respond to follow-up "
            "verification after several retries. Cancel on the device, wait for the normal clock "
            "screen, search the employee again, and retry enrollment."
        ) from (last_error or enrollment_error)

    def _read_face_state(
        self,
        device: ScannedDevice,
        record: EmployeeRecord,
    ) -> tuple[list[int], BiometricCounts | None]:
        last_error: BaseException | None = None
        for delay in (0.0, *self.verification_retry_delays):
            if delay:
                time.sleep(delay)
            try:
                with self._session(device, timeout=self.command_timeout) as session:
                    users = session.get_users()
                    user = next((item for item in users if str(item.uid) == str(record.uid)), None)
                    if user is None:
                        raise ValueError("Employee was not found on the selected device. Search again.")
                    templates = session.get_templates()
                    return _template_fids(templates, record.uid), _safe_counts(session)
            except Exception as exc:
                if not is_device_communication_error(exc):
                    raise
                last_error = exc
        raise RuntimeError(
            "The device is still busy from the previous enrollment attempt and did not respond after "
            "several retries. Cancel on the device, wait for the normal clock screen, search the "
            "employee again, and retry enrollment."
        ) from last_error


def _default_session_factory(
    device: ScannedDevice,
    comm_key: int,
    *,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> AbstractContextManager:
    return open_zkt_session(
        ip=device.ip,
        port=device.port,
        comm_key=comm_key,
        timeout=timeout,
        force_udp=device.force_udp,
    )


def _enrollment_attempt_devices(device: ScannedDevice) -> list[ScannedDevice]:
    if device.force_udp is None:
        protocols: list[bool] = [False, True]
    elif device.force_udp is False:
        protocols = [False, True]
    else:
        protocols = [True, False]
    return [replace(device, force_udp=force_udp) for force_udp in protocols]


def _safe_counts(session) -> BiometricCounts | None:
    get_counts = getattr(session, "get_biometric_counts", None)
    if not callable(get_counts):
        return None
    try:
        return get_counts()
    except Exception:
        return None


def _count_increased(before: int | None, after: int | None) -> bool:
    return before is not None and after is not None and after > before


def _face_completed(result: object) -> bool:
    if isinstance(result, OfficialFaceEnrollmentResult):
        return result.completed
    return bool(result)


def _face_outcome(
    record: EmployeeRecord,
    *,
    sdk_result: object,
    before_fingers: list[int],
    after_fingers: list[int],
    face_before: int | None,
    face_after: int | None,
    message: str,
) -> EnrollmentOutcome:
    updated = EmployeeRecord(
        uid=record.uid,
        user_id=record.user_id,
        machine_name=record.machine_name,
        cnic=record.cnic,
        shift_worker=record.shift_worker,
        privilege=record.privilege,
        enrolled_fingers=after_fingers,
    )
    detail = ""
    if face_before is not None and face_after is not None:
        detail = f" Face templates: {face_before} -> {face_after}."
    return EnrollmentOutcome(
        success=True,
        record=updated,
        sdk_result=sdk_result,
        before_fingers=before_fingers,
        after_fingers=after_fingers,
        message=message + detail,
        modality="face",
        face_count_before=face_before,
        face_count_after=face_after,
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
