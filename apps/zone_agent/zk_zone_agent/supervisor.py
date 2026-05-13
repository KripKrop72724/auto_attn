from __future__ import annotations

import threading

from sqlalchemy import select

from zk_common.enums import IncidentSeverity, PayloadType, SyncStatus
from zk_common.schemas import DeviceHeartbeat, HeartbeatRequest, IncidentSyncItem
from zk_zone_agent import APP_VERSION
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.config import config_manager
from zk_zone_agent.db import Device, FraudIncident, ServiceEvent, SessionLocal, init_db, session_scope
from zk_zone_agent.device_registry import device_registry
from zk_zone_agent.device_worker import DeviceWorker
from zk_zone_agent.settings import settings
from zk_zone_agent.sync import HeadOfficeClient, SyncWorker, sync_queue_writer
from zk_zone_agent.trusted_time import trusted_time_service


class ZoneSupervisor:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.started = False
        self.sync_worker = SyncWorker(self.stop_event)
        self.time_thread = threading.Thread(target=self._time_loop, name="trusted-time-loop", daemon=True)
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat-loop", daemon=True)
        self.device_workers: dict[str, DeviceWorker] = {}

    def start(self) -> None:
        if self.started:
            return
        init_db()
        self._record_service_start()
        if settings.disable_workers:
            self.started = True
            return
        self.sync_worker.start()
        self.time_thread.start()
        self.heartbeat_thread.start()
        self._start_device_workers()
        self.started = True

    def stop(self) -> None:
        self.stop_event.set()
        with session_scope() as session:
            session.add(ServiceEvent(event_type="SERVICE_STOPPED_CLEANLY", description="Service stopped cleanly."))

    def _record_service_start(self) -> None:
        with session_scope() as session:
            last_event = session.scalar(select(ServiceEvent).order_by(ServiceEvent.id.desc()).limit(1))
            session.add(ServiceEvent(event_type="SERVICE_STARTED", description="Zone agent service started."))
            config = config_manager.get(session)
            if config and last_event and last_event.event_type != "SERVICE_STOPPED_CLEANLY":
                incident = FraudIncident(
                    zone_id=config.zone_id,
                    device_id=None,
                    incident_type="ZONE_AGENT_UNEXPECTED_STOP",
                    severity=IncidentSeverity.HIGH.value,
                    description="Previous service run did not record a clean shutdown.",
                    sync_status=SyncStatus.PENDING.value,
                )
                session.add(incident)
                session.flush()
                payload = IncidentSyncItem(
                    id=incident.id,
                    zone_id=incident.zone_id,
                    device_id=None,
                    incident_type="ZONE_AGENT_UNEXPECTED_STOP",
                    severity=IncidentSeverity.HIGH,
                    description=incident.description,
                    created_at=incident.created_at,
                )
                audit_ledger.append(session, "fraud_incident", incident.id, payload)
                sync_queue_writer.enqueue(
                    session,
                    payload_type=PayloadType.INCIDENT,
                    payload=payload,
                    record_id=incident.id,
                )

    def _start_device_workers(self) -> None:
        with session_scope() as session:
            config = config_manager.get(session)
            if not config:
                return
            devices = device_registry.enabled_devices(session)
            for device in devices:
                if device.device_id in self.device_workers:
                    continue
                worker = DeviceWorker(
                    device_id=device.device_id,
                    zone_config=config,
                    stop_event=self.stop_event,
                    trusted_time=trusted_time_service,
                    session_factory=SessionLocal,
                    clock_interval_seconds=settings.clock_check_interval_seconds,
                )
                self.device_workers[device.device_id] = worker
                worker.start()

    def refresh_device_workers(self) -> None:
        if not settings.disable_workers:
            self._start_device_workers()

    def _time_loop(self) -> None:
        while not self.stop_event.is_set():
            with session_scope() as session:
                config = config_manager.get(session)
                if config:
                    tamper = trusted_time_service.check_pc_clock_tamper()
                    if tamper:
                        incident = trusted_time_service.record_pc_tamper(session, config.zone_id, tamper)
                        payload = IncidentSyncItem(
                            id=incident.id,
                            zone_id=incident.zone_id,
                            device_id=None,
                            incident_type="ZONE_PC_CLOCK_TAMPER",
                            severity=IncidentSeverity.CRITICAL,
                            description=incident.description,
                            created_at=incident.created_at,
                        )
                        sync_queue_writer.enqueue(
                            session,
                            payload_type=PayloadType.INCIDENT,
                            payload=payload,
                            record_id=incident.id,
                        )
                    try:
                        server_utc = HeadOfficeClient(config.head_office_url, config.zone_token).get_time()
                        trusted_time_service.update_from_head_office(server_utc, session)
                    except Exception:
                        pass
            self.stop_event.wait(settings.time_sync_interval_seconds)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            with session_scope() as session:
                config = config_manager.get(session)
                if config:
                    devices = [
                        DeviceHeartbeat(
                            device_id=device.device_id,
                            serial=device.serial,
                            online=device.online,
                            last_clock_status=device.last_clock_status,
                            last_drift_seconds=device.last_drift_seconds,
                        )
                        for device in session.scalars(select(Device).order_by(Device.label.asc()))
                    ]
                    heartbeat = HeartbeatRequest(
                        zone_id=config.zone_id,
                        zone_name=config.zone_name,
                        agent_version=APP_VERSION,
                        server_time_estimate=trusted_time_service.now().value,
                        devices=devices,
                        pending_queue_count=sync_queue_writer.pending_count(session),
                    )
                    try:
                        HeadOfficeClient(config.head_office_url, config.zone_token).post_json(
                            "/api/zones/heartbeat", heartbeat.model_dump(mode="json")
                        )
                    except Exception:
                        pass
            self.stop_event.wait(settings.heartbeat_interval_seconds)


zone_supervisor = ZoneSupervisor()
