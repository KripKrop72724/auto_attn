#!/usr/bin/env python3
"""Isolated receipt/inspection load; never a firmware, Oracle or fleet verdict.

Creates and removes its own database in a local disposable PostgreSQL container.
Does not start ADD's application lifecycle or any external delivery worker.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import resource
import signal
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SITES, RATE = 17, 10
DELIVERY_GRACE_SECONDS = 15


class BurstInputs:
    """Bounded source arrivals independent of receipt/replay latency.

    The grace period belongs to receipt processing, never input generation.
    A full input queue, missed deadline or uncommitted item fails qualification.
    These RAM queues model only a test source, not ESP durable preservation.
    """

    def __init__(self, sites: int, rate: int, seconds: int, *, capacity: int, context=None):
        if min(sites, rate, seconds, capacity) < 1:
            raise ValueError("Positive bounded input dimensions required")
        self.queues = [(context.Queue if context else Queue)(maxsize=capacity) for _ in range(sites)]
        self.rate, self.seconds, self.capacity = rate, seconds, capacity
        self.done = (context.Event if context else Event)()
        self.lock = (context.Lock if context else Lock)()
        # Fixed shared counters avoid Queue.qsize(), which is unavailable on
        # macOS, and never retain an unbounded list of emitted observations.
        self.counters = context.Array('d', 4, lock=False) if context else [0.0] * 4
        self.offered_by_site = context.Array('q', sites, lock=False) if context else [0] * sites
        self.taken_by_site = context.Array('q', sites, lock=False) if context else [0] * sites

    @property
    def offered(self):
        return int(self.counters[0])

    @property
    def refused(self):
        return int(self.counters[1])

    @property
    def last_offer_elapsed_s(self):
        return self.counters[2]

    @property
    def max_emitter_lag_ms(self):
        return self.counters[3]

    def take(self, site: int, timeout: float):
        value = self.queues[site].get(timeout=timeout)
        with self.lock:
            self.taken_by_site[site] += 1
        return value

    def pending_depths(self):
        with self.lock:
            return [offered - taken for offered, taken in zip(self.offered_by_site, self.taken_by_site)]

    def emit(self, start: float, stop: Event, *, now=time.monotonic, wait=None):
        pause = wait or stop.wait
        try:
            for sequence in range(1, self.rate * self.seconds + 1):
                due = start + (sequence - 1) / self.rate
                pause(max(0, due - now()))
                for site, queue in enumerate(self.queues):
                    instant = now()
                    if stop.is_set() or instant >= start + self.seconds:
                        return
                    with self.lock:
                        self.counters[3] = max(self.counters[3], (instant - due) * 1000)
                        try:
                            queue.put_nowait((sequence, due))
                        except Full:
                            self.counters[1] += 1
                        else:
                            self.counters[0] += 1
                            self.offered_by_site[site] += 1
                            self.counters[2] = instant - start
        finally:
            self.done.set()

    def snapshot(self):
        with self.lock:
            return dict(offered=self.offered, input_refusals=self.refused,
                        input_generation_complete=self.done.is_set(),
                        input_queue_capacity_per_site=self.capacity,
                        last_offer_elapsed_s=round(self.last_offer_elapsed_s, 3),
                        max_emitter_lag_ms=round(self.max_emitter_lag_ms, 3))


def emit_in_process(inputs, start_value, start_gate, ready, stop):
    """A fresh interpreter owns arrivals; backend Python work cannot hold its GIL."""
    ready.set()
    while not start_gate.wait(0.1):
        if stop.is_set():
            inputs.done.set()
            return
    inputs.emit(start_value.value, stop)


def quantiles(values):
    ordered = sorted(values)
    return {f"p{p}": round(ordered[min(len(ordered)-1, int((len(ordered)-1)*p/100))], 3)
            for p in (50, 95, 99, 100)} if ordered else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--postgres-user", default="add_service")
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--drain-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", args.postgres_container)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,62}", args.postgres_user)
            or not 10 <= args.seconds <= 900 or not 0 <= args.drain_seconds <= 600):
        parser.error("Use bounded durations and simple local container/role names")
    output = args.output.resolve()
    if output.exists():
        parser.error("Choose a new output path; prior evidence is never overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.touch(mode=0o600, exist_ok=False)
    if os.environ.get("DOCKER_HOST") and not os.environ["DOCKER_HOST"].startswith("unix://"):
        parser.error("Only a local Docker daemon is permitted")
    endpoint = subprocess.check_output(["docker", "context", "inspect", "--format",
                                        "{{.Endpoints.docker.Host}}"], text=True).strip()
    if not endpoint.startswith("unix://"):
        parser.error("Only a local Docker daemon is permitted")
    port_mapping = subprocess.check_output(["docker", "port", args.postgres_container,
                                            "5432/tcp"], text=True).strip()
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", port_mapping):
        parser.error("The disposable PostgreSQL port must bind only to 127.0.0.1")
    port = int(port_mapping.rsplit(":", 1)[1])
    name = "custody_load_" + uuid4().hex
    url = f"postgresql+psycopg://{args.postgres_user}@127.0.0.1:{port}/{name}"
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    diff = subprocess.check_output(["git", "diff", "HEAD", "--", "apps/add_backend", "scripts/run_zkt_custody_load.py"], cwd=ROOT)
    # Configure before importing backend modules. Ignore deployment environment
    # and .env.add entirely, using a temporary working directory and fresh keys.
    for key in list(os.environ):
        if key.startswith("ADD_"):
            del os.environ[key]
    from cryptography.fernet import Fernet
    os.environ.update(ADD_DATABASE_URL=url, ADD_PII_FERNET_KEY=Fernet.generate_key().decode(),
                      ADD_ORDS_BASE_URL="http://127.0.0.1:1/disabled-synthetic-only")
    sys.path.insert(0, str(ROOT / "apps/add_backend"))
    prior_cwd = Path.cwd()
    temp = TemporaryDirectory(prefix="zkt-custody-load-")
    os.chdir(temp.name)
    # A future accidental outbound call from a receipt/inspection path must
    # fail the test. libpq uses the explicitly bounded URL above.
    def network_guard(event, values):
        if event == "socket.connect":
            address = values[1]
            if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", port):
                raise RuntimeError("EXTERNAL_NETWORK_FORBIDDEN")
    sys.addaudithook(network_guard)
    from sqlalchemy import func, select
    from zk_add import db as database
    from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, ZKTDevice,
                               ZktCustodyWork, ZktDerivedEvidence, ZktObservationReceipt)
    from zk_add.schemas import Envelope
    from zk_add.web import persist_envelope
    from zk_add.zkt_custody import observation_id
    from zk_add.zkt_custody_runtime import BUSY_SECONDS, IDLE_SECONDS, CustodyProcessor

    input_context = multiprocessing.get_context("spawn")
    lock, stop_intake, stop_worker = Lock(), input_context.Event(), Event()
    input_start_value = input_context.Value('d', 0.0)
    input_start_gate, input_ready = input_context.Event(), input_context.Event()
    barrier = Barrier(SITES + 1)
    metrics = dict(committed=0, replays=0, errors=0, interrupted=False, last_commit_elapsed_s=0.0)
    service_ms, scheduled_ms, lag_ms, errors, samples, restarts = [], [], [], [], [], []
    processor = CustodyProcessor()
    inputs = BurstInputs(SITES, RATE, args.seconds, capacity=RATE * DELIVERY_GRACE_SECONDS,
                         context=input_context)
    start = 0.0
    expected = SITES * RATE * args.seconds
    base_time = datetime.now(timezone.utc)
    context = dict(scope="ISOLATED_BACKEND_COMPONENT_ONLY", full_hil="NOT_ASSERTED",
                   external_delivery_started=False, source_commit=source,
                   backend_diff_sha256=hashlib.sha256(diff).hexdigest() if diff else None,
                   harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   sites=SITES, rate_per_site=RATE, requested_duration_s=args.seconds,
                   receipt_grace_seconds=DELIVERY_GRACE_SECONDS,
                   input_emitter="SPAWNED_PROCESS",
                   expected=expected, database_name=name)

    def save(value):
        # Atomic replacement retains the latest complete progress report if
        # the process is interrupted, including errors and latency evidence.
        temporary = output.with_suffix(output.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({**context, **value}, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)

    def snapshot(state):
        with lock:
            counters = dict(metrics)
            durations, scheduled, lag = list(service_ms), list(scheduled_ms), list(lag_ms)
            failures = list(errors)
        return dict(state=state, elapsed_s=round(time.monotonic()-start, 3), **counters, **inputs.snapshot(),
                    unsent_or_failed=expected-counters["committed"], error_samples=failures,
                    handler_latency_ms=quantiles(durations), scheduled_commit_latency_ms=quantiles(scheduled),
                    producer_lag_ms=quantiles(lag), processor=processor.snapshot())

    def interrupt(signum, frame):
        metrics["interrupted"] = True
        stop_intake.set()

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)

    def send(site):
        pk, serial, epoch, index = site
        barrier.wait()
        while not stop_intake.is_set():
            if time.monotonic() >= start + args.seconds + DELIVERY_GRACE_SECONDS:
                break
            try:
                sequence, due = inputs.take(index, timeout=0.1)
            except Empty:
                # A multiprocessing feeder may still be publishing an item
                # after generation completes. Account for it before exiting.
                if inputs.done.is_set() and inputs.pending_depths()[index] == 0:
                    break
                continue
            began = time.monotonic()
            captured = base_time + timedelta(seconds=(sequence-1)/RATE)
            local = captured + timedelta(hours=5)
            body = bytearray(32)
            user = str((sequence-1) % 2048 + 1).encode()
            body[:len(user)] = user
            body[24:32] = bytes([7, 2, local.year-2000, local.month, local.day, local.hour, local.minute, local.second])
            raw = struct.pack("<HHHH", 500, 4321, 23, sequence % 65536) + body
            value = dict(observation_id=observation_id(serial, epoch, sequence), terminal_serial=serial,
                capture_epoch=epoch, capture_sequence=sequence, captured_at=captured.isoformat(),
                raw_b64=base64.b64encode(raw).decode(), raw_digest=hashlib.sha256(raw).hexdigest(),
                raw_format="LIVE_PACKET", decoder_profile="synthetic-unqualified", decoder_version="1", time_quality="UNKNOWN")
            envelope = Envelope(connector_id=f"SYNTHETIC-{index}", message_id=f"burst-{index}-{sequence}",
                boot_id=f"burst-{index}", seq=sequence, sent_at=captured, type="zkt_observation_batch",
                payload={"schema_version": 1, "observations": [value]})
            try:
                receipt = persist_envelope(pk, envelope).ack["items"][0]
                assert receipt["custody"] == "PRESERVED_UNRESOLVED"
                finished = time.monotonic()
                with lock:
                    metrics["committed"] += 1
                    metrics["last_commit_elapsed_s"] = max(metrics["last_commit_elapsed_s"], finished-start)
                    service_ms.append((finished-began)*1000)
                    scheduled_ms.append((finished-due)*1000)
                    lag_ms.append((began-due)*1000)
                if sequence % 97 == 0:
                    repeated = persist_envelope(pk, envelope).ack["items"][0]
                    assert repeated["receipt_id"] == receipt["receipt_id"] and repeated["replay"]
                    with lock:
                        metrics["replays"] += 1
            except Exception as exc:
                with lock:
                    metrics["errors"] += 1
                    if len(errors) < 20:
                        errors.append(dict(site=index, sequence=sequence, type=type(exc).__name__))

    def inspect():
        nonlocal processor
        next_restart = time.monotonic() + 300
        while not stop_worker.is_set():
            groups, error = processor._one_tick()
            if time.monotonic() >= next_restart:
                restarts.append(processor.snapshot())
                processor = CustodyProcessor()
                next_restart = time.monotonic() + 300
            stop_worker.wait(BUSY_SECONDS if groups and not error else IDLE_SECONDS)

    def docker(*command):
        return subprocess.run(["docker", "exec", args.postgres_container, *command],
                              check=True, capture_output=True)

    created, worker, emitter = False, None, None
    try:
        docker("createdb", "-U", args.postgres_user, "--template=template0", name)
        created = True
        from alembic.command import check, upgrade
        from alembic.config import Config
        config = Config(str(ROOT / "apps/add_backend/alembic.ini"))
        upgrade(config, "head")
        check(config)
        sites = []
        with database.session_scope() as db:
            for index in range(SITES):
                serial = f"SYNTHETIC-{index:02}"
                connector = Connector(connector_id=f"SYNTHETIC-{index}", hardware_id=f"aa:bb:cc:dd:ee:{index:02x}",
                    zone_id=f"SYNTHETIC-{index}", zone_name="Synthetic local load", device_id=f"SYNTHETIC-{index}",
                    display_name="Synthetic local load", zkt_custody_enabled=True)
                db.add(connector)
                db.flush()
                connector.zkt_device = ZKTDevice(connector_id=connector.id, serial=serial, confirmed_serial=serial)
                sites.append((connector.id, serial, f"{index+1:032x}", index))
        emitter = input_context.Process(target=emit_in_process,
            args=(inputs, input_start_value, input_start_gate, input_ready, stop_intake), daemon=True)
        emitter.start()
        if not input_ready.wait(15):
            raise RuntimeError("INPUT_EMITTER_DID_NOT_START")
        context["input_emitter_pid"] = emitter.pid
        worker = Thread(target=inspect, daemon=True)
        worker.start()
        with ThreadPoolExecutor(max_workers=SITES) as executor:
            futures = [executor.submit(send, site) for site in sites]
            # Startup and interpreter imports happen before this measured
            # window. Both processes use the same system monotonic clock.
            start = time.monotonic() + 0.1
            base_time = datetime.now(timezone.utc) + timedelta(seconds=0.1)
            input_start_value.value = start
            barrier.wait()
            input_start_gate.set()
            save(snapshot("RUNNING"))
            while not all(future.done() for future in futures):
                time.sleep(min(30, max(1, start+args.seconds-time.monotonic())))
                sample = snapshot("RUNNING")
                samples.append(sample)
                save({**sample, "samples": samples})
                print(json.dumps({key: sample[key] for key in ("elapsed_s", "committed", "replays", "errors")}), flush=True)
                if emitter.exitcode not in (None, 0):
                    stop_intake.set()
                    raise RuntimeError("INPUT_EMITTER_FAILED")
            for future in futures:
                future.result()
        emitter.join(timeout=1)
        if emitter.is_alive():
            raise RuntimeError("INPUT_EMITTER_DID_NOT_STOP")
        if emitter.exitcode != 0:
            raise RuntimeError("INPUT_EMITTER_FAILED")
        delivery_elapsed = time.monotonic()-start
        deadline = time.monotonic()+args.drain_seconds
        while time.monotonic() < deadline and not metrics["interrupted"]:
            with database.session_scope() as db:
                pending = db.scalar(select(func.count()).select_from(ZktCustodyWork).where(ZktCustodyWork.next_attempt_at.is_not(None)))
            if not pending:
                break
            time.sleep(2)
        stop_worker.set()
        worker.join(timeout=45)
        if worker.is_alive():
            raise RuntimeError("INSPECTOR_DID_NOT_STOP")
        with database.session_scope() as db:
            totals = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                      for model in (ZktObservationReceipt, ZktCustodyWork, ZktDerivedEvidence, AttendanceEvent, OrdsOutbox)}
            per_site = dict(db.execute(select(ZktObservationReceipt.connector_id, func.count()).group_by(ZktObservationReceipt.connector_id)).all())
            states = dict(db.execute(select(ZktCustodyWork.state, func.count()).group_by(ZktCustodyWork.state)).all())
            interpretations = dict(db.execute(select(ZktDerivedEvidence.result, func.count()).group_by(ZktDerivedEvidence.result)).all())
            pending = db.scalar(select(func.count()).select_from(ZktCustodyWork).where(ZktCustodyWork.next_attempt_at.is_not(None)))
        result = {**snapshot("COMPLETE"), "delivery_elapsed_s": round(delivery_elapsed, 3), "totals": totals,
                  "unoffered_inputs": expected-inputs.offered-inputs.refused,
                  "uncommitted_offered_inputs": inputs.offered-metrics["committed"],
                  "pending_input_queue_depths": inputs.pending_depths(),
                  "input_emitter_exitcode": emitter.exitcode,
                  "per_synthetic_site": per_site, "work_states": states, "pending_after_drain": pending,
                  "interpretation_results": interpretations,
                  "samples": samples, "prior_processors": restarts,
                  "max_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        result["committed_custody_accounted"] = (
            totals["add_zkt_observation_receipts"] == totals["add_zkt_custody_work"] == metrics["committed"])
        result["offered_load_passed"] = (not metrics["interrupted"] and not metrics["errors"]
            and inputs.offered == expected and not inputs.refused
            and inputs.last_offer_elapsed_s < args.seconds
            and inputs.max_emitter_lag_ms <= 1000 / RATE
            and metrics["committed"] == expected and len(per_site) == SITES
            and all(count == RATE*args.seconds for count in per_site.values()))
        result["component_passed"] = (result["committed_custody_accounted"] and result["offered_load_passed"]
            and metrics["last_commit_elapsed_s"] <= args.seconds + DELIVERY_GRACE_SECONDS
            and not pending and not totals["add_attendance_events"] and not totals["add_ords_outbox"]
            and states == {"WAIT_PROFILE": expected} and interpretations == {"UNQUALIFIED_FACTS": expected}
            and result["scheduled_commit_latency_ms"]["p95"] <= 5000
            and result["scheduled_commit_latency_ms"]["p99"] <= 15000)
        save(result)
        print(json.dumps({key: result[key] for key in ("committed", "errors", "pending_after_drain",
                         "committed_custody_accounted", "offered_load_passed", "component_passed")}), flush=True)
        return 0 if result["component_passed"] else 1
    except Exception as exc:
        stop_intake.set()
        save({**snapshot("INCOMPLETE"), "failure_type": type(exc).__name__, "samples": samples})
        raise
    finally:
        stop_intake.set()
        stop_worker.set()
        if emitter is not None:
            emitter.join(timeout=1)
            if emitter.is_alive():
                emitter.terminate()
                emitter.join(timeout=1)
        if worker is not None:
            worker.join(timeout=45)
        database.engine.dispose()
        # Preserve the isolated database if a worker is still using it.
        if created and (worker is None or not worker.is_alive()):
            docker("dropdb", "-U", args.postgres_user, "--if-exists", name)
        os.chdir(prior_cwd)
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
