"""Runs the bundled esptool and espefuse (5.1.0) as child processes.

Frozen app: ``<exe> --tool esptool|espefuse ...`` (``dispatch_tool`` below is what
``__main__`` calls for that). Development: ``python -m esptool|espefuse ...``.

Each run gets a private working directory and a pinned empty esptool.cfg, and every
ESPTOOL_* variable is removed from its environment: a user's config file or
variable could otherwise change reset sequences or flash settings behind our back.
Output is decoded, split on carriage returns as well as newlines (esptool redraws
progress with ``\\r``), redacted, streamed to the caller and kept for parsing.
"""
from __future__ import annotations

import codecs
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import texts
from .api import Advice, CancelToken
from .logging_setup import get_logger, redact

ESPTOOL = "esptool"
ESPEFUSE = "espefuse"
TOOLS = (ESPTOOL, ESPEFUSE)
# Every command line in flashplan.py is written for this version; esptool 4 spells
# them differently ("default_reset", "get_security_info") and refuses them.
TOOL_VERSION = "5.1.0"
_TOOL_VERSION = re.compile(r"(?<![\d.])" + re.escape(TOOL_VERSION) + r"(?![\d.])")

LineFn = Callable[[str], None]
log = get_logger("tools")

PROGRESS_LINE = re.compile(
    r"(?:Writing|Reading) at 0x([0-9a-fA-F]+)\s*\[[^\]]*\]\s*([0-9.]+)%")
_MAX_KEPT_CHARS = 400_000
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: Tuple[str, ...]
    label: str
    timeout_s: float = 90.0
    # A call that prints nothing for this long is treated as hung. Flash writes print
    # progress every block, connects print dots, so a long silence means trouble.
    idle_timeout_s: float = 60.0


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    returncode: Optional[int]
    output: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    start_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (self.returncode == 0 and not self.timed_out and not self.cancelled
                and self.start_error is None)


class ToolRunner:
    """Interface the engine uses; tests substitute a scripted runner."""

    def run(self, call: ToolCall, *, cancel: Optional[CancelToken] = None,
            on_line: Optional[LineFn] = None) -> ToolResult:
        raise NotImplementedError


def tool_command(tool: str, frozen: Optional[bool] = None,
                 executable: Optional[str] = None) -> List[str]:
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool!r}")
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = executable or sys.executable
    if frozen:
        return [executable, "--tool", tool]
    return [executable, "-m", tool]


def child_environment(config_file: Path, base: Optional[Dict[str, str]] = None
                      ) -> Dict[str, str]:
    source = dict(os.environ if base is None else base)
    env = {key: value for key, value in source.items()
           if not key.upper().startswith("ESPTOOL_")}
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        # Keeps esptool's output plain: no colours, no cursor movement.
        "NO_COLOR": "1",
        "TERM": "dumb",
        "ESPTOOL_CFGFILE": str(config_file),
    })
    return env


class SubprocessRunner(ToolRunner):
    def __init__(self, work_dir: Path, *, frozen: Optional[bool] = None,
                 executable: Optional[str] = None) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.work_dir / "esptool.cfg"
        self.config_file.write_text("[esptool]\n", encoding="utf-8")
        self._frozen = frozen
        self._executable = executable

    def command(self, call: ToolCall) -> List[str]:
        return tool_command(call.tool, self._frozen, self._executable) + list(call.args)

    def run(self, call: ToolCall, *, cancel: Optional[CancelToken] = None,
            on_line: Optional[LineFn] = None) -> ToolResult:
        command = self.command(call)
        log.info("run %s: %s", call.label, " ".join(call.args))
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=str(self.work_dir),
                env=child_environment(self.config_file),
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as error:
            log.error("could not start %s: %s", call.tool, error)
            return ToolResult(call, None, "", 0.0, start_error=str(error))

        lines: "queue.Queue[Optional[str]]" = queue.Queue()
        reader = threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True)
        reader.start()
        kept: List[str] = []
        kept_chars = 0
        last_output = started
        timed_out = cancelled = False
        finished = False
        while not finished:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                line = ""
            else:
                if line is None:
                    finished = True
                    line = ""
            now = time.monotonic()
            if line:
                last_output = now
                clean = redact(line)
                if not PROGRESS_LINE.search(clean) and kept_chars < _MAX_KEPT_CHARS:
                    kept.append(clean)
                    kept_chars += len(clean)
                if on_line is not None:
                    # A listener's bug must never leave a write running unattended.
                    try:
                        on_line(clean)
                    except Exception as error:
                        log.warning("line listener failed: %s", error)
                        on_line = None
            if finished:
                break
            if cancel is not None and cancel.cancelled:
                cancelled = True
            elif now - started > call.timeout_s or now - last_output > call.idle_timeout_s:
                timed_out = True
            if cancelled or timed_out:
                _kill(process)
                break
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill(process)
            returncode = process.wait()
        reader.join(timeout=5)
        while True:
            try:
                tail = lines.get_nowait()
            except queue.Empty:
                break
            if tail:
                clean = redact(tail)
                if not PROGRESS_LINE.search(clean):
                    kept.append(clean)
        output = "\n".join(kept)
        duration = time.monotonic() - started
        result = ToolResult(call, None if (timed_out or cancelled) else returncode, output,
                            duration, timed_out=timed_out, cancelled=cancelled)
        log.info("%s finished in %.1f s (exit %s%s%s)\n%s", call.label, duration, returncode,
                 ", timed out" if timed_out else "", ", cancelled" if cancelled else "",
                 output[-20000:])
        return result


