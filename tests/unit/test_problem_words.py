"""Problem words: a plain title, sentence and next step for every problem code."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from zk_add.device_health import POLICY
from zk_add.problem_words import WORDS, plain

ROOT = Path(__file__).resolve().parents[2]
GLOSSARY = ROOT / "apps/add_frontend/src/words/glossary.ts"
FALLBACK_BANNED = {"esp", "zkt", "ords", "oracle", "terminal", "connector", "hikvision", "reconcile",
                   "reconciliation", "custody", "cursor", "epoch", "anchor", "outbox", "payload", "snapshot",
                   "ordinal", "chunk", "journal", "nvs", "spiffs", "psram", "ota", "uid", "telemetry", "heartbeat",
                   "firmware", "lease", "quarantine", "quarantined", "punch", "errno", "derived"}
SAMPLE_DETAILS = {
    "boots": ["a", "b", "c"], "window_seconds": 900, "drift_seconds": -420, "affected_users": 3, "quarantined": 2,
    "exception_count": 4, "slot_count": 1, "event_count": 5, "first_stale_at": "2026-11-05T04:24:00Z",
    "types": ["heartbeat", "reconcile_assignment_release"], "failure_category": "HTTP_401", "reason": "HIK_AUTH",
}


def banned_words() -> set[str]:
    """One list: the frontend glossary when it exists, else the same words kept here."""
    if GLOSSARY.exists():
        match = re.search(r"BANNED_WORDS[^=]*=\s*\[([^\]]*)\]", GLOSSARY.read_text(encoding="utf-8"))
        if match:
            return {word.lower() for word in re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))}
    return FALLBACK_BANNED


@pytest.mark.parametrize("details", [{}, SAMPLE_DETAILS], ids=["empty", "sample"])
@pytest.mark.parametrize("code", sorted(POLICY))
def test_every_policy_code_has_plain_words(code, details):
    words = plain(code, details, minutes=12)
    title, detail, next_step = words["plain_title"], words["plain_detail"], words["next_step"]
    assert title and detail and next_step
    assert not title.endswith("."), title
    assert detail.endswith(".") and next_step.endswith("."), (detail, next_step)
    banned = banned_words()
    for text in (title, detail, next_step):
        assert not set(re.findall(r"[a-z]+", text.lower())) & banned, text
        assert "_" not in text and "=" not in text and not re.search(r"[0-9a-fA-F]{8,}", text), text
        assert not re.search(r"\b(LF-)?ZONE-", text), text


def test_dictionary_covers_policy():
    assert set(POLICY) - set(WORDS) == set()


def test_numbers_come_from_details():
    assert plain("ESP_RESTART_LOOP", SAMPLE_DETAILS)["plain_title"] == "Link box keeps restarting (3 times in 15 min)"
    assert plain("ZKT_CLOCK_DRIFT", SAMPLE_DETAILS)["plain_title"] == "Machine clock is 7 min slow"
    assert plain("DUPLICATE_USER_CNIC", {"affected_users": 1})["plain_title"] == "1 person shares one CNIC"
    assert plain("HIK_CAPTURE_UNHEALTHY", {"reason": "HIK_NETWORK"})["plain_title"] == "Machine not responding"
    assert plain("ORDS_DELIVERY_FAILED", {"failure_category": "HTTP_403"})["next_step"] == (
        "The HR system refused the login; tell IT now.")
    assert plain("DEVICE_MESSAGE_REJECTED", {"types": ["heartbeat"]})["plain_title"] == (
        "Some information from this device was refused (a status report)")
    assert plain("ESP_DURABILITY_FAULT")["plain_title"] == "Link box cannot save attendance safely"


def test_unknown_codes_get_a_group_title():
    words = plain("ZKT_SOMETHING_NEW")
    assert words["plain_title"] == "Machine problem" and words["group"] == "machine"
    assert plain("MYSTERY")["plain_title"] == "Link box problem"


def test_health_payloads_keep_their_keys_and_add_the_words():
    from datetime import timedelta
    from types import SimpleNamespace

    from zk_add.device_health import Reason, _primary_payload, reason_payload
    from zk_add.time_utils import utc_now

    reason = Reason(code="TERMINAL_DISCONNECTED", error_code=None, tier="OFFLINE", currency="CURRENT", gating=False,
                    severity="HIGH", message="Terminal disconnected", since=utc_now() - timedelta(minutes=12),
                    last_seen_at=None, clear_condition="Clears when the terminal answers.", priority=1)
    payload = reason_payload(reason)
    assert {"code", "message", "clear_condition", "operator"} <= set(payload)
    assert payload["plain_title"] == "Machine not responding"
    assert payload["plain_detail"] == "The link box has not reached the machine for 12 min."
    primary = _primary_payload(SimpleNamespace(primary=reason))
    assert set(primary) == {"code", "error_code", "tier", "message", "since", "plain_title", "plain_detail",
                            "next_step"}
