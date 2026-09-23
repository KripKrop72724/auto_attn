from __future__ import annotations

import signal
import pytest

import docker_healthcheck


@pytest.fixture()
def running_api(monkeypatch):
    monkeypatch.setattr(docker_healthcheck, "api_process_ids", lambda: [23])
    monkeypatch.setattr(docker_healthcheck, "api_startup_complete", lambda _ids: True)


def test_repeated_liveness_failures_terminate_uvicorn(monkeypatch, tmp_path, running_api):
    failure_file = tmp_path / "failures"
    failure_file.write_text("2", encoding="ascii")
    terminated = []

    monkeypatch.setattr(docker_healthcheck, "FAILURE_FILE", failure_file)
    monkeypatch.setattr(docker_healthcheck, "FAILURES_BEFORE_RESTART", 3)
    monkeypatch.setattr(docker_healthcheck, "probe", lambda _url, timeout: False)
    monkeypatch.setattr(
        docker_healthcheck,
        "terminate_api_process",
        lambda: terminated.append(True),
    )

    assert docker_healthcheck.main() == 1
    assert failure_file.read_text(encoding="ascii") == "3"
    assert terminated == [True]


def test_readiness_failure_does_not_terminate_live_api(monkeypatch, tmp_path, running_api):
    failure_file = tmp_path / "failures"
    failure_file.write_text("2", encoding="ascii")
    terminated = []

    monkeypatch.setattr(docker_healthcheck, "FAILURE_FILE", failure_file)
    monkeypatch.setattr(
        docker_healthcheck,
        "probe",
        lambda url, timeout: url in {
            docker_healthcheck.LIVE_URL,
            docker_healthcheck.SERVE_URL,
        },
    )
    monkeypatch.setattr(
        docker_healthcheck,
        "terminate_api_process",
        lambda: terminated.append(True),
    )

    assert docker_healthcheck.main() == 1
    assert failure_file.read_text(encoding="ascii") == "0"
    assert terminated == []


def test_threadpool_starvation_counts_as_liveness_failure(monkeypatch, tmp_path, running_api):
    failure_file = tmp_path / "failures"
    terminated = []

    monkeypatch.setattr(docker_healthcheck, "FAILURE_FILE", failure_file)
    monkeypatch.setattr(docker_healthcheck, "FAILURES_BEFORE_RESTART", 2)
    monkeypatch.setattr(
        docker_healthcheck,
        "probe",
        lambda url, timeout: url == docker_healthcheck.LIVE_URL,
    )
    monkeypatch.setattr(
        docker_healthcheck,
        "terminate_api_process",
        lambda: terminated.append(True),
    )

    assert docker_healthcheck.main() == 1
    assert docker_healthcheck.main() == 1
    assert terminated == [True]


def test_terminate_api_process_targets_uvicorn_child(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True)
    (proc / "23").mkdir()
    (proc / "24").mkdir()
    (proc / "25").mkdir()
    (proc / "26").mkdir()
    (proc / "23" / "cmdline").write_bytes(b"python\0-m\0uvicorn\0zk_add.web:app\0")
    (proc / "24" / "cmdline").write_bytes(
        b"/bin/sh\0-c\0alembic upgrade head && exec uvicorn zk_add.web:app\0"
    )
    (proc / "25" / "cmdline").write_bytes(b"python\0/usr/local/bin/alembic\0upgrade\0head\0")
    (proc / "26" / "cmdline").write_bytes(
        b"/usr/local/bin/python\0/usr/local/bin/uvicorn\0zk_add.web:app\0"
    )
    killed = []

    original_path = docker_healthcheck.Path

    def mapped_path(value):
        if value == "/proc":
            return proc
        return original_path(value)

    monkeypatch.setattr(docker_healthcheck, "Path", mapped_path)
    monkeypatch.setattr(docker_healthcheck.os, "getpid", lambda: 99)
    monkeypatch.setattr(docker_healthcheck, "api_startup_complete", lambda _ids: True)
    monkeypatch.setattr(
        docker_healthcheck.os,
        "kill",
        lambda process_id, sent_signal: killed.append((process_id, sent_signal)),
    )

    docker_healthcheck.terminate_api_process()

    assert sorted(killed) == [(23, signal.SIGKILL), (26, signal.SIGKILL)]


@pytest.mark.parametrize("process_ids", [[], [23]])
def test_migration_and_initial_api_startup_never_accumulate_restart_failures(
    monkeypatch, tmp_path, process_ids
):
    failures = tmp_path / "failures"
    failures.write_text("20")
    monkeypatch.setattr(docker_healthcheck, "FAILURE_FILE", failures)
    monkeypatch.setattr(docker_healthcheck, "probe", lambda _url, timeout: False)
    monkeypatch.setattr(docker_healthcheck, "api_process_ids", lambda: process_ids)
    monkeypatch.setattr(docker_healthcheck, "api_startup_complete", lambda _ids: False)
    killed = []
    monkeypatch.setattr(docker_healthcheck.os, "kill", lambda *args: killed.append(args))
    for _ in range(20):
        assert docker_healthcheck.main() == 1
    assert failures.read_text() == "0"
    assert not killed


@pytest.mark.parametrize("started,ready", [(5000, True), (11000, False), (13000, False)])
def test_startup_grace_uses_kernel_process_age(monkeypatch, tmp_path, started, ready):
    proc = tmp_path / "proc"
    (proc / "23").mkdir(parents=True)
    (proc / "uptime").write_text("120.0 1.0")
    (proc / "23" / "stat").write_text(
        "23 (python worker (api)) S " + "0 " * 18 + str(started) + " 0"
    )
    original_path = docker_healthcheck.Path
    monkeypatch.setattr(
        docker_healthcheck,
        "Path",
        lambda path: proc / path.removeprefix("/proc/")
        if path.startswith("/proc/") else original_path(path),
    )
    monkeypatch.setattr(docker_healthcheck.os, "sysconf", lambda _name: 100)
    monkeypatch.setattr(docker_healthcheck, "STARTUP_GRACE_SECONDS", 60)
    assert docker_healthcheck.api_startup_complete([23]) is ready
