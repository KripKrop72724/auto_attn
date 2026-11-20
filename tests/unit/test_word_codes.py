"""The plain-words code list stays in step with the source (02-words WORDS-2).

`scripts/export_word_codes.py` lists every code the dashboard can show, by
words domain, from the backend (AST) and the link box software (C patterns).
The checked-in copies must match what it builds, so a new code shows up in a
pull request; the frontend words test (apps/add_frontend/src/words/words.test.ts)
then fails until the code has words (dashboard and backend codes) or lists it
as a follow-up (link box only codes, owner decision D-22).
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_word_codes.py"
spec = importlib.util.spec_from_file_location("export_word_codes", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

WORDS = ROOT / "apps" / "add_frontend" / "src" / "words"


def test_checked_in_code_lists_are_fresh():
    expected = exporter.render(exporter.build())
    for path in exporter.OUTPUTS:
        assert path.exists(), f"{path.relative_to(ROOT)} is missing. Run `python scripts/export_word_codes.py`."
        current = path.read_text(encoding="utf-8")
        if current != expected:
            old = json.loads(current)["domains"]
            new = json.loads(expected)["domains"]
            added = sorted(
                f"{name}.{code}"
                for name, domain in new.items()
                for code in domain["backend"] + domain["firmware"]
                if code not in old.get(name, {}).get("backend", []) + old.get(name, {}).get("firmware", [])
            )
            raise AssertionError(
                f"{path.relative_to(ROOT)} is out of date. Run `python scripts/export_word_codes.py` "
                f"and add words for: {', '.join(added) or '(codes were removed or moved)'}"
            )


def test_code_lists_are_not_empty():
    domains = exporter.build()["domains"]
    count = {name: len(d["backend"]) + len(d["firmware"]) for name, d in domains.items()}
    assert count["log"] >= 180
    assert count["problem"] >= 47
    assert count["error"] >= 50
    assert count["updateCode"] >= 90
    assert set(count) == set(exporter.DOMAINS)
    for name, domain in domains.items():
        assert not set(domain["backend"]) & set(domain["firmware"]), name
        for code in domain["backend"] + domain["firmware"]:
            assert exporter.CODE.match(code), f"{name}: {code!r} is not a code"


def test_every_policy_code_and_known_live_code_is_listed():
    from zk_add.device_health import POLICY

    domains = exporter.build()["domains"]
    assert set(POLICY) == set(domains["problem"]["backend"])
    logs = set(domains["log"]["backend"]) | set(domains["log"]["firmware"])
    for live in ("ZKT_TIME_SYNC_READBACK_FAILED", "HIK_POLL_STATUS", "IDENTITY_CATALOG_APPLIED",
                 "ADD_MESSAGE_PROCESSING_FAILED", "BLOCKED_IDENTITY_RECOVERY_DEFERRED",
                 "ZKT_FRAME_SESSION_MISMATCH", "STORAGE_REPAIR_EXCEPTION_UNREPAIRED"):
        assert live in logs, live


def _banned_words() -> list[str]:
    source = (WORDS / "glossary.ts").read_text(encoding="utf-8")
    block = re.search(r"export const BANNED_WORDS: readonly string\[\] = \[(.*?)\]", source, re.S)
    assert block, "BANNED_WORDS not found in glossary.ts"
    return re.findall(r"'([^']+)'", block.group(1))


def test_banned_words_list_is_readable_by_the_backend_word_tests():
    words = _banned_words()
    assert len(words) > 60
    assert {"terminal", "oracle", "held", "hold", "kept back", "esp"} <= set(words)


def test_device_status_offline_words_match_public_status():
    """D-1: the dashboard's Offline reasons read exactly like /status."""
    from zk_add.public_status import REASONS

    source = (WORDS / "deviceStatus.ts").read_text(encoding="utf-8")
    for code, (heading, sentence) in REASONS.items():
        pattern = re.compile(
            re.escape(code) + r": \{ heading: (['\"])(.*?)\1, sentence: (['\"])(.*?)\3 \}")
        found = pattern.search(source)
        assert found, f"deviceStatus.ts has no OFFLINE_REASONS entry for {code}"
        assert found.group(2) == heading, code
        assert found.group(4).replace("\\'", "'") == sentence, code
