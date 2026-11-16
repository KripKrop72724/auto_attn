"""Records a person can keep or send: the setup record and the help file.

The setup record is a plain text file with what was set and how the board's two
watched starts went: the first start and the restart check. Only when both passed
does it say the board started correctly and kept its settings. It never contains a
password or the comm key. The help file is a ZIP of the app's logs and the boards'
console logs (redacted again on the way out), recent setup records and basic facts
about this computer, for head office.

A board test (core/boardtest.py) is kept as a certificate PDF plus an evidence ZIP
(``save_board_test``): the ZIP is written first and its SHA-256 printed in the PDF, so
anyone can check the PDF belongs to that evidence. The ZIP holds the certificate as
text, the report as JSON, the run's console log, the tool transcripts, both eFuse
summaries and the test images' manifest; never key material (blank key blocks are all
zero and burned ones unreadable; anything else is replaced before it is written).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import texts
from .api import BootOutcome, BootReport, Family, InstallMode, InstallSummary
from .certificate import (FILE_WORDS, CertificateKind, build_certificate, certificate_text,
                          default_kind, write_pdf)
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
HELP_FILE_BOARD_TESTS = 10  # the newest board test records in a help file
BOARD_TEST_PREFIX = "board-test-"
BOARD_TEST_MANIFEST = "manifest.json"
NOT_COPIED = "[not copied]"
_KEY_BLOCK = re.compile(r"^BLOCK_KEY\d+$")
_ZERO_OR_UNREADABLE = re.compile(r"^(?:[0\s]+|[?\s]+)$")


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
                 now: Optional[Callable[[], datetime]] = None,
                 board_test_dir: Optional[Path] = None) -> None:
        self._records = records_dir
        self._logs = logs_dir
        self._app_version = app_version
        self._now = now or datetime.now
        self._board_test_dir = board_test_dir

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
            for record in self._recent_records(10) + self._recent_board_tests(
                    HELP_FILE_BOARD_TESTS):
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

    def _recent_board_tests(self, limit: int) -> List[Path]:
        if not self._records.is_dir():
            return []
        found = [path for pattern in (f"{BOARD_TEST_PREFIX}*.txt", f"{BOARD_TEST_PREFIX}*.json")
                 for path in self._records.glob(pattern)]
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return found[:limit]

    # ------------------------------------------------------------- board tests

    def save_board_test(self, report: object, tester: str,
                        kind: Union[CertificateKind, str, None] = None) -> Tuple[str, str]:
        """Save ``report`` as a certificate PDF and an evidence ZIP; returns their paths.

        ``kind`` is the certificate kind (certificate.CertificateKind or its value); by
        default the one the verdict allows (PASSED, VENDOR_RETURN, else FAULT_REPORT).
        The ZIP is written first; its SHA-256 goes into the PDF. A plain-text copy of the
        certificate is kept next to them for the help file.
        """
        cert_kind = CertificateKind(kind) if kind is not None else default_kind(report)
        return self.save_board_tests(report, tester, [cert_kind])[cert_kind.value]

    def save_board_tests(self, report: object, tester: str,
                         kinds: Sequence[Union[CertificateKind, str]]
                         ) -> Dict[str, Tuple[str, str]]:
        """Several certificates of one report (a vendor claim and a fault report) that
        share ONE evidence ZIP, so the SHA-256 every PDF prints is the ZIP's; returns
        {kind value: (pdf, zip)}. The ZIP holds each certificate's text: certificate.txt
        for the first, certificate-<word>.txt for the others."""
        cert_kinds = [CertificateKind(kind) for kind in kinds]
        if not cert_kinds:
            raise ValueError("no certificate kind")
        moment = self._now()
        if moment.tzinfo is None:
            moment = moment.astimezone()
        self._records.mkdir(parents=True, exist_ok=True)
        mac = str(getattr(report, "mac", "") or "").replace(":", "").lower() or "board"
        base = f"{BOARD_TEST_PREFIX}{_safe(mac)}-{moment.strftime('%Y%m%d-%H%M%S')}-"
        zip_path = self._unused(self._records / f"{base}{FILE_WORDS[cert_kinds[0]]}.zip")
        program = self._test_program_version()

        # The certificates inside the ZIP cannot carry the ZIP's own hash.
        inner_texts: List[Tuple[str, str]] = []
        for index, cert_kind in enumerate(cert_kinds):
            inner = build_certificate(report, tester=tester, app_version=self._app_version,
                                      now=moment, kind=cert_kind,
                                      test_program_version=program)
            name = ("certificate.txt" if index == 0
                    else f"certificate-{FILE_WORDS[cert_kind]}.txt")
            inner_texts.append((name, certificate_text(inner)))
        self._write_evidence(zip_path, report, inner_texts, moment)
        digest = _sha256(zip_path)
        saved: Dict[str, Tuple[str, str]] = {}
        for index, cert_kind in enumerate(cert_kinds):
            stem = (zip_path.stem if index == 0 else
                    self._unused(self._records / f"{base}{FILE_WORDS[cert_kind]}.zip").stem)
            pdf_path = self._records / f"{stem}.pdf"
            text_path = self._records / f"{stem}.txt"
            data = build_certificate(report, tester=tester, app_version=self._app_version,
                                     now=moment, kind=cert_kind, evidence_name=zip_path.name,
                                     evidence_sha256=digest, test_program_version=program)
            try:
                write_pdf(data, pdf_path)
            except Exception:
                log.exception("board test certificate %s not written", pdf_path.name)
                raise
            try:
                text_path.write_text(certificate_text(data), encoding="utf-8")
            except OSError as error:  # the PDF and the ZIP are what count
                log.warning("board test text %s not kept: %s", text_path.name, error)
            log.info("saved board test %s (%s, evidence %s sha256 %s)", pdf_path.name,
                     cert_kind.value, zip_path.name, digest)
            saved[cert_kind.value] = (str(pdf_path), str(zip_path))
        return saved

    def _unused(self, path: Path) -> Path:
        """``path``, or ``-2``, ``-3``... when a record of the same second exists."""
        candidate, number = path, 1
        while candidate.exists() or candidate.with_suffix(".pdf").exists():
            number += 1
            candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        return candidate

    def _manifest(self) -> Optional[Dict[str, object]]:
        if self._board_test_dir is None:
            return None
        try:
            value = json.loads((self._board_test_dir / BOARD_TEST_MANIFEST).read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _test_program_version(self) -> Optional[str]:
        manifest = self._manifest()
        version = manifest.get("version") if manifest else None
        return str(version) if version else None

    def _write_evidence(self, path: Path, report: object,
                        certificates: Sequence[Tuple[str, str]], moment: datetime) -> None:
        stamp = moment.timetuple()[:6]
        files: List[Tuple[str, str]] = [
            # The certificate redacted what came from the board (proof lines, names); run
            # over all of it, redact would also blank our own "passed: ..." sentences.
            *certificates,
            ("result.json", json.dumps(report_json(report), indent=2, sort_keys=True)),
            ("console.log", self._console_text(report)),
            ("tools.log", redact("\n\n".join(str(t) for t in
                                              getattr(report, "tool_log", ()) or ()))),
            ("efuse-before.json", efuse_json(getattr(report, "efuse_before", {}) or {})),
            ("efuse-after.json", efuse_json(getattr(report, "efuse_after", {}) or {})),
            ("test-images.json", json.dumps(self._images_json(report), indent=2,
                                            sort_keys=True)),
        ]
        part = path.with_name(path.name + ".part")
        try:
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, text in files:
                    info = zipfile.ZipInfo(name, date_time=stamp)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    archive.writestr(info, text + ("" if text.endswith("\n") else "\n"))
            os.replace(part, path)
        finally:
            if part.exists():
                part.unlink()

    def _console_text(self, report: object) -> str:
        source = getattr(report, "console_log", None)
        if not source:
            return "No console log was kept for this run."
        try:
            return redact(Path(source).read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            return f"The console log {Path(source).name} could not be read: {error}"

    def _images_json(self, report: object) -> Dict[str, object]:
        manifest = self._manifest()
        used = dict(getattr(report, "image_sha256", {}) or {})
        if manifest is not None:
            return {"manifest": manifest, "used": used}
        return {"manifest": None, "used": used}

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


# ------------------------------------------------------------------- board test JSON

def _plain(value: object) -> object:
    """A JSON-ready copy of report objects: dataclasses, enums, tuples, paths."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def safe_efuses(summary: Dict[str, object]) -> Dict[str, object]:
    """``summary`` with every key block value that is not all zero or unreadable replaced.

    A blank board's key blocks read as zeros and a burned key is read-protected ("??"),
    so normally nothing changes; a readable, used key block is never copied out.
    """
    clean: Dict[str, object] = {}
    for name, field in summary.items():
        if _KEY_BLOCK.match(str(name)) and isinstance(field, dict):
            value = field.get("value")
            if value is not None and not _ZERO_OR_UNREADABLE.match(str(value)):
                field = dict(field, value=NOT_COPIED)
        clean[str(name)] = _plain(field)
    return clean


