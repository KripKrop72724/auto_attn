"""The light guide must match firmware/zone_lite/main/led_status.c (no Qt needed)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sl_installer.ui import leds

REPO_ROOT = Path(__file__).resolve().parents[4]
LED_DIR = REPO_ROOT / "firmware" / "zone_lite" / "main"


def firmware_states():
    header = (LED_DIR / "led_status.h").read_text(encoding="utf-8")
    block = header[header.index("typedef enum {") : header.index("} led_status_t;")]
    return set(re.findall(r"LED_STATUS_([A-Z_]+)", block))


def firmware_priorities():
    source = (LED_DIR / "led_status.c").read_text(encoding="utf-8")
    body = source[
        source.index("static int priority_for_status") : source.index("static bool is_fault_status")
    ]
    found, pending = {}, []
    for line in body.splitlines():
        case = re.search(r"case LED_STATUS_([A-Z_]+):", line)
        if case:
            pending.append(case.group(1))
            continue
        value = re.search(r"return (\d+);", line)
        if value and pending:
            for name in pending:
                found[name] = int(value.group(1))
            pending = []
    return found


def test_every_firmware_state_is_explained():
    guide = {state.key for state in leds.LIGHT_STATES} - {"OFF", "LIVE_PUNCH"}
    assert guide == firmware_states()


def test_priorities_match_the_firmware():
    priorities = firmware_priorities()
    for state in leds.LIGHT_STATES:
        if state.key in priorities:
            assert state.priority == priorities[state.key], state.key


def test_every_rhythm_has_states_and_words():
    for rhythm in leds.RHYTHMS:
        assert leds.states_for(rhythm.key), rhythm.key
        assert rhythm.title and rhythm.description
    for state in leds.LIGHT_STATES:
        assert state.meaning.endswith(".") and state.what_to_do


def test_colours_within_a_rhythm_have_different_names():
    for rhythm in leds.RHYTHMS:
        names = [state.colour for state in leds.states_for(rhythm.key)]
        assert len(names) == len(set(names)), rhythm.key


@pytest.mark.parametrize(
    "key, tick, expected",
    [
        ("WIFI_CONNECTING", 100, (0, 0, 255)),  # slow blink: 500 ms on
        ("WIFI_CONNECTING", 600, (0, 0, 0)),  # ... 500 ms off
        ("RECOVERY_REBOOT", 100, (255, 0, 0)),  # fast blink: 150 ms on
        ("RECOVERY_REBOOT", 200, (0, 0, 0)),  # ... 150 ms off
        ("BACKLOG", 100, (255, 160, 0)),  # heartbeat: first flash
        ("BACKLOG", 200, (20, 12, 0)),  # dim between flashes
        ("BACKLOG", 300, (255, 160, 0)),  # second flash
        ("BACKLOG", 1000, (20, 12, 0)),
        ("BOOTING", 0, (32, 32, 32)),  # breathing starts at level 32
        ("BOOTING", 1000, (212, 212, 212)),  # ... peaks at 212
        ("ZKT_DISCOVERING", 1000, (0, 212, 212)),
        ("SYNCING", 1000, (212, 0, 212)),
        ("STORAGE_REPAIR", 100, (255, 255, 255)),  # white and blue swapping
        ("STORAGE_REPAIR", 200, (0, 0, 255)),
        ("ZKT_FLAPPING", 100, (255, 120, 0)),
        ("ZKT_FLAPPING", 200, (0, 40, 90)),
        ("HEALTHY", 1234, (0, 255, 0)),
        ("FATAL", 777, (255, 0, 0)),
        ("OFF", 100, (0, 0, 0)),
    ],
)
def test_timing_matches_render_status(key, tick, expected):
    assert leds.colour_at(leds.BY_KEY[key], tick) == expected
