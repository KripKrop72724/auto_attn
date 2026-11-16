"""Board test records (records.Records.save_board_test): certificate PDF + evidence ZIP."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from sl_installer.core import records as records_module
from sl_installer.core.certificate import CertificateKind
from sl_installer.core.records import Records, efuse_json, report_json, safe_efuses

from .test_certificate import NOW, efuses, make_report

ZIP_NAMES = ["certificate.txt", "result.json", "console.log", "tools.log", "efuse-before.json",
             "efuse-after.json", "test-images.json"]


@pytest.fixture(autouse=True)
def offscreen(qapp):
    """The session's QApplication (see test_certificate.offscreen)."""
    return qapp


@pytest.fixture
def images(tmp_path):
    folder = tmp_path / "boardtest"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({
        "version": "1.0.0", "idf": "v5.5.3",
        "files": [{"name": "board-test.bin", "offset": "0x10000", "size": 1,
                   "sha256": "3" * 64}]}), encoding="utf-8")
    return folder


def recorder(tmp_path, images=None):
    return Records(tmp_path / "records", tmp_path / "logs", "1.2.0", now=lambda: NOW,
                   board_test_dir=images)


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.parametrize("scenario, word", [("passed", "passed"), ("psram_errors", "fault"),
                                            ("locked_psram", "fault"),
                                            ("settings_missing", "report")])
def test_save_board_test_writes_zip_then_pdf(tmp_path, images, scenario, word):
    pdf, zip_path = recorder(tmp_path, images).save_board_test(
        make_report(tmp_path, scenario), "Asad Ullah")
    mac = make_report(tmp_path, scenario).mac.replace(":", "")
    assert Path(pdf).name == f"board-test-{mac}-20261116-140322-{word}.pdf"
    assert Path(zip_path).name == f"board-test-{mac}-20261116-140322-{word}.zip"
    assert Path(pdf).read_bytes().startswith(b"%PDF")
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ZIP_NAMES
        inner = archive.read("certificate.txt").decode()
        result = json.loads(archive.read("result.json"))
        manifest = json.loads(archive.read("test-images.json"))
    assert "This text is part of the evidence file" in inner
    if scenario == "passed":
        assert "Every test passed: the extra memory" in inner  # not blanked as a "pass"
        assert result["passed"] is True
    assert result["mac"] == make_report(tmp_path, scenario).mac
    assert result["verdict"] == make_report(tmp_path, scenario).verdict.value
    assert result["board"]["evidence"]["usb_mac"] == result["mac"]
    assert manifest["manifest"]["version"] == "1.0.0"
    # The text copy next to the PDF names the ZIP and its hash, as the PDF does.
    text = Path(pdf).with_suffix(".txt").read_text(encoding="utf-8")
    assert Path(zip_path).name in text
    assert sha256(zip_path) in text
    assert "Test program:" in text and not list((tmp_path / "records").glob("*.part"))


def test_vendor_claim_and_fault_report_for_the_same_board(tmp_path):
    rec = recorder(tmp_path)
    report = make_report(tmp_path, "locked_psram")
    claim, _zip1 = rec.save_board_test(report, "Asad Ullah", CertificateKind.VENDOR_RETURN)
    fault, _zip2 = rec.save_board_test(report, "Asad Ullah", "fault_report")
    assert claim.endswith("-fault.pdf") and fault.endswith("-report.pdf")
    assert "The keys do not cause it." in Path(claim).with_suffix(".txt").read_text(
        encoding="utf-8")
    # A second record of the same kind in the same second gets its own name.
    again, again_zip = rec.save_board_test(report, "Asad Ullah", CertificateKind.VENDOR_RETURN)
    assert again.endswith("-fault-2.pdf") and again_zip.endswith("-fault-2.zip")


def test_kind_that_does_not_fit_is_refused(tmp_path):
    with pytest.raises(ValueError):
        recorder(tmp_path).save_board_test(make_report(tmp_path, "psram_errors"), "A", "passed")


def test_zip_holds_console_and_tool_logs_redacted(tmp_path):
    report = make_report(tmp_path, "passed")
    with open(report.console_log, "a", encoding="utf-8") as handle:
        handle.write("    9.9  wifi_password=hunter22 seen\n")
    # A low-entropy placeholder: CI's gitleaks scan must not take it for a real key.
    report = replace(report, tool_log=("espefuse summary\nTOKEN=redactme0000",))
    _pdf, zip_path = recorder(tmp_path).save_board_test(report, "Asad Ullah")
    with zipfile.ZipFile(zip_path) as archive:
        console = archive.read("console.log").decode()
        tools = archive.read("tools.log").decode()
        images_json = json.loads(archive.read("test-images.json"))
    assert "SLTEST RESULT stage=wifi" in console
    assert "hunter22" not in console and "redactme0000" not in tools
    assert "espefuse summary" in tools
    # Without a manifest folder the images actually used are still listed.
    assert images_json["manifest"] is None
    assert images_json["used"]["board-test.bin"] == "3" * 64


