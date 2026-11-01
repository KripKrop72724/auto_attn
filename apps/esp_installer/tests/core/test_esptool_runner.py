"""The child-process runner, against a scripted stand-in for the frozen ``--tool`` exe."""
from __future__ import annotations

import os
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from sl_installer.core import esptool_runner, texts
from sl_installer.core.api import CancelToken
from sl_installer.core.esptool_runner import (SubprocessRunner, ToolCall, ToolResult, ToolRunner,
                                              advice_for, child_environment, failure_cause,
                                              tool_command, tool_version_problems)

from . import support

# Started through this interpreter on every system: Windows cannot start a text file
# as a program, and these tests are there for the Windows kill, timeout and pipes.
FAKE_TOOL = textwrap.dedent("""\
    import json, os, sys, time
    args = sys.argv[1:]
    assert args[0] == "--tool", args
    mode = args[2]
    if mode == "progress":
        for done in (0, 50, 100):
            sys.stdout.write("\\rWriting at 0x%08x [%-30s] %5.1f%% %d/100 bytes... " % (
                0x10000 + done, "=" * (done * 30 // 100), done, done))
            sys.stdout.flush()
        print("\\nWrote 100 bytes at 0x00010000 in 0.1 seconds (8.0 kbit/s).")
        print("Hash of data verified.")
    elif mode == "env":
        print(json.dumps({k: v for k, v in os.environ.items()
                          if k.startswith(("ESPTOOL", "NO_COLOR", "PYTHONUNBUFFERED", "TERM"))}))
        print("cwd=" + os.getcwd())
    elif mode == "fail":
        sys.stderr.write(" ".join(args[3:]).replace("\\\\n", "\\n") + "\\n")
        sys.exit(int(os.environ.get("FAKE_EXIT", "2")))
    elif mode == "sleep":
        print("starting", flush=True)
        time.sleep(float(args[3]))
    elif mode == "chatty":
        for _ in range(400):
            print(".", flush=True)
            time.sleep(0.01)
    elif mode == "secret":
        print("wifi_password=test-wifi-pass and a key " + "Q" * 20 + "x9" * 20)
""")


class _ScriptRunner(SubprocessRunner):
    """The real runner, starting ``python fake_installer.py --tool ...`` for the frozen exe."""

    def __init__(self, work_dir: Path, script: Path) -> None:
        super().__init__(work_dir, frozen=True, executable=sys.executable)
        self._script = str(script)

    def command(self, call):
        return [sys.executable, self._script, "--tool", call.tool, *call.args]


@pytest.fixture
def fake_tool(tmp_path):
    path = tmp_path / "fake_installer.py"
    path.write_text(FAKE_TOOL, encoding="utf-8")
    return path


@pytest.fixture
def runner(tmp_path, fake_tool):
    return _ScriptRunner(tmp_path / "work", fake_tool)


def call(*args, **kwargs):
    return ToolCall("esptool", tuple(args), "test call", **kwargs)


def test_command_shapes():
    assert tool_command("esptool", frozen=True, executable="C:/App/x.exe") == \
        ["C:/App/x.exe", "--tool", "esptool"]
    assert tool_command("espefuse", frozen=False, executable="python") == \
        ["python", "-m", "espefuse"]
    with pytest.raises(ValueError):
        tool_command("idf.py")


def test_progress_is_streamed_split_on_carriage_returns(runner):
    lines = []
    result = runner.run(call("progress"), on_line=lines.append)
    assert result.ok and result.returncode == 0
    progress = [line for line in lines if line.startswith("Writing at")]
    assert len(progress) == 3 and progress[-1].startswith("Writing at 0x00010064")
    assert "Writing at" not in result.output
    assert "Hash of data verified." in result.output


def test_environment_is_cleaned_and_config_pinned(runner, monkeypatch):
    monkeypatch.setenv("ESPTOOL_BEFORE", "no-reset")
    monkeypatch.setenv("ESPTOOL_FM", "qio")
    result = runner.run(call("env"))
    env_line, cwd_line = result.output.splitlines()[:2]
    import json
    env = json.loads(env_line)
    assert "ESPTOOL_BEFORE" not in env and "ESPTOOL_FM" not in env
    assert env["NO_COLOR"] == "1" and env["PYTHONUNBUFFERED"] == "1" and env["TERM"] == "dumb"
    config = Path(env["ESPTOOL_CFGFILE"])
    assert config.read_text(encoding="utf-8") == "[esptool]\n"
    assert cwd_line.startswith("cwd=") and os.path.samefile(cwd_line[4:], runner.work_dir)