def _pump(stream, lines: "queue.Queue[Optional[str]]") -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            parts = re.split(r"\r\n|\r|\n", pending)
            pending = parts.pop()
            for part in parts:
                if part.strip():
                    lines.put(part.rstrip())
        pending += decoder.decode(b"", final=True)
        if pending.strip():
            lines.put(pending.rstrip())
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _kill(process: "subprocess.Popen[bytes]") -> None:
    try:
        process.kill()
    except OSError:
        pass


# -------------------------------------------------------------- failure codes

# Checked in order; the first match wins.
#
# A port that is already open is never "busy": pyserial words a board that drops off
# USB in the middle of a call as PermissionError on Windows (WinError 5, 22 and 31 all
# map to EACCES: "WriteFile failed (PermissionError(13, ...))"), and on macOS/Linux
# as "device reports readiness to read but returned no data (device disconnected or
# multiple access on port?)". So a dropped connection is recognised first, and the
# busy-port words count only where opening the port failed. esptool prints "Could not
# open X, the port is busy or doesn't exist." for both a busy and a missing port; the
# operating system's reason that follows tells them apart. "Write failed, the written
# flash region is empty." is a check failure, so it comes before "write failed".
_FAILURES: Sequence[Tuple[re.Pattern, Advice]] = (
    (re.compile(r"MD5 of file does not match data in flash|Verification failed|"
                r"written flash region is empty", re.I), texts.COPY_CHECK_FAILED),
    (re.compile(r"(?:WriteFile|ReadFile|ClearCommError|GetOverlappedResult) failed|"
                r"device reports readiness to read but returned no data|"
                r"Serial data stream stopped|Invalid head of packet|Packet content transfer "
                r"stopped|Lost connection|write failed|serial exception|SerialException|"
                r"The chip stopped responding", re.I), texts.CONNECTION_LOST),
    (re.compile(r"could not open port[^\n]*(?:Access is denied|PermissionError|"
                r"Permission denied|Resource busy)|could not exclusively lock|"
                r"Check if the port is not used", re.I), texts.PORT_BUSY),
    (re.compile(r"FileNotFoundError|No such file or directory|cannot find the file specified|"
                r"device not configured|Could not connect to an Espressif device", re.I),
     texts.BOARD_DISCONNECTED),
    (re.compile(r"the port is busy or doesn't exist", re.I), texts.PORT_BUSY),
    (re.compile(r"This chip is [\w-]+, not ESP32-S3", re.I), texts.WRONG_CHIP),
    (re.compile(r"Secure Download Mode is enabled|not available in Secure Download Mode|"
                r"is not supported in Secure Download Mode|Secure Boot detected, writing to "
                r"flash regions < 0x8000|Active security features detected", re.I),
     texts.ALREADY_SET_UP),
    (re.compile(r"Download mode successfully detected, but getting no sync reply", re.I),
     texts.CABLE_PROBLEM),
    (re.compile(r"No serial data received|Wrong boot mode detected|Failed to connect to "
                r"ESP32-S3", re.I), texts.AUTO_DOWNLOAD_FAILED),
)


def advice_for(result: ToolResult) -> Advice:
    """Plain words for a failed call."""
    if result.start_error is not None:
        return texts.TOOL_MISSING
    if result.cancelled:
        return texts.CANCELLED
    if result.timed_out:
        return texts.TOOL_TIMEOUT
    for pattern, advice in _FAILURES:
        if pattern.search(result.output):
            return advice
    if re.search(r"No module named|ModuleNotFoundError|ImportError", result.output):
        return texts.TOOL_MISSING
    return texts.TOOL_FAILED


def tool_version_problems(runner: ToolRunner) -> List[str]:
    """Why the tools cannot be used: empty when esptool and espefuse are both 5.1.0.

    Neither call opens a port: "version" and "--help" only print.
    """
    problems: List[str] = []
    for tool, args in ((ESPTOOL, ("version",)), (ESPEFUSE, ("--help",))):
        result = runner.run(ToolCall(tool, args, f"{tool} version check", timeout_s=60.0,
                                     idle_timeout_s=60.0))
        if not result.ok:
            problems.append(f"{tool} does not run (exit {result.returncode}): "
                            f"{result.start_error or result.output[-200:]!r}")
        elif not _TOOL_VERSION.search(result.output):
            said = next((line.strip() for line in result.output.splitlines()
                         if re.search(r"\bv?\d+\.\d+", line)), result.output.strip()[:80])
            problems.append(f"{tool} is not version {TOOL_VERSION} (it says {said!r})")
    return problems


def failure_cause(result: ToolResult) -> str:
    """One technical line for the log: the tool's own error, never secrets."""
    if result.start_error:
        return f"{result.call.label}: could not start ({result.start_error})"
    if result.timed_out:
        return f"{result.call.label}: timed out after {result.duration_s:.0f} s"
    if result.cancelled:
        return f"{result.call.label}: cancelled"
    lines = [line for line in result.output.splitlines() if line.strip()]
    fatal = [line for line in lines if "fatal error" in line.lower() or "error" in line.lower()]
    last = (fatal or lines or ["no output"])[-1]
    return f"{result.call.label}: exit {result.returncode}: {last.strip()[:300]}"


# ------------------------------------------------------------ frozen dispatch

def dispatch_tool(argv: Sequence[str]) -> int:
    """Entry point for ``<exe> --tool esptool|espefuse args...`` in the frozen app.

    ``__main__`` must call this before importing Qt. Line buffering makes progress
    reach the parent as it happens.
    """
    if not argv or argv[0] not in TOOLS:
        print("usage: --tool esptool|espefuse [args...]", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    name = argv[0]
    sys.argv = [name] + list(argv[1:])
    try:
        if name == ESPTOOL:
            import esptool
            esptool._main()
        else:
            import espefuse
            espefuse._main()
    except SystemExit as stop:
        code = stop.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0
