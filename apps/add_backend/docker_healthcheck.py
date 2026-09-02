"""Docker health probe with bounded self-healing for a wedged ADD API process.

Docker records an unhealthy container but does not restart it.  This probe
terminates the Uvicorn process only after repeated *liveness* failures, allowing
the existing restart policy to recover a process whose event loop no longer
accepts work.  Compose uses an init process as PID 1, so the probe resolves the
actual API child through ``/proc`` rather than signaling the container init.
Readiness failures still mark the container unhealthy, but dependency failures
alone never trigger a restart loop.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import urllib.error
import urllib.request


LIVE_URL = "http://127.0.0.1:8096/health/live"
SERVE_URL = "http://127.0.0.1:8096/health/serve"
READY_URL = "http://127.0.0.1:8096/health/ready"
FAILURE_FILE = Path("/tmp/add-liveness-failures")
FAILURES_BEFORE_RESTART = max(
    2, int(os.environ.get("ADD_LIVENESS_FAILURES_BEFORE_RESTART", "3"))
)


def probe(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def read_failures() -> int:
    try:
        return int(FAILURE_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def write_failures(value: int) -> None:
    try:
        FAILURE_FILE.write_text(str(value), encoding="ascii")
    except OSError:
        pass


def terminate_api_process() -> None:
    own_pid = os.getpid()
    candidates: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        process_id = int(entry.name)
        if process_id in {1, own_pid}:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"uvicorn" in command and b"zk_add.web:app" in command:
            candidates.append(process_id)
    if not candidates:
        raise RuntimeError("The unhealthy Uvicorn process could not be resolved.")
    for process_id in candidates:
        os.kill(process_id, signal.SIGKILL)


def main() -> int:
    event_loop_live = probe(LIVE_URL, timeout=2.0)
    request_threadpool_live = event_loop_live and probe(SERVE_URL, timeout=2.0)
    if not event_loop_live or not request_threadpool_live:
        failures = read_failures() + 1
        write_failures(failures)
        if failures >= FAILURES_BEFORE_RESTART:
            print(
                f"ADD liveness failed {failures} consecutive probes; restarting API process.",
                file=sys.stderr,
            )
            terminate_api_process()
        return 1

    write_failures(0)
    if not probe(READY_URL, timeout=3.0):
        print("ADD is live but not ready.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
