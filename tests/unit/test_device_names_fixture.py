"""The dashboard's naming fallback must match the backend naming rule.

apps/add_frontend/src/lib/deviceNames.fixture.json holds shared vectors: this
test checks the backend (zk_add/public_status.py) against them and
src/lib/deviceNames.test.ts checks the frontend mirror, so the two cannot drift.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zk_add.public_status import ONLINE, build_snapshot, device_name
from zk_add.time_utils import utc_now

FIXTURE = Path(__file__).resolve().parents[2] / "apps" / "add_frontend" / "src" / "lib" / "deviceNames.fixture.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", DATA["cases"], ids=[case["code"] or "<empty>" for case in DATA["cases"]])
def test_backend_name_rule_matches_the_shared_vectors(case):
    name = device_name(case["code"])
    assert (name.zone, name.location, name.unit, name.number) == (
        case["zone"], case["location"], case["unit"], case["number"])


def test_backend_friendly_names_match_the_live_names():
    now = utc_now()
    rows = [(SimpleNamespace(display_name=code, zone_id=code), ONLINE, now, None) for code in DATA["live"]["active"]]
    devices = build_snapshot(rows, now)["devices"]
    assert [{key: device[key] for key in ("code", "zone", "name", "location", "unit")} for device in devices] == DATA["live"]["names"]


def test_fixture_covers_the_24_live_names():
    assert len(DATA["live"]["active"]) + len(DATA["live"]["spare"]) == 24