def efuse_json(summary: Dict[str, object]) -> str:
    """The eFuse summary as JSON, key blocks made safe, redacted when that leaves it whole.

    logging_setup.redact does not touch espefuse's field names today; should it ever
    blank one, the redacted text would no longer be the same JSON and the made-safe
    copy is written instead (it holds no key values, see safe_efuses).
    """
    clean = safe_efuses(summary)
    text = json.dumps(clean, indent=2, sort_keys=True)
    redacted = redact(text)
    if redacted == text:
        return text
    log.warning("redaction would change the eFuse summary; writing the made-safe copy")
    return text


def _redact_leaves(value: object) -> object:
    """redact() on every string in a JSON-ready value; names and numbers stay as they are.

    Redacting the JSON text instead would blank fields such as "passed": false.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_leaves(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_leaves(v) for v in value]
    return value


def report_json(report: object) -> Dict[str, object]:
    """The report for result.json: every text redacted, eFuse key blocks made safe."""
    data = _redact_leaves(_plain(report))
    if not isinstance(data, dict):
        data = {"report": data}
    for name in ("efuse_before", "efuse_after"):
        value = getattr(report, name, None)
        if isinstance(value, dict):
            data[name] = safe_efuses(value)
    for name in ("passed", "hardware_fault"):
        if hasattr(report, name):
            data[name] = bool(getattr(report, name))
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()
