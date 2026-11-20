"""Activity words: every device log code has plain words; kinds agree between SQL and Python."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import column, select, table

from zk_add import activity_words
from zk_add.activity_words import LEVELS, WORDS, display_kind, fill, kind_clause, plain_log, topic_clause

ROOT = Path(__file__).resolve().parents[2]
LOG_HELPERS = re.compile(r"\b(add_connector_log_acknowledged|add_connector_log|logf_line|say|queue_line)\s*\(")
BACKEND_CODES = {"DEVICE_MESSAGE_REJECTED", "ADD_MESSAGE_PROCESSING_FAILED", "SOURCE_TAIL_COVERAGE_INACTIVE",
                 "SOURCE_TAIL_EPOCH_STALE", "BLOCKED_IDENTITY_RECOVERY_DEFERRED"}
# Words people who do not know the system should never see (extends tests/unit/test_public_status.py).
BANNED = {"add", "esp", "zkt", "ords", "oracle", "terminal", "connector", "hikvision", "reconcile", "reconciliation",
          "custody", "cursor", "epoch", "anchor", "outbox", "payload", "snapshot", "ordinal", "chunk", "journal",
          "nvs", "spiffs", "psram", "ota", "uid", "http", "telemetry", "heartbeat", "firmware", "lease",
          "quarantine", "punch", "boot", "errno"}


def _call_args(text: str, start: int) -> str:
    depth, index = 1, start
    while index < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        index += 1
    return text[start:index - 1]


def firmware_codes() -> set[str]:
    codes: set[str] = set()
    for path in (ROOT / "firmware/zone_lite/main").glob("*.c"):
        text = path.read_text(errors="replace")
        for match in LOG_HELPERS.finditer(text):
            codes.update(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', _call_args(text, match.end())))
        for array in re.finditer(r"codes\[\]\s*=\s*\{([^}]*)\}", text):
            codes.update(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', array.group(1)))
    return codes - set(LEVELS)


def _row(code, level="INFO", message="", context=None, subsystem="zkt"):
    return SimpleNamespace(code=code, level=level, message=message, context=context or {}, subsystem=subsystem)


def _sentences(entry: dict) -> list[str]:
    texts = [entry["sentence"], entry.get("group_sentence"), entry.get("action")]
    texts += list(entry.get("by_level", {}).values()) + list(entry.get("action_by_level", {}).values())
    return [text for text in texts if text]


def test_every_firmware_and_backend_log_code_has_words():
    codes = firmware_codes()
    assert len(codes) >= 150, "the firmware scan found too few codes; the pattern broke"
    missing = sorted((codes | BACKEND_CODES) - set(WORDS))
    assert missing == [], f"add plain words in activity_words_data.py for: {missing}"


@pytest.mark.parametrize("code", sorted(WORDS))
def test_words_are_plain(code):
    entry = WORDS[code]
    assert entry["group"] in activity_words.TOPICS
    assert entry["kind"] in {"routine", "problem"}
    assert entry["act"] in {"no", "if_repeats", "yes", "list"}
    for text in _sentences(entry):
        filled = fill(text, {})
        words = set(re.findall(r"[a-z]+", filled.lower()))
        assert not words & BANNED, (code, filled)
        assert not re.search(r"\b[A-Z]+_[A-Z_]+\b", filled), (code, filled)
        assert "=" not in filled and not re.search(r"[0-9a-f]{8,}", filled), (code, filled)
        assert "{" not in filled and "}" not in filled, (code, filled)


@pytest.mark.parametrize(("level", "kind", "title"), [
    ("DEBUG", "routine", "Other activity from the link box."),
    ("INFO", "routine", "Other activity from the link box."),
    ("WARN", "retried", "The link box reported a small issue and is handling it."),
    ("ERROR", "problem", "The link box reported a problem."),
    ("CRITICAL", "problem", "The link box reported a serious problem."),
])
def test_unknown_codes_fall_back_by_level(level, kind, title):
    words = plain_log(_row("SOMETHING_NEW_FROM_FIRMWARE_9", level, "raw text CNIC 3520212345671", subsystem="ords"))
    assert (words["kind"], words["title"], words["topic"]) == (kind, title, "hr_system")
    assert words["serious"] is (level == "CRITICAL")
    assert "3520212345671" not in str(words)


def test_live_samples_read_in_plain_words():
    assert plain_log(_row("ZKT_CONNECTION_LOST", "WARN", "ZKT session ended before the stability window"))[
        "title"] == "The connection dropped right after connecting. Trying again."
    assert plain_log(_row("ZKT_SERIAL_MISMATCH", "CRITICAL"))["serious"] is True
    hik = plain_log(_row("HIK_POLL_STATUS", "WARN", "Poll retrying; reason=authentication_rejected; failures=2"))
    assert hik["title"] == "Couldn't check the machine: the machine refused the password. Trying again."
    assert hik["kind"] == "retried"
    ok = plain_log(_row("HIK_POLL_STATUS", "INFO", "Poll OK; reason=ok; queued=4"))
    assert (ok["kind"], ok["title"]) == ("routine", "Checked the machine: all fine. 4 check-ins waiting to be sent.")
    rejected = plain_log(_row("DEVICE_MESSAGE_REJECTED", "ERROR",
                              "reconcile_assignment_release message was rejected (EVIDENCE_INVALID: "
                              "UNCLASSIFIED_VALUE_ERROR).", subsystem="add"))
    assert rejected["title"] == ("The dashboard refused the end of a record check from the link box because it "
                                 "didn't match what the dashboard expected. The link box keeps it and will send it "
                                 "again.")
    delegated = plain_log(_row("ORACLE_RECONCILE_DELEGATED_TO_ADD", "INFO",
                               "Authoritative truth window 2026-11-05..2026-11-06 events=42 was durably delegated"))
    assert delegated["title"] == "Handed 42 records from 5 Nov to 6 Nov to the dashboard to send to the HR system."
    quarantined = plain_log(_row("ATTENDANCE_TIMESTAMP_QUARANTINED", "WARN"))
    assert quarantined["kind"] == "review" and quarantined["action"]


def test_missing_values_never_show_braces():
    assert fill("Started repairing the link box memory ({p}% full).", {}) == "Started repairing the link box memory."
    assert fill("{n} records with an impossible date or time were set aside for checking.", {}) == (
        "Some records with an impossible date or time were set aside for checking.")


def _logs():
    return table("logs", column("code"), column("level"), column("subsystem"))


def _sqlite_rows():
    from sqlalchemy import create_engine, insert, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("create table logs (code text, level text, subsystem text)"))
        rows = [{"code": code, "level": level, "subsystem": "zkt"} for code in sorted(WORDS) for level in LEVELS]
        rows += [{"code": None, "level": level, "subsystem": sub} for level in LEVELS
                 for sub in ("zkt", "ords", "users", "storage", "ota", "firmware", None)]
        rows += [{"code": "NOT_IN_THE_DICTIONARY", "level": level, "subsystem": "zkt"} for level in LEVELS]
        conn.execute(insert(_logs()), rows)
    return engine


@pytest.mark.parametrize("kind", activity_words.KINDS)
def test_kind_clause_selects_exactly_the_rows_plain_log_labels(kind):
    engine = _sqlite_rows()
    logs = _logs()
    with engine.connect() as conn:
        selected = {tuple(row) for row in conn.execute(
            select(logs.c.code, logs.c.level, logs.c.subsystem).where(kind_clause({kind}, logs.c.code, logs.c.level)))}
        every = {tuple(row) for row in conn.execute(select(logs.c.code, logs.c.level, logs.c.subsystem))}
    labelled = {row for row in every if display_kind(row[0], row[1]) == kind}
    # Message variants may raise a row's kind, so their codes are also selected for problem/review.
    extra = {row for row in selected - labelled if row[0] in activity_words.VARIANT_CODES}
    assert selected - extra == labelled


@pytest.mark.parametrize("topic", activity_words.TOPICS)
def test_topic_clause_selects_exactly_the_rows_of_a_group(topic):
    engine = _sqlite_rows()
    logs = _logs()
    with engine.connect() as conn:
        selected = {tuple(row) for row in conn.execute(
            select(logs.c.code, logs.c.level, logs.c.subsystem).where(
                topic_clause({topic}, logs.c.code, logs.c.subsystem)))}
        every = {tuple(row) for row in conn.execute(select(logs.c.code, logs.c.level, logs.c.subsystem))}
    assert selected == {row for row in every if activity_words.topic_of(row[0], row[2]) == topic}