def test_child_environment_keeps_the_rest(tmp_path):
    env = child_environment(tmp_path / "e.cfg", {"PATH": "/bin", "esptool_chip": "esp32"})
    assert env["PATH"] == "/bin" and "esptool_chip" not in env


def test_exit_code_and_failure_mapping(runner):
    result = runner.run(call("fail", "A fatal error occurred: Failed to connect to ESP32-S3:",
                             "No serial data received."))
    assert not result.ok and result.returncode == 2
    assert advice_for(result) is texts.AUTO_DOWNLOAD_FAILED
    assert "No serial data received" in failure_cause(result)


def test_total_timeout_kills_the_child(runner):
    result = runner.run(call("sleep", "30", timeout_s=1.0, idle_timeout_s=10.0))
    assert result.timed_out and not result.ok and result.duration_s < 10
    assert advice_for(result) is texts.TOOL_TIMEOUT


def test_idle_timeout_kills_a_silent_child(runner):
    result = runner.run(call("sleep", "30", timeout_s=60.0, idle_timeout_s=1.0))
    assert result.timed_out and result.duration_s < 10


def test_a_chatty_child_is_not_idle(runner):
    result = runner.run(call("chatty", timeout_s=30.0, idle_timeout_s=1.0))
    assert result.ok


def test_cancel_kills_the_child(runner):
    cancel = CancelToken()
    threading.Timer(0.5, cancel.cancel).start()
    result = runner.run(call("sleep", "30", timeout_s=60, idle_timeout_s=60), cancel=cancel)
    assert result.cancelled and advice_for(result) is texts.CANCELLED


def test_output_is_redacted(runner):
    lines = []
    result = runner.run(call("secret"), on_line=lines.append)
    assert result.ok and "wifi_password=[redacted]" in result.output  # the child really ran
    assert "test-wifi-pass" not in result.output and "test-wifi-pass" not in "".join(lines)
    assert "QQQQ" not in result.output


def test_missing_executable_means_a_damaged_install(tmp_path):
    runner = SubprocessRunner(tmp_path / "w", frozen=True, executable=str(tmp_path / "nope"))
    result = runner.run(call("version"))
    assert result.start_error and advice_for(result) is texts.TOOL_MISSING


@pytest.mark.parametrize("output, advice", [
    (support.PORT_BUSY, texts.PORT_BUSY),
    (support.PORT_GONE, texts.BOARD_DISCONNECTED),
    ("A fatal error occurred: Could not open COM5, the port is busy or doesn't exist.",
     texts.PORT_BUSY),
    ("could not open port /dev/cu.usbmodem101: [Errno 16] Resource busy", texts.PORT_BUSY),
    (support.FAILED_TO_CONNECT, texts.AUTO_DOWNLOAD_FAILED),
    ("A fatal error occurred: Failed to connect to ESP32-S3: Wrong boot mode detected (0x8)! "
     "The chip needs to be in download mode.", texts.AUTO_DOWNLOAD_FAILED),
    ("A fatal error occurred: Failed to connect to ESP32-S3: Download mode successfully "
     "detected, but getting no sync reply: The serial TX path seems to be down.",
     texts.CABLE_PROBLEM),
    (support.SERIAL_NOISE, texts.CONNECTION_LOST),
    ("A fatal error occurred: Invalid head of packet (0x65): Possible serial noise or "
     "corruption.", texts.CONNECTION_LOST),
    (support.WRONG_CHIP, texts.WRONG_CHIP),
    (support.SDM_REFUSAL, texts.ALREADY_SET_UP),
    ("A fatal error occurred: Secure Download Mode is enabled. The tool can not read eFuses.",
     texts.ALREADY_SET_UP),
    ("A fatal error occurred: MD5 of file does not match data in flash!",
     texts.COPY_CHECK_FAILED),
    ("ModuleNotFoundError: No module named 'esptool'", texts.TOOL_MISSING),
    ("A fatal error occurred: something new", texts.TOOL_FAILED),
    # A board that drops off USB mid-call: pyserial words it as PermissionError on
    # Windows (WinError 5, 22, 31) and as "multiple access on port?" elsewhere. Only a
    # port that would not open is busy.
    ("Writing at 0x0002c000... (40 %)\nA serial exception error occurred: WriteFile failed "
     "(PermissionError(13, 'The device does not recognize the command.', None, 22))\n"
     "Note: This error originates from pySerial.", texts.CONNECTION_LOST),
    ("A serial exception error occurred: ClearCommError failed (PermissionError(13, "
     "'Access is denied.', None, 5))", texts.CONNECTION_LOST),
    ("A serial exception error occurred: GetOverlappedResult failed (PermissionError(13, "
     "'A device attached to the system is not functioning.', None, 31))", texts.CONNECTION_LOST),
    ("Traceback (most recent call last):\nserial.serialutil.SerialException: ReadFile failed "
     "(PermissionError(13, 'Access is denied.', None, 5))", texts.CONNECTION_LOST),
    ("A serial exception error occurred: device reports readiness to read but returned no "
     "data (device disconnected or multiple access on port?)", texts.CONNECTION_LOST),
    # esptool's own retry after a drop, whose reopen then fails: still the drop.
    ("Lost connection, retrying...\nWaiting for the chip to reconnect.....\nA serial exception "
     "error occurred: could not open port 'COM7': PermissionError(13, 'Access is denied.', "
     "None, 5)", texts.CONNECTION_LOST),
    ("Could not open COM5, the port is busy or doesn't exist.\n(could not open port 'COM5': "
     "PermissionError(13, 'Access is denied.', None, 5))", texts.PORT_BUSY),
    ("A fatal error occurred: Could not open /dev/ttyACM0, the port is busy or doesn't exist."
     "\n(could not exclusively lock port /dev/ttyACM0: [Errno 11] Resource temporarily "
     "unavailable)", texts.PORT_BUSY),
    ("Writing at 0x007a0000... (100 %)\nA fatal error occurred: Write failed, the written "
     "flash region is empty.", texts.COPY_CHECK_FAILED),
])
def test_known_failures(output, advice):
    result = ToolResult(call("x"), 2, output, 1.0)
    assert advice_for(result) is advice


