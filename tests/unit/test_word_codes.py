"""Words guard source of truth: the checked-in code lists are fresh and every log code has words."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("export_word_codes", ROOT / "scripts/export_word_codes.py")
export_word_codes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export_word_codes)

GROUPS = {"machine", "link_box", "hr_system", "people", "software", "storage"}


def test_checked_in_code_lists_are_fresh():
    expected = export_word_codes.render(export_word_codes.build())
    stale = [str(path.relative_to(ROOT)) for path in export_word_codes.OUTPUTS
             if not path.exists() or path.read_text(encoding="utf-8") != expected]
    if stale:
        current = set(export_word_codes.build()["domains"]["log"])
        import json

        old = set(json.loads(export_word_codes.OUTPUTS[1].read_text(encoding="utf-8"))["domains"]["log"]) \
            if export_word_codes.OUTPUTS[1].exists() else set()
        raise AssertionError(
            f"Run `python scripts/export_word_codes.py` and add words for: {sorted(current - old) or 'the changes'} "
            f"(stale: {stale})")


def test_every_log_code_has_a_class():
    data = export_word_codes.build()
    missing = sorted(set(data["domains"]["log"]) - set(data["log_classes"]))
    assert missing == [], f"add plain words in apps/add_backend/zk_add/activity_words_data.py for: {missing}"
    for code, value in data["log_classes"].items():
        assert value["group"] in GROUPS, code
        assert value["kind"] in {"routine", "problem"}, code
        assert value["act"] in {"no", "if_repeats", "yes", "list"}, code


def test_sanity_floors_so_a_broken_pattern_cannot_pass():
    domains = export_word_codes.build()["domains"]
    assert len(domains["log"]) >= 180
    assert len(domains["problem"]) >= 47
    for name in ("ords_status", "identity_status", "command_status", "command_type", "deployment_state"):
        assert len(domains[name]) >= 5, name
