from __future__ import annotations

from dataclasses import dataclass, field
from queue import Empty, Queue
import threading
import time
from datetime import datetime
from typing import Callable, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.enums import (
    ClockStatus,
    IncidentSeverity,
    OutageType,
    PayloadType,
    SourceType,
    SyncStatus,
)
from zk_common.schemas import ClockCheckSyncItem, IncidentSyncItem, OutageSyncItem
from zk_common.time_utils import utc_now
from zk_zone_agent.attendance import AttendanceContext, attendance_processor
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.config import ActiveZoneConfig, config_manager
from zk_zone_agent.db import (
    BulkUserUpdateItem,
    BulkUserUpdateJob,
    ClockCheck,
    Device,
    FraudIncident,
    OutagePeriod,
)
from zk_zone_agent.device_registry import device_registry
from zk_zone_agent.device_users import DeviceUserUpdate
from zk_zone_agent.fraud import fraud_engine
from zk_zone_agent.settings import settings
from zk_zone_agent.sync import sync_queue_writer
from zk_zone_agent.trusted_time import TrustedTimeService
from zk_zone_agent.zk_client import PyZKClient, ZKAttendance, ZKClient, ZKUser

T = TypeVar("T")


@dataclass
class _DeviceCommand:
    kind: str
    payload: object | None
    done: threading.Event
    result: object | None = None
    error: BaseException | None = None
    started: bool = False
    canceled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class DeviceWorker(threading.Thread):
    def __init__(
        self,
        *,
        device_id: str,
        zone_config: ActiveZoneConfig,
        stop_event: threading.Event,
        trusted_time: TrustedTimeService,
        session_factory: Callable[[], Session],
        client_factory: Callable[[Device], ZKClient] | None = None,
        clock_interval_seconds: int = 5,
        live_poll_reconcile_interval_seconds: int | None = None,
    ) -> None:
        super().__init__(name=f"device-worker-{device_id}", daemon=True)
        self.device_id = device_id
        self.zone_config = zone_config
        self.stop_event = stop_event
        self.trusted_time = trusted_time
        self.session_factory = session_factory
        self.client_factory = client_factory or self._default_client_factory
        self.clock_interval_seconds = clock_interval_seconds
        self.live_poll_reconcile_interval_seconds = (
            settings.live_poll_reconcile_interval_seconds
            if live_poll_reconcile_interval_seconds is None
            else live_poll_reconcile_interval_seconds
        )
        self.last_live_poll_reconcile_monotonic: float | None = None
        self.previous_device_time: datetime | None = None
        self.previous_trusted_time: datetime | None = None
        self.last_clock_status: ClockStatus = ClockStatus.ERROR
        self.reconnect_clock_ok = True
        self.command_queue: Queue[_DeviceCommand] = Queue()
        self.command_available = threading.Event()
        self._active_client: ZKClient | None = None
        self._active_client_lock = threading.Lock()

    def run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            session = self.session_factory()
            device = session.scalar(select(Device).where(Device.device_id == self.device_id))
            session.close()
            if device is None or not device.enabled:
                return
            client = self.client_factory(device)
            try:
                self._mark_connecting(backoff)
                client.connect()
                backoff = 5
                self._set_active_client(client)
                self._on_connected(client)
                self.command_available.set()
                self._live_loop(client)
            except Exception as exc:
                self._mark_offline(f"{exc}. Retrying in {backoff} seconds.")
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self.command_available.clear()
                self._clear_active_client(client)
                self._fail_pending_commands(
                    "Device disconnected before the user command could run."
                )
                try:
                    client.disconnect()
                except Exception:
                    pass

    def _default_client_factory(self, device: Device) -> ZKClient:
        return PyZKClient(
            ip=device.ip,
            port=device.port,
            comm_key=device_registry.comm_key(device),
            timeout=settings.zkt_client_timeout_seconds,
        )

    def _set_active_client(self, client: ZKClient) -> None:
        with self._active_client_lock:
            self._active_client = client

    def _clear_active_client(self, client: ZKClient) -> None:
        with self._active_client_lock:
            if self._active_client is client:
                self._active_client = None

    def _wake_live_capture(self) -> None:
        with self._active_client_lock:
            client = self._active_client
        if client is None:
            return
        self._stop_live_capture_quietly(client)

    def _stop_live_capture_quietly(self, client: ZKClient) -> None:
        try:
            client.stop_live_capture()
        except Exception:
            pass

    def _session_device(self, session: Session) -> Device:
        device = session.scalar(select(Device).where(Device.device_id == self.device_id))
        if device is None:
            raise RuntimeError(f"Device {self.device_id} disappeared.")
        return device

    def _zone_config(self, session: Session) -> ActiveZoneConfig:
        return config_manager.runtime_config(session)

    def _on_connected(self, client: ZKClient) -> None:
        with self.session_factory() as session:
            device = self._session_device(session)
            info = client.get_info()
            device.serial = info.serial or device.serial
            device.platform = info.platform or device.platform
            device.device_name = info.device_name or device.device_name
            device.online = True
            device.last_error = "Connected. Loading users and running initial clock check."
            users = client.get_users()
            attendance_processor.replace_users(session, device, users)
            session.commit()

        self._close_outage("Device reconnected.")
        self._clock_check(client)
        self._reconcile_dump(client, SourceType.DUMP_STARTUP)

    def _live_loop(self, client: ZKClient) -> None:
        self._drain_commands(client)
        for attendance in client.live_capture(new_timeout=self.clock_interval_seconds):
            if self.stop_event.is_set():
                break
            self._drain_commands(client)
            if attendance is None:
                self._clock_check(client)
                self._live_poll_reconcile_if_due(client)
                self._drain_commands(client)
                continue
            self._process_observed_attendance(attendance, SourceType.LIVE)
            self._drain_commands(client)

    def refresh_users(self, timeout_seconds: float | None = None) -> list[ZKUser]:
        timeout_seconds = timeout_seconds or settings.device_user_refresh_timeout_seconds
        result = self._submit_command("refresh_users", None, timeout_seconds)
        return list(result) if isinstance(result, list) else []

    def update_user(
        self,
        update: DeviceUserUpdate,
        timeout_seconds: float | None = None,
    ) -> ZKUser:
        timeout_seconds = timeout_seconds or settings.device_user_update_timeout_seconds
        result = self._submit_command("update_user", update, timeout_seconds)
        if not isinstance(result, ZKUser):
            raise RuntimeError("Device update returned an unexpected response.")
        return result

    def run_bulk_user_update_job(self, job_id: int, timeout_seconds: float | None = None) -> None:
        timeout_seconds = timeout_seconds or settings.bulk_user_update_timeout_seconds
        self._submit_command("bulk_user_update_job", job_id, timeout_seconds)

    def _submit_command(
        self,
        kind: str,
        payload: object | None,
        timeout_seconds: float,
    ) -> object | None:
        if not self.is_alive() or not self.command_available.is_set():
            raise RuntimeError(
                "Device is not online; user changes can be retried after it reconnects."
            )
        command = _DeviceCommand(kind=kind, payload=payload, done=threading.Event())
        self.command_queue.put(command)
        self._wake_live_capture()
        if not command.done.wait(timeout_seconds):
            with command.lock:
                if not command.started:
                    command.canceled = True
                    command.error = TimeoutError(
                        "Device command expired before it started; the queued operation was canceled."
                    )
                else:
                    command.error = TimeoutError(
                        "Device command started but did not finish before the timeout."
                    )
            self._wake_live_capture()
            raise TimeoutError(
                f"Device did not finish the {kind.replace('_', ' ')} command within "
                f"{timeout_seconds:g} seconds. The app interrupted live capture so the "
                "command could run; check the device network/power and refresh before retrying."
            )
        if command.error is not None:
            raise command.error
        return command.result

    def _drain_commands(self, client: ZKClient) -> None:
        should_restart_live_capture = False
        while True:
            try:
                command = self.command_queue.get_nowait()
            except Empty:
                break
            try:
                with command.lock:
                    if command.canceled:
                        if command.error is None:
                            command.error = TimeoutError("Timed-out command was canceled before execution.")
                        continue
                    command.started = True
                self._stop_live_capture_quietly(client)
                if command.kind == "refresh_users":
                    command.result = self._refresh_device_users(client)
                    should_restart_live_capture = True
                elif command.kind == "update_user":
                    if not isinstance(command.payload, DeviceUserUpdate):
                        raise RuntimeError("Invalid user update command.")
                    command.result = self._update_device_user(client, command.payload)
                    should_restart_live_capture = True
                elif command.kind == "bulk_user_update_job":
                    if not isinstance(command.payload, int):
                        raise RuntimeError("Invalid bulk user update command.")
                    self._run_bulk_user_update_job(client, command.payload)
                    command.result = True
                    should_restart_live_capture = True
                else:
                    raise RuntimeError(f"Unknown device command {command.kind}.")
            except BaseException as exc:
                command.error = exc
            finally:
                command.done.set()
                self.command_queue.task_done()
        if should_restart_live_capture:
            self._stop_live_capture_quietly(client)

    def _fail_pending_commands(self, message: str) -> None:
        while True:
            try:
                command = self.command_queue.get_nowait()
            except Empty:
                break
            command.error = RuntimeError(message)
            command.done.set()
            self.command_queue.task_done()

    def _refresh_device_users(self, client: ZKClient) -> list[ZKUser]:
        users = self._device_io(client, "read users from device", client.get_users)
        with self.session_factory() as session:
            device = self._session_device(session)
            attendance_processor.replace_users(session, device, users)
            device.last_error = f"Loaded {len(users)} user(s) from device."
            session.commit()
        return users

    def _update_device_user(self, client: ZKClient, update: DeviceUserUpdate) -> ZKUser:
        users = self._device_io(client, "read users before update", client.get_users)
        existing = next((user for user in users if str(user.uid) == update.uid), None)
        if existing is None:
            raise ValueError("User was not found on the device. Refresh users and try again.")
        duplicate = next(
            (
                user
                for user in users
                if str(user.user_id) == update.user_id and str(user.uid) != update.uid
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError(f"User ID {update.user_id} already exists on this device.")
        updated = self._device_io(
            client,
            f"write user {update.user_id}",
            lambda: client.update_user(
                uid=update.uid,
                user_id=update.user_id,
                name=update.name,
                privilege=update.privilege,
                card=update.card,
            ),
        )
        refreshed_users = self._device_io(client, "verify users after update", client.get_users)
        with self.session_factory() as session:
            device = self._session_device(session)
            attendance_processor.replace_users(session, device, refreshed_users)
            device.last_error = f"Updated user {updated.name or updated.user_id} on device."
            session.commit()
        return updated

    def _device_io(self, client: ZKClient, action: str, operation: Callable[[], T]) -> T:
        attempts = max(1, settings.device_user_io_retry_attempts)
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            self._stop_live_capture_quietly(client)
            try:
                return operation()
            except BaseException as exc:
                last_error = exc
                if attempt >= attempts or not self._is_retryable_device_error(exc):
                    raise
                self._mark_device_command_retry(action, attempt, attempts, exc)
                time.sleep(max(0, settings.device_user_io_retry_delay_seconds))
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{action} did not return a result.")

    def _is_retryable_device_error(self, exc: BaseException) -> bool:
        if isinstance(exc, (TimeoutError, OSError, ConnectionError)):
            return True
        message = str(exc).lower()
        retryable_fragments = (
            "timed out",
            "timeout",
            "can't read sizes",
            "cannot read sizes",
            "read sizes",
            "connection",
            "socket",
            "temporarily",
            "broken pipe",
            "reset by peer",
        )
        return any(fragment in message for fragment in retryable_fragments)

    def _mark_device_command_retry(
        self,
        action: str,
        attempt: int,
        attempts: int,
        exc: BaseException,
    ) -> None:
        try:
            with self.session_factory() as session:
                device = self._session_device(session)
                device.last_error = (
                    f"{action} failed on attempt {attempt}/{attempts}: {exc}. Retrying."
                )
                session.commit()
        except Exception:
            pass

    def _run_bulk_user_update_job(self, client: ZKClient, job_id: int) -> None:
        self._mark_bulk_job_started(job_id)
        try:
            self._refresh_device_users(client)
            while not self.stop_event.is_set():
                item = self._next_bulk_item(job_id)
                if item is None:
                    break
                self._process_bulk_item(client, item.id)
            if self.stop_event.is_set():
                self._interrupt_bulk_job(job_id, "Zone agent stopped while the bulk update was running.")
                return
            self._finish_bulk_job(job_id)
        except BaseException as exc:
            self._interrupt_bulk_job(job_id, str(exc))
            raise

    def _mark_bulk_job_started(self, job_id: int) -> None:
        with self.session_factory() as session:
            job = session.get(BulkUserUpdateJob, job_id)
            if job is None:
                raise RuntimeError("Bulk update job was not found.")
            if job.device_id != self.device_id:
                raise RuntimeError("Bulk update job belongs to a different device.")
            job.status = "RUNNING"
            job.started_at = job.started_at or utc_now()
            job.last_error = None
            job.updated_at = utc_now()
            for item in session.scalars(
                select(BulkUserUpdateItem).where(
                    BulkUserUpdateItem.job_id == job_id,
                    BulkUserUpdateItem.status == "UPDATING",
                )
            ):
                item.status = "PENDING"
                item.message = "Retrying after an interrupted attempt."
                item.updated_at = utc_now()
            self._refresh_bulk_job_counts(session, job)
            session.commit()

    def _next_bulk_item(self, job_id: int) -> BulkUserUpdateItem | None:
        with self.session_factory() as session:
            job = session.get(BulkUserUpdateJob, job_id)
            if job is None or job.status == "CANCELED":
                return None
            item = session.scalar(
                select(BulkUserUpdateItem)
                .where(
                    BulkUserUpdateItem.job_id == job_id,
                    BulkUserUpdateItem.status == "PENDING",
                )
                .order_by(BulkUserUpdateItem.id.asc())
                .limit(1)
            )
            if item is None:
                return None
            session.expunge(item)
            return item

    def _process_bulk_item(self, client: ZKClient, item_id: int) -> None:
        with self.session_factory() as session:
            item = session.get(BulkUserUpdateItem, item_id)
            if item is None or item.status != "PENDING":
                return
            item.status = "UPDATING"
            item.started_at = utc_now()
            item.attempt_count += 1
            item.message = "Updating user on device."
            item.updated_at = utc_now()
            job = session.get(BulkUserUpdateJob, item.job_id)
            if job:
                self._refresh_bulk_job_counts(session, job)
            session.commit()

        try:
            users = self._device_io(client, "read users before bulk row update", client.get_users)
            with self.session_factory() as session:
                item = session.get(BulkUserUpdateItem, item_id)
                if item is None:
                    return
                existing = next((user for user in users if str(user.user_id) == item.user_id), None)
                if existing is None:
                    self._fail_bulk_item(session, item, "ID was not found on the device during update.")
                    return
                if item.expected_name and (existing.name or "") == item.expected_name:
                    self._verify_bulk_item(session, item, "Already matched expected machine data.")
                    return
                privilege = _bulk_privilege(existing.privilege or item.privilege)
                card = existing.card if existing.card is not None else (item.card or 0)
                uid = existing.uid

            if not item.expected_name:
                raise RuntimeError("Bulk item is missing an expected name.")
            updated = self._device_io(
                client,
                f"write bulk user {item.user_id}",
                lambda: client.update_user(
                    uid=uid,
                    user_id=item.user_id,
                    name=item.expected_name,
                    privilege=privilege,
                    card=card,
                ),
            )
            refreshed_users = self._device_io(
                client,
                "verify users after bulk row update",
                client.get_users,
            )
            verified = next((user for user in refreshed_users if str(user.user_id) == item.user_id), None)
            with self.session_factory() as session:
                item = session.get(BulkUserUpdateItem, item_id)
                if item is None:
                    return
                if verified is None:
                    self._fail_bulk_item(session, item, "Device reload did not contain this ID after update.")
                    return
                if str(verified.user_id) != item.user_id:
                    self._fail_bulk_item(session, item, "Device ID changed during update; verification failed.")
                    return
                if (verified.name or "") != item.expected_name:
                    self._fail_bulk_item(
                        session,
                        item,
                        f"Verification failed: device has {verified.name or ''!r}.",
                    )
                    return
                device = self._session_device(session)
                attendance_processor.replace_users(session, device, refreshed_users)
                self._verify_bulk_item(
                    session,
                    item,
                    f"Verified {updated.name or updated.user_id} on device.",
                )
        except BaseException as exc:
            with self.session_factory() as session:
                item = session.get(BulkUserUpdateItem, item_id)
                if item is not None:
                    item.status = "PENDING"
                    item.message = f"Interrupted during update: {exc}"
                    item.updated_at = utc_now()
                    job = session.get(BulkUserUpdateJob, item.job_id)
                    if job:
                        self._refresh_bulk_job_counts(session, job)
            raise

    def _verify_bulk_item(self, session: Session, item: BulkUserUpdateItem, message: str) -> None:
        item.status = "VERIFIED"
        item.message = message
        item.ended_at = utc_now()
        item.updated_at = utc_now()
        job = session.get(BulkUserUpdateJob, item.job_id)
        if job:
            self._refresh_bulk_job_counts(session, job)
        audit_ledger.append(
            session,
            "bulk_user_update_item",
            item.id,
            {
                "job_id": item.job_id,
                "device_id": item.device_id,
                "user_id": item.user_id,
                "expected_name": item.expected_name,
                "cnic": item.cnic,
                "status": item.status,
                "message": message,
                "updated_at": utc_now(),
            },
        )

    def _fail_bulk_item(self, session: Session, item: BulkUserUpdateItem, message: str) -> None:
        item.status = "FAILED"
        item.message = message
        item.ended_at = utc_now()
        item.updated_at = utc_now()
        job = session.get(BulkUserUpdateJob, item.job_id)
        if job:
            self._refresh_bulk_job_counts(session, job)
        audit_ledger.append(
            session,
            "bulk_user_update_item",
            item.id,
            {
                "job_id": item.job_id,
                "device_id": item.device_id,
                "user_id": item.user_id,
                "status": item.status,
                "message": message,
                "updated_at": utc_now(),
            },
        )

    def _finish_bulk_job(self, job_id: int) -> None:
        with self.session_factory() as session:
            job = session.get(BulkUserUpdateJob, job_id)
            if job is None:
                return
            if job.status == "CANCELED":
                return
            self._refresh_bulk_job_counts(session, job)
            job.status = "COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED"
            job.ended_at = utc_now()
            job.updated_at = utc_now()
            audit_ledger.append(
                session,
                "bulk_user_update_job",
                job.id,
                {
                    "device_id": job.device_id,
                    "status": job.status,
                    "verified_count": job.verified_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "ended_at": utc_now(),
                },
            )

    def _interrupt_bulk_job(self, job_id: int, message: str) -> None:
        with self.session_factory() as session:
            job = session.get(BulkUserUpdateJob, job_id)
            if job is None:
                return
            for item in session.scalars(
                select(BulkUserUpdateItem).where(
                    BulkUserUpdateItem.job_id == job_id,
                    BulkUserUpdateItem.status == "UPDATING",
                )
            ):
                item.status = "PENDING"
                item.message = message
                item.updated_at = utc_now()
            job.status = "INTERRUPTED"
            job.last_error = message
            job.updated_at = utc_now()
            self._refresh_bulk_job_counts(session, job)

    def _refresh_bulk_job_counts(self, session: Session, job: BulkUserUpdateJob) -> None:
        items = list(session.scalars(select(BulkUserUpdateItem).where(BulkUserUpdateItem.job_id == job.id)))
        job.total_count = len(items)
        job.pending_count = sum(1 for item in items if item.status == "PENDING")
        job.updating_count = sum(1 for item in items if item.status == "UPDATING")
        job.verified_count = sum(1 for item in items if item.status == "VERIFIED")
        job.skipped_count = sum(1 for item in items if item.status == "SKIPPED")
        job.failed_count = sum(1 for item in items if item.status == "FAILED")
        job.updated_at = utc_now()

    def _process_observed_attendance(
        self,
        attendance: ZKAttendance,
        source_type: SourceType,
        *,
        zone_trusted_time: datetime | None = None,
    ) -> bool:
        trusted_now = zone_trusted_time or self.trusted_time.now().value
        with self.session_factory() as session:
            device = self._session_device(session)
            zone_config = self._zone_config(session)
            context = AttendanceContext(
                zone_id=zone_config.zone_id,
                timezone=zone_config.timezone,
                internet_online=self.trusted_time.last_head_office_time_utc is not None,
                current_clock_status=device.last_clock_status,
                pc_clock_suspicious=self.trusted_time.last_pc_tamper_at is not None,
                reconnect_clock_ok=self.reconnect_clock_ok,
                oracle_attendance_configured=zone_config.oracle_attendance_configured,
                oracle_cutover_utc=zone_config.oracle_cutover_utc,
            )
            before_id = attendance_processor.find_event_id(session, device, attendance, context)
            row = attendance_processor.process(
                session,
                device=device,
                attendance=attendance,
                context=context,
                source_type=source_type,
                zone_trusted_time=trusted_now,
            )
            inserted = row.id != before_id
            if inserted:
                device.last_error = (
                    f"Captured attendance for user {row.employee_name or row.user_id} "
                    f"via {source_type.value}."
                )
            session.commit()
            return inserted

    def _live_poll_reconcile_if_due(self, client: ZKClient) -> int:
        if not settings.live_poll_reconcile_enabled:
            return 0
        now = time.monotonic()
        if (
            self.last_live_poll_reconcile_monotonic is not None
            and now - self.last_live_poll_reconcile_monotonic
            < self.live_poll_reconcile_interval_seconds
        ):
            return 0
        self.last_live_poll_reconcile_monotonic = now
        try:
            return self._reconcile_dump(client, SourceType.LIVE_POLL)
        except Exception as exc:
            with self.session_factory() as session:
                device = self._session_device(session)
                device.last_error = f"Live attendance polling failed: {exc}"
                session.commit()
            return 0

    def _clock_check(self, client: ZKClient) -> None:
        trusted_now = self.trusted_time.now().value
        wall_now = utc_now()
        monotonic_ns = time.monotonic_ns()
        device_time = None
        error: str | None = None
        try:
            device_time = client.get_time()
        except Exception as exc:
            error = str(exc)

        with self.session_factory() as session:
            device = self._session_device(session)
            zone_config = self._zone_config(session)
            result = fraud_engine.classify_clock_check(
                device_time=device_time,
                trusted_time=trusted_now,
                timezone_name=zone_config.timezone,
                previous_device_time=self.previous_device_time,
                previous_trusted_time=self.previous_trusted_time,
            )
            if device_time is not None:
                self.previous_device_time = device_time
                self.previous_trusted_time = trusted_now
            self.last_clock_status = result.status
            self.reconnect_clock_ok = result.status == ClockStatus.OK
            device.last_clock_status = result.status.value
            device.last_drift_seconds = result.drift_seconds
            if error:
                device.last_error = error
            elif result.status == ClockStatus.OK:
                device.last_error = None
            else:
                device.last_error = result.reason
            row = ClockCheck(
                zone_id=zone_config.zone_id,
                device_id=device.device_id,
                device_serial=device.serial,
                device_time=device_time,
                trusted_time=trusted_now,
                windows_wall_time=wall_now,
                monotonic_ns=monotonic_ns,
                drift_seconds=result.drift_seconds,
                expected_device_time=result.expected_device_time,
                jump_seconds=result.jump_seconds,
                status=result.status.value,
                reason=error or result.reason,
                sync_status=SyncStatus.PENDING.value,
            )
            session.add(row)
            session.flush()
            payload = ClockCheckSyncItem(
                id=row.id,
                zone_id=row.zone_id,
                device_id=row.device_id,
                device_serial=row.device_serial,
                device_time=row.device_time,
                trusted_time=row.trusted_time,
                windows_wall_time=row.windows_wall_time,
                monotonic_ns=row.monotonic_ns,
                drift_seconds=row.drift_seconds,
                expected_device_time=row.expected_device_time,
                jump_seconds=row.jump_seconds,
                status=ClockStatus(row.status),
                reason=row.reason,
                created_at=row.created_at,
            )
            audit_ledger.append(session, "clock_check", row.id, payload)
            sync_queue_writer.enqueue(
                session,
                payload_type=PayloadType.CLOCK_CHECK,
                payload=payload,
                record_id=row.id,
            )
            if result.incident_type is not None and result.severity is not None:
                incident = FraudIncident(
                    zone_id=zone_config.zone_id,
                    device_id=device.device_id,
                    incident_type=result.incident_type.value,
                    severity=result.severity.value,
                    description=result.reason,
                    sync_status=SyncStatus.PENDING.value,
                )
                session.add(incident)
                session.flush()
                incident_payload = IncidentSyncItem(
                    id=incident.id,
                    zone_id=incident.zone_id,
                    device_id=incident.device_id,
                    incident_type=result.incident_type,
                    severity=result.severity,
                    description=incident.description,
                    created_at=incident.created_at,
                )
                audit_ledger.append(session, "fraud_incident", incident.id, incident_payload)
                sync_queue_writer.enqueue(
                    session,
                    payload_type=PayloadType.INCIDENT,
                    payload=incident_payload,
                    record_id=incident.id,
                )
            session.commit()

    def _mark_connecting(self, next_retry_seconds: int) -> None:
        with self.session_factory() as session:
            device = self._session_device(session)
            device.online = False
            device.last_error = (
                "Connecting to device for live capture and clock sync. "
                f"Next retry window: {next_retry_seconds} seconds."
            )
            if not device.last_clock_status:
                device.last_clock_status = "PENDING"
            session.commit()

    def _mark_offline(self, reason: str) -> None:
        with self.session_factory() as session:
            device = self._session_device(session)
            device.online = False
            device.last_error = reason
            open_outage = session.scalar(
                select(OutagePeriod).where(
                    OutagePeriod.device_id == device.device_id,
                    OutagePeriod.outage_type == OutageType.DEVICE_LAN_OUTAGE.value,
                    OutagePeriod.end_time == None,  # noqa: E711
                )
            )
            if open_outage is None:
                zone_config = self._zone_config(session)
                outage = OutagePeriod(
                    zone_id=zone_config.zone_id,
                    device_id=device.device_id,
                    outage_type=OutageType.DEVICE_LAN_OUTAGE.value,
                    start_time=self.trusted_time.now().value,
                    start_reason=reason,
                    classification="LAN_DEVICE_OFFLINE",
                    sync_status=SyncStatus.PENDING.value,
                )
                session.add(outage)
                session.flush()
                incident = FraudIncident(
                    zone_id=zone_config.zone_id,
                    device_id=device.device_id,
                    incident_type="DEVICE_LAN_BLIND_PERIOD",
                    severity=IncidentSeverity.MEDIUM.value,
                    description=f"Device became unreachable on LAN: {reason}",
                    related_outage_id=outage.id,
                    sync_status=SyncStatus.PENDING.value,
                )
                session.add(incident)
                session.flush()
                incident_payload = IncidentSyncItem(
                    id=incident.id,
                    zone_id=incident.zone_id,
                    device_id=incident.device_id,
                    incident_type="DEVICE_LAN_BLIND_PERIOD",
                    severity=IncidentSeverity.MEDIUM,
                    description=incident.description,
                    related_outage_id=outage.id,
                    created_at=incident.created_at,
                )
                audit_ledger.append(session, "fraud_incident", incident.id, incident_payload)
                sync_queue_writer.enqueue(
                    session,
                    payload_type=PayloadType.INCIDENT,
                    payload=incident_payload,
                    record_id=incident.id,
                )
            session.commit()

    def _close_outage(self, reason: str) -> None:
        with self.session_factory() as session:
            device = self._session_device(session)
            outage = session.scalar(
                select(OutagePeriod).where(
                    OutagePeriod.device_id == device.device_id,
                    OutagePeriod.outage_type == OutageType.DEVICE_LAN_OUTAGE.value,
                    OutagePeriod.end_time == None,  # noqa: E711
                )
            )
            if outage is not None:
                outage.end_time = self.trusted_time.now().value
                outage.duration_seconds = (outage.end_time - outage.start_time).total_seconds()
                outage.end_reason = reason
                outage_payload = OutageSyncItem(
                    id=outage.id,
                    zone_id=outage.zone_id,
                    device_id=outage.device_id,
                    outage_type=OutageType.DEVICE_LAN_OUTAGE,
                    start_time=outage.start_time,
                    end_time=outage.end_time,
                    duration_seconds=outage.duration_seconds,
                    start_reason=outage.start_reason,
                    end_reason=outage.end_reason,
                    classification=outage.classification,
                    created_at=outage.created_at,
                )
                audit_ledger.append(session, "outage_period", outage.id, outage_payload)
                sync_queue_writer.enqueue(
                    session,
                    payload_type=PayloadType.OUTAGE,
                    payload=outage_payload,
                    record_id=outage.id,
                )
            session.commit()

    def _reconcile_dump(self, client: ZKClient, source_type: SourceType) -> int:
        trusted_now = self.trusted_time.now().value
        attendances = client.get_attendance()
        inserted_count = 0
        with self.session_factory() as session:
            device = self._session_device(session)
            zone_config = self._zone_config(session)
            context = AttendanceContext(
                zone_id=zone_config.zone_id,
                timezone=zone_config.timezone,
                internet_online=self.trusted_time.last_head_office_time_utc is not None,
                current_clock_status=device.last_clock_status,
                reconnect_clock_ok=self.reconnect_clock_ok,
                oracle_attendance_configured=zone_config.oracle_attendance_configured,
                oracle_cutover_utc=zone_config.oracle_cutover_utc,
            )
            for item in attendances:
                before_id = attendance_processor.find_event_id(session, device, item, context)
                row = attendance_processor.process(
                    session,
                    device=device,
                    attendance=item,
                    context=context,
                    source_type=source_type,
                    zone_trusted_time=trusted_now,
                )
                if row.id != before_id:
                    inserted_count += 1
            if inserted_count and source_type == SourceType.LIVE_POLL:
                device.last_error = f"Captured {inserted_count} attendance record(s) via live polling."
            session.commit()
        return inserted_count


def _bulk_privilege(value: str | int | None) -> int:
    try:
        privilege = int(value) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0
    return privilege if privilege in {0, 14} else 0
