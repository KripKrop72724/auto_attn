"""Codes behind the attendance tools' reason sentences (UI/UX revamp WORDS-8, backend)."""

import ast
import inspect

from sqlalchemy import select

from test_attendance_force_release import approve, checked, store as store, tick
from test_attendance_repair import repair_store as repair_store
from zk_add import attendance_direct_ords, attendance_force_release, attendance_recovery, attendance_words
from zk_add.models import AttendanceRecoveryJob as Job


def test_every_force_release_message_and_fixed_sentence_has_a_reason_code():
    for code, sentence in attendance_force_release.MESSAGES.items():
        assert attendance_words.force_item_reason_code(None, sentence) == code
        assert attendance_words.force_item_reason_code(code, None) == code
    source = inspect.getsource(attendance_force_release)
    for sentence, code in attendance_words.FORCE_FIXED_REASON_CODES.items():
        assert sentence in source, "the worker's fixed sentence changed; update FORCE_FIXED_REASON_CODES"
        assert attendance_words.force_item_reason_code("ANYTHING", sentence) == code
    unknown = attendance_force_release.explain("SOMETHING_NEW")
    assert attendance_words.force_item_reason_code("SOMETHING_NEW", unknown) == "UNKNOWN"


def _event_lane_sentences() -> set[str]:
    tree = ast.parse(inspect.getsource(attendance_recovery._event_lane))
    sentences = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            reason = node.value.elts[1]
            assert isinstance(reason, ast.Constant), "every _event_lane reason must be a fixed sentence"
            sentences.add(reason.value)
    return sentences


def test_lane_reason_codes_cover_every_event_lane_sentence():
    sentences = _event_lane_sentences()
    assert len(sentences) >= 11
    assert sentences == set(attendance_words.LANE_REASON_CODES)
    assert attendance_words.lane_reason_code("Something new.") == "UNKNOWN"
    codes = list(attendance_words.LANE_REASON_CODES.values())
    assert len(codes) == len(set(codes))


def test_direct_send_reason_fallback_is_plain_and_never_a_title_cased_code():
    text = attendance_direct_ords._reason("SOMETHING_NEW")
    assert text == attendance_words.UNKNOWN_REASON
    assert "_" not in text and "Something New" not in text
    assert attendance_direct_ords._reason("CLOCK_INVALID") == attendance_direct_ords.REASONS["CLOCK_INVALID"]
    assert attendance_direct_ords._reason(None) == "Approved; waiting to prepare delivery."


def test_force_release_items_and_devices_carry_their_codes(store):
    sessions, _, _ = store
    job_id = checked(store)
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        rows = attendance_force_release.items_page(db, job)["rows"]
        assert rows and all(row["reason_code"] for row in rows)
        assert rows[0]["reason_code"] == attendance_words.force_item_reason_code(None, rows[0]["reason"])
        devices = attendance_force_release.serialize(db, job, "operator")["devices"]
        assert devices and all("error_code" in device for device in devices)
    approve(store, job_id)
    tick(sessions)
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        rows = attendance_force_release.items_page(db, job)["rows"]
        assert {row["reason_code"] for row in rows} <= {"WAITING_ORACLE", "CONFIRMED", "READY"} | set(
            attendance_force_release.MESSAGES)