def test_dispatch_runs_esptool_in_process(capsys):
    assert esptool_runner.dispatch_tool(["esptool", "version"]) == 0
    assert "5.1.0" in capsys.readouterr().out
    assert esptool_runner.dispatch_tool(["idf.py"]) == 2


def test_real_dev_command_runs_esptool_version(tmp_path):
    runner = SubprocessRunner(tmp_path / "w", frozen=False)
    result = runner.run(call("version", timeout_s=60))
    assert result.ok and "5.1.0" in result.output


def test_another_interpreter_runs_the_tools(tmp_path):
    runner = SubprocessRunner(tmp_path / "w", frozen=False, executable="/opt/py312/bin/python3")
    assert runner.command(call("version"))[:3] == ["/opt/py312/bin/python3", "-m", "esptool"]


def test_the_real_tools_pass_the_version_check(tmp_path):
    assert tool_version_problems(SubprocessRunner(tmp_path / "w", frozen=False)) == []


class _Versions(ToolRunner):
    def __init__(self, outputs):
        self.outputs, self.calls = outputs, []

    def run(self, call, *, cancel=None, on_line=None):
        self.calls.append(call)
        output = self.outputs.get(call.tool)
        return ToolResult(call, 0 if output is not None else 1, output or "", 0.1)


@pytest.mark.parametrize("outputs, problem", [
    ({"esptool": "esptool v5.1.0\n5.1.0", "espefuse": "Usage: espefuse [OPTIONS]\n espefuse "
                                                       "v5.1.0 - Utility"}, None),
    # The Mac's own Python 3.9: esptool 4.7.0 refuses every 5.x command line.
    ({"esptool": "esptool.py v4.7.0\n4.7.0", "espefuse": "espefuse.py v4.7.0\nusage: x"},
     "esptool is not version 5.1.0 (it says 'esptool.py v4.7.0')"),
    ({"esptool": "esptool v15.1.0\n15.1.0", "espefuse": "espefuse v5.1.0"}, "esptool is not"),
    ({"esptool": "esptool v5.1.0\n5.1.0"}, "espefuse does not run"),
])
def test_tool_version_check(outputs, problem):
    runner = _Versions(outputs)
    problems = tool_version_problems(runner)
    assert [c.args for c in runner.calls] == [("version",), ("--help",)]
    assert all("--port" not in c.args for c in runner.calls)  # nothing reaches a board
    if problem is None:
        assert problems == []
    else:
        assert problems and problems[0].startswith(problem)


def test_a_failing_listener_does_not_abandon_the_child(runner):
    def broken(line):
        raise RuntimeError("screen bug")
    result = runner.run(call("progress"), on_line=broken)
    assert result.ok and "Hash of data verified." in result.output
