from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from zk_common.time_utils import utc_now
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.crypto import protect_secret, unprotect_secret
from zk_zone_agent.db import (
    CommKeyBruteforceAttempt,
    CommKeyBruteforceJob,
    Device,
    DeviceDiscoveryResult,
    SessionLocal,
)
from zk_zone_agent.settings import settings
from zk_zone_agent.zk_client import PyZKClient, ZKClient, ZKDeviceInfo


COMMON_COMM_KEYS = [0, 12345, 123456, 9999, 1979]
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED"}
ACTIVE_STATUSES = {"PENDING", "RUNNING", "PAUSED"}


ClientFactory = Callable[..., ZKClient]


@dataclass(frozen=True)
class BruteForceStart:
    candidate_id: int | None
    ip: str
    port: int
    mode: str = "SAFE_FAST"
    range_start: int = 0
    range_end: int = 999999
    worker_count: int | None = None
    timeout_seconds: float | None = None
    common_keys: list[int] | None = None
    allow_configured: bool = False


class CommKeyBruteforceManager:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session] = SessionLocal,
        client_factory: ClientFactory = PyZKClient,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.stop_event = stop_event or threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._job_locks: dict[int, threading.Lock] = {}
        self._manager_lock = threading.Lock()

    def start_pending_jobs(self) -> None:
        with self.session_factory() as session:
            jobs = session.scalars(
                select(CommKeyBruteforceJob).where(
                    CommKeyBruteforceJob.status.in_(["PENDING", "RUNNING"])
                )
            ).all()
        for job in jobs:
            self._ensure_thread(job.id)

    def start_job(self, request: BruteForceStart, *, enforce_enabled: bool = True) -> CommKeyBruteforceJob:
        if enforce_enabled and not settings.bruteforce_enabled:
            raise PermissionError("Comm Key brute force is disabled by local settings.")
        ip = ipaddress.ip_address(request.ip)
        if not ip.is_private:
            raise ValueError("Comm Key brute force is only allowed for private LAN IP addresses.")
        if request.range_start < 0 or request.range_end < request.range_start:
            raise ValueError("Invalid Comm Key range.")

        mode = request.mode.upper()
        worker_count = self._resolve_worker_count(mode, request.worker_count)
        timeout_seconds = request.timeout_seconds or settings.bruteforce_default_timeout_seconds
        common_keys = self._normalized_common_keys(request.common_keys)

        with self.session_factory() as session:
            configured = session.scalar(
                select(Device).where(Device.ip == request.ip, Device.port == request.port, Device.enabled == True)  # noqa: E712
            )
            if configured and not request.allow_configured:
                raise ValueError("This device is already configured and owned by a DeviceWorker.")

            existing = session.scalar(
                select(CommKeyBruteforceJob)
                .where(
                    CommKeyBruteforceJob.ip == request.ip,
                    CommKeyBruteforceJob.port == request.port,
                    CommKeyBruteforceJob.status.in_(list(ACTIVE_STATUSES)),
                )
                .order_by(CommKeyBruteforceJob.id.desc())
            )
            if existing:
                return existing

            job = CommKeyBruteforceJob(
                device_candidate_id=request.candidate_id,
                ip=request.ip,
                port=request.port,
                mode=mode,
                status="PENDING",
                range_start=request.range_start,
                range_end=request.range_end,
                current_key=request.range_start,
                worker_count=worker_count,
                timeout_seconds=timeout_seconds,
                common_keys_json=json.dumps(common_keys),
                started_at=utc_now(),
            )
            session.add(job)
            session.flush()
            audit_ledger.append(
                session,
                "comm_key_bruteforce_job",
                job.id,
                {
                    "action": "START",
                    "ip": job.ip,
                    "port": job.port,
                    "mode": job.mode,
                    "range_start": job.range_start,
                    "range_end": job.range_end,
                    "worker_count": job.worker_count,
                },
            )
            session.commit()
            job_id = job.id
        self._ensure_thread(job_id)
        with self.session_factory() as session:
            return session.get(CommKeyBruteforceJob, job_id)  # type: ignore[return-value]

    def pause(self, job_id: int) -> None:
        self._transition(job_id, "PAUSED", "PAUSE")

    def resume(self, job_id: int) -> None:
        self._transition(job_id, "RUNNING", "RESUME")
        self._ensure_thread(job_id)

    def cancel(self, job_id: int) -> None:
        self._transition(job_id, "CANCELED", "CANCEL", ended=True)

    def serialize_job(self, job: CommKeyBruteforceJob, *, include_secret: bool = False) -> dict[str, Any]:
        total = max((job.range_end - job.range_start) + 1, 1)
        scanned_range = min(max(job.current_key - job.range_start, 0), total)
        progress = 100.0 if job.status == "SUCCEEDED" else (scanned_range / total) * 100
        elapsed = max((utc_now() - job.started_at).total_seconds(), 0.001)
        attempts_per_second = job.attempt_count / elapsed
        remaining = max(total - scanned_range, 0)
        eta_seconds = remaining / attempts_per_second if attempts_per_second > 0 else None
        payload = {
            "id": job.id,
            "device_candidate_id": job.device_candidate_id,
            "ip": job.ip,
            "port": job.port,
            "mode": job.mode,
            "status": job.status,
            "range_start": job.range_start,
            "range_end": job.range_end,
            "current_key": job.current_key,
            "attempt_count": job.attempt_count,
            "worker_count": job.worker_count,
            "timeout_seconds": job.timeout_seconds,
            "progress_percent": round(progress, 2),
            "attempts_per_second": round(attempts_per_second, 2),
            "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
            "found_key_available": bool(job.found_key_encrypted),
            "success_at": job.success_at.isoformat() if job.success_at else None,
            "started_at": job.started_at.isoformat(),
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
            "last_error": job.last_error,
        }
        if include_secret and job.found_key_encrypted:
            payload["found_key"] = unprotect_secret(job.found_key_encrypted)
        return payload

    def _ensure_thread(self, job_id: int) -> None:
        with self._manager_lock:
            thread = self._threads.get(job_id)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(target=self._run_job, args=(job_id,), name=f"comm-key-bruteforce-{job_id}", daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _run_job(self, job_id: int) -> None:
        try:
            self._set_status(job_id, "RUNNING")
            if self._try_common_keys(job_id):
                return
            threads = [
                threading.Thread(
                    target=self._range_worker,
                    args=(job_id,),
                    name=f"comm-key-bruteforce-{job_id}-{index}",
                    daemon=True,
                )
                for index in range(self._job_worker_count(job_id))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            with self.session_factory() as session:
                job = session.get(CommKeyBruteforceJob, job_id)
                if job and job.status == "RUNNING" and job.current_key > job.range_end:
                    job.status = "FAILED"
                    job.ended_at = utc_now()
                    job.updated_at = utc_now()
                    session.commit()
        except Exception as exc:
            self._fail_job(job_id, exc)

    def _try_common_keys(self, job_id: int) -> bool:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None:
                return True
            common_keys = [key for key in job.common_keys() if job.range_start <= key <= job.range_end]
        seen: set[int] = set()
        for key in common_keys:
            if key in seen or self.stop_event.is_set():
                continue
            seen.add(key)
            status = self._job_status(job_id)
            if status in TERMINAL_STATUSES:
                return True
            while status == "PAUSED" and not self.stop_event.is_set():
                time.sleep(0.2)
                status = self._job_status(job_id)
            if status in TERMINAL_STATUSES:
                return True
            success, error = self._attempt_key(job_id, key)
            self._record_attempts(job_id, key, key, 1, "SUCCEEDED" if success else "FAILED", error)
            if success:
                return True
        return False

    def _range_worker(self, job_id: int) -> None:
        while not self.stop_event.is_set():
            status = self._job_status(job_id)
            if status in TERMINAL_STATUSES:
                return
            if status == "PAUSED":
                time.sleep(0.2)
                continue
            chunk = self._claim_chunk(job_id)
            if chunk is None:
                return
            start_key, end_key = chunk
            attempts = 0
            last_error = None
            for key in range(start_key, end_key + 1):
                status = self._job_status(job_id)
                if status in TERMINAL_STATUSES:
                    return
                if status == "PAUSED":
                    break
                success, last_error = self._attempt_key(job_id, key)
                attempts += 1
                if success:
                    self._record_attempts(job_id, start_key, key, attempts, "SUCCEEDED", None)
                    return
                if attempts % 10 == 0:
                    time.sleep(0)
            if attempts:
                self._record_attempts(job_id, start_key, start_key + attempts - 1, attempts, "FAILED", last_error)

    def _attempt_key(self, job_id: int, key: int) -> tuple[bool, str | None]:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None:
                return False, "Job not found."
            ip, port, timeout_seconds = job.ip, job.port, job.timeout_seconds
        client = self.client_factory(ip=ip, port=port, comm_key=key, timeout=timeout_seconds)
        try:
            client.connect()
            info = client.get_info()
            self._mark_success(job_id, key, info)
            return True, None
        except Exception as exc:
            return False, str(exc)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def _claim_chunk(self, job_id: int) -> tuple[int, int] | None:
        lock = self._job_locks.setdefault(job_id, threading.Lock())
        with lock, self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None or job.status != "RUNNING" or job.current_key > job.range_end:
                return None
            start_key = job.current_key
            end_key = min(job.range_end, start_key + settings.bruteforce_chunk_size - 1)
            job.current_key = end_key + 1
            job.updated_at = utc_now()
            session.commit()
            return start_key, end_key

    def _record_attempts(
        self,
        job_id: int,
        bucket_start: int,
        bucket_end: int,
        attempts: int,
        status: str,
        last_error: str | None,
    ) -> None:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None:
                return
            if job.status in TERMINAL_STATUSES and status != "SUCCEEDED":
                return
            job.attempt_count += attempts
            if job.status != "SUCCEEDED":
                job.last_error = last_error
            job.updated_at = utc_now()
            session.add(
                CommKeyBruteforceAttempt(
                    job_id=job_id,
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    attempts=attempts,
                    status=status,
                    last_error=last_error,
                )
            )
            session.commit()

    def _mark_success(self, job_id: int, key: int, info: ZKDeviceInfo) -> None:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            now = utc_now()
            job.status = "SUCCEEDED"
            job.found_key_encrypted = protect_secret(str(key))
            job.success_at = now
            job.ended_at = now
            job.updated_at = now
            job.last_error = None
            if job.device_candidate_id:
                candidate = session.get(DeviceDiscoveryResult, job.device_candidate_id)
                if candidate is not None:
                    candidate.status = "VALIDATED_ZK"
                    candidate.serial = info.serial
                    candidate.platform = info.platform
                    candidate.device_name = info.device_name
                    candidate.last_error = None
                    candidate.updated_at = now
            audit_ledger.append(
                session,
                "comm_key_bruteforce_job",
                job.id,
                {
                    "action": "SUCCESS",
                    "ip": job.ip,
                    "port": job.port,
                    "serial": info.serial,
                    "platform": info.platform,
                },
            )
            session.commit()

    def _transition(self, job_id: int, status: str, action: str, *, ended: bool = False) -> None:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status in TERMINAL_STATUSES:
                return
            job.status = status
            job.updated_at = utc_now()
            if ended:
                job.ended_at = utc_now()
            audit_ledger.append(
                session,
                "comm_key_bruteforce_job",
                job.id,
                {"action": action, "ip": job.ip, "port": job.port},
            )
            session.commit()

    def _set_status(self, job_id: int, status: str) -> None:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            if status == "RUNNING" and job.status == "PAUSED":
                return
            job.status = status
            job.updated_at = utc_now()
            session.commit()

    def _job_status(self, job_id: int) -> str:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            return "CANCELED" if job is None else job.status

    def _job_worker_count(self, job_id: int) -> int:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            return 1 if job is None else job.worker_count

    def _fail_job(self, job_id: int, exc: Exception) -> None:
        with self.session_factory() as session:
            job = session.get(CommKeyBruteforceJob, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = "FAILED"
            job.last_error = str(exc)
            job.ended_at = utc_now()
            job.updated_at = utc_now()
            session.commit()

    def _resolve_worker_count(self, mode: str, requested: int | None) -> int:
        if mode == "SAFE_FAST":
            workers = settings.bruteforce_safe_fast_workers
        elif mode == "AGGRESSIVE":
            workers = requested or settings.bruteforce_aggressive_workers
        elif mode == "CUSTOM":
            workers = requested or 1
        else:
            raise ValueError("Unsupported brute-force mode.")
        cpu_limit = max(1, os.cpu_count() or 1) * 8
        global_limit = settings.bruteforce_global_max_workers or min(64, cpu_limit)
        return max(1, min(workers, settings.bruteforce_hard_per_device_workers, global_limit))

    def _normalized_common_keys(self, keys: list[int] | None) -> list[int]:
        combined = list(COMMON_COMM_KEYS)
        if keys:
            combined.extend(keys)
        normalized: list[int] = []
        for key in combined:
            if key < 0 or key in normalized:
                continue
            normalized.append(key)
        return normalized


comm_key_bruteforce_manager = CommKeyBruteforceManager()
