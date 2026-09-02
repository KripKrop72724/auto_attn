from __future__ import annotations

import signal

import docker_healthcheck


def test_repeated_liveness_failures_terminate_uvicorn(monkeypatch, tmp_path):
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


def test_readiness_failure_does_not_terminate_live_api(monkeypatch, tmp_path):
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


def test_threadpool_starvation_counts_as_liveness_failure(monkeypatch, tmp_path):
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
    (proc / "23" / "cmdline").write_bytes(b"python\0-m\0uvicorn\0zk_add.web:app\0")
    (proc / "24" / "cmdline").write_bytes(b"python\0worker.py\0")
    killed = []

    original_path = docker_healthcheck.Path

    def mapped_path(value):
        if value == "/proc":
            return proc
        return original_path(value)

    monkeypatch.setattr(docker_healthcheck, "Path", mapped_path)
    monkeypatch.setattr(docker_healthcheck.os, "getpid", lambda: 99)
    monkeypatch.setattr(
        docker_healthcheck.os,
        "kill",
        lambda process_id, sent_signal: killed.append((process_id, sent_signal)),
    )

    docker_healthcheck.terminate_api_process()

    assert killed == [(23, signal.SIGKILL)]
