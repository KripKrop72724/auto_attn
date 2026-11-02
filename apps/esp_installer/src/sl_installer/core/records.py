"""Records a person can keep or send: the setup record and the help file.

The setup record is a plain text file with what was set and how the board's two
watched starts went: the first start and the restart check. Only when both passed
does it say the board started correctly and kept its settings. It never contains a
password or the comm key. The help file is a ZIP of the app's logs and the boards'
console logs (redacted again on the way out), recent setup records and basic facts
about this computer, for head office.
"""
from __future__ import annotations

import platform
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import texts
from .api import BootOutcome, BootReport, Family, InstallMode, InstallSummary
from .console import console_log_files
from .logging_setup import get_logger, log_files, redact
from .naming import hotspot_name, public_name_preview

log = get_logger("records")

_MODE_WORDS = {
    InstallMode.NEW_BOARD: "New board set up",
    InstallMode.RESUME: "Unfinished setup finished",
    InstallMode.CHANGE_SETTINGS: "Settings changed",
}
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
HELP_FILE_CONSOLE_LOGS = 10  # the newest boards' console logs in a help file


def _safe(value: str) -> str:
    return _SAFE.sub("-", value).strip("-")[:40] or "board"


def start_result(boot: Optional[BootReport], restart: Optional[BootReport]) -> str:
    """The record's verdict on the watched starts: a success only after the restart check."""
    if boot is None:
        return "Not watched."
    if boot.outcome is not BootOutcome.OK:
        return "The board did not pass its first start."
    if restart is not None and restart.outcome is BootOutcome.OK:
        return texts.SETTINGS_KEPT
    if restart is not None and restart.outcome is BootOutcome.SETTINGS_LOST:
        return "The board lost its settings when it restarted. Do not install it at a site."
    return ("Not finished: the app did not see that the board keeps its settings after a "
            "restart.")


def _start_lines(label: str, report: Optional[BootReport]) -> List[str]:
    head = f"  {label + ':':<18}"
    if report is None:
        return [f"{head}Not watched."]
    # A start that passed is only "Passed": the words of success belong to the Result line,
    # and only once both starts passed (start_result).
    if report.outcome is BootOutcome.OK:
        verdict = "Passed"
    else:
        verdict = report.advice.title if report.advice else report.outcome.value
    lines = [f"{head}{verdict}"]
    seen = ", ".join(part for part in (
        f"zone {report.zone_id_seen}" if report.zone_id_seen else "",
        f"device {report.device_id_seen}" if report.device_id_seen else "",
        f"version {report.app_version_seen}" if report.app_version_seen else "") if part)
    if seen:
        lines.append(f"  {'Seen:':<18}{seen}")
    return lines


class Records:
    """RecordsService."""

    def __init__(self, records_dir: Path, logs_dir: Path, app_version: str,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self._records = records_dir
        self._logs = logs_dir
        self._app_version = app_version
        self._now = now or datetime.now

    def install_record_text(self, summary: InstallSummary, boot: Optional[BootReport],
                            restart: Optional[BootReport] = None) -> str:
        config = summary.config
        preview = public_name_preview(config.zone_name)
        lines = [
            "State Life Device Installer - setup record",
            "=" * 42,
            f"Saved:              {self._now().strftime('%Y-%m-%d %H:%M')}",
            f"App version:        {self._app_version}",
            f"Reference:          {summary.request_id}",
            "",
            "Board",
            f"  Board ID:         {summary.mac}",
            f"  What was done:    {_MODE_WORDS[summary.mode]}",
            f"  Started:          {summary.started_at}",
            f"  Finished:         {summary.finished_at}",
            "",
            "Place",
            f"  ZONE ID:          {config.zone_id}",
            f"  ZONE NAME:        {config.zone_name}",
            f"  Public name:      {preview.name} ({preview.group})",
            f"  Device number:    {config.device_id}",
            "",
            "Attendance machine",
            f"  Type:             {texts.family_name(config.family)}",
        ]
        if config.family is Family.ZKT and config.zkt is not None:
            lines += [f"  Port:             {config.zkt.port}",
                      f"  Machine address:  {config.zkt.preferred_ip}"]
        elif config.hik is not None:
            hik = config.hik
            lines += [f"  Machine address:  {hik.host}",
                      f"  Port:             {hik.port}",
                      f"  Connection:       {'HTTPS' if hik.transport == 'https' else 'HTTP digest'}",
                      f"  Username:         {hik.username}",
                      f"  Model profile:    {hik.profile}",
                      f"  Serial number:    {hik.expected_serial}",
                      f"  Record start:     {hik.source_epoch}"]
        if summary.mode is InstallMode.CHANGE_SETTINGS:
            # Only the settings were written; any bundle named is not on the board.
            software = "Not changed (settings only)"
        elif summary.bundle.bundle_id:
            software = f"{summary.bundle.label} ({summary.bundle.bundle_id})"
        else:
            software = summary.bundle.label
        lines += [
            "",
            "Firmware",
            f"  Firmware:         {software}",
            "",
            "Wi-Fi",
            f"  Wi-Fi name:       {config.wifi_ssid}",
            f"  Board's Wi-Fi:    {hotspot_name(summary.mac) or 'unknown'}",
            f"  Setup page:       {texts.PORTAL_ADDRESS}",
            "",
            "How the board started",
            f"  Result:           {start_result(boot, restart)}",
        ]
        lines += _start_lines("First start", boot)
        lines += _start_lines("After a restart", restart if boot is not None else None)
        lines += ["", "Passwords and the comm key are not saved in this file.", ""]
        return redact("\n".join(lines), extra_secrets=_secrets(summary))

    def save_install_record(self, summary: InstallSummary, boot: BootReport,
                            restart: Optional[BootReport] = None) -> str:
        self._records.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        name = (f"setup-{_safe(summary.config.zone_id)}-{_safe(summary.config.device_id)}-"
                f"{summary.mac.replace(':', '')}-{stamp}.txt")
        path = self._records / name
        path.write_text(self.install_record_text(summary, boot, restart), encoding="utf-8")
        log.info("saved setup record %s (%s)", path.name, start_result(boot, restart))
        return str(path)

    def save_help_file(self) -> str:
        self._records.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        path = self._records / f"help-file-{stamp}.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("about-this-computer.txt", self._system_text())
            # The app's own log, then what the newest boards printed while they started.
            logs = log_files(self._logs) + console_log_files(self._logs)[:HELP_FILE_CONSOLE_LOGS]
            for log_path in logs:
                try:
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                archive.writestr(f"logs/{log_path.name}", redact(text))
            for record in self._recent_records(10):
                try:
                    archive.writestr(f"records/{record.name}",
                                     redact(record.read_text(encoding="utf-8")))
                except OSError:
                    continue
        log.info("saved help file %s", path.name)
        return str(path)

    def _recent_records(self, limit: int) -> List[Path]:
        if not self._records.is_dir():
            return []
        found = sorted(self._records.glob("setup-*.txt"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        return found[:limit]

    def _system_text(self) -> str:
        return "\n".join([
            f"Saved: {self._now().isoformat(timespec='seconds')}",
            f"App version: {self._app_version}",
            f"Windows / system: {platform.platform()}",
            f"Python: {sys.version.split()[0]}",
            f"Frozen app: {bool(getattr(sys, 'frozen', False))}",
            "",
        ])


def _secrets(summary: InstallSummary) -> List[str]:
    config = summary.config
    values = [config.wifi_password]
    if config.hik is not None:
        values.append(config.hik.password)
    return [value for value in values if value]