def test_efuse_files_keep_field_names_and_never_carry_key_values(tmp_path):
    before = efuses("blank")
    after = efuses("key-burned")
    assert efuse_json(before) == json.dumps(before, indent=2, sort_keys=True)
    # The redactor leaves espefuse's names (BLOCK_KEY0, KEY_PURPOSE_0...) alone.
    assert json.loads(efuse_json(after)) == after
    used = dict(before)
    used["BLOCK_KEY3"] = dict(before["BLOCK_KEY3"], value="12 " * 31 + "12")
    clean = safe_efuses(used)
    assert clean["BLOCK_KEY3"]["value"] == records_module.NOT_COPIED
    assert clean["BLOCK_KEY0"] == before["BLOCK_KEY0"]
    report = replace(make_report(tmp_path, "passed"), efuse_before=used, efuse_after=used)
    _pdf, zip_path = recorder(tmp_path).save_board_test(report, "Asad Ullah",
                                                        CertificateKind.FAULT_REPORT)
    with zipfile.ZipFile(zip_path) as archive:
        everything = b"".join(archive.read(name) for name in archive.namelist()).decode()
    assert "12 12 12" not in everything
    assert '"BLOCK_KEY0"' in everything and '"KEY_PURPOSE_0"' in everything


def test_key_blocks_in_saved_files_are_zero_or_unreadable(tmp_path):
    report = replace(make_report(tmp_path, "passed"), efuse_after=efuses("key-burned"),
                     efuse_before=efuses("blank"), efuses_unchanged=False)
    _pdf, zip_path = recorder(tmp_path).save_board_test(report, "Asad Ullah",
                                                        CertificateKind.FAULT_REPORT)
    with zipfile.ZipFile(zip_path) as archive:
        for name in ("efuse-before.json", "efuse-after.json"):
            data = json.loads(archive.read(name))
            for field, value in data.items():
                if re.fullmatch(r"BLOCK_KEY\d", field):
                    assert re.fullmatch(r"[0\s]+|[?\s]+", str(value["value"])), (name, field)


def test_result_json_is_plain_json(tmp_path):
    data = report_json(make_report(tmp_path, "settings_missing"))
    text = json.dumps(data)
    assert data["recovery"] == "FIX_SETTINGS"
    assert data["advice"]["code"] == "SETTINGS_MISSING"
    assert data["board"]["security"] == "secured_state_life"
    assert data["passed"] is False and data["hardware_fault"] is False
    assert "Click Fix the settings" in text


def test_evidence_zip_is_reproducible(tmp_path):
    report = make_report(tmp_path, "psram_errors")
    one = tmp_path / "one"
    two = tmp_path / "two"
    _p1, z1 = Records(one, tmp_path / "logs", "1.2.0", now=lambda: NOW).save_board_test(
        report, "Asad Ullah")
    _p2, z2 = Records(two, tmp_path / "logs", "1.2.0", now=lambda: NOW).save_board_test(
        report, "Asad Ullah")
    assert sha256(z1) == sha256(z2)


def test_help_file_includes_recent_board_tests(tmp_path):
    rec = recorder(tmp_path)
    rec.save_board_test(make_report(tmp_path, "psram_errors"), "Asad Ullah")
    help_file = rec.save_help_file()
    with zipfile.ZipFile(help_file) as archive:
        names = archive.namelist()
    assert "records/board-test-a4e5e8123456-20261116-140322-fault.txt" in names
    assert not any(name.endswith((".pdf", ".zip")) for name in names)


def test_vendor_claim_and_fault_report_share_one_evidence_zip(tmp_path):
    rec = recorder(tmp_path)
    report = make_report(tmp_path, "locked_psram")
    saved = rec.save_board_tests(report, "Asad Ullah", ["vendor_return", "fault_report"])
    (claim, claim_zip), (fault, fault_zip) = saved["vendor_return"], saved["fault_report"]
    assert claim_zip == fault_zip and claim.endswith("-fault.pdf")
    assert fault.endswith("-report.pdf")
    digest = sha256(claim_zip)
    for pdf in (claim, fault):
        text = Path(pdf).with_suffix(".txt").read_text(encoding="utf-8")
        assert digest in text and Path(claim_zip).name in text
    with zipfile.ZipFile(claim_zip) as archive:
        names = archive.namelist()
        assert "certificate.txt" in names and "certificate-report.txt" in names
    assert len(list((tmp_path / "records").glob("*.zip"))) == 1
