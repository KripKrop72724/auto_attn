"""Every state of the board's light, from firmware/zone_lite/main/led_status.c.

The colours here are the light's own RGB values (render_status), not UI
colours, so the State Life palette does not apply to them: the guide has to
look like the real light. The timings mirror render_status exactly:

- slow blink: 500 ms on, 500 ms off;
- fast blink: 150 ms on, 150 ms off;
- breathing: a 2 s ramp of the level from 32 up to 212 and back;
- heartbeat: two 120 ms flashes in each 2 s, dim in between;
- swap: two colours alternating every 150 ms.

When several things are true, the board shows the one with the highest
priority, so the guide lists the meaning of what is shown, not everything
that might also be true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..core.api import Severity

RGB = Tuple[int, int, int]

STEADY = "steady"
SLOW = "slow"
FAST = "fast"
BREATHING = "breathing"
HEARTBEAT = "heartbeat"
SWAP = "swap"
OFF = "off"
FLASH = "flash"


@dataclass(frozen=True)
class Rhythm:
    key: str
    title: str
    description: str


RHYTHMS: Tuple[Rhythm, ...] = (
    Rhythm(STEADY, "Always on", "The light stays on and does not change."),
    Rhythm(SLOW, "Slow blink", "On and off, about once a second."),
    Rhythm(FAST, "Fast blink", "On and off very quickly, about three times a second."),
    Rhythm(BREATHING, "Slowly brighter and dimmer", "The light fades up and down, like breathing."),
    Rhythm(HEARTBEAT, "Two quick flashes", "Two short flashes, then a pause."),
    Rhythm(SWAP, "Two colours swapping", "It changes quickly between two colours."),
    Rhythm(OFF, "No light", "The light is off all the time."),
    Rhythm(FLASH, "A quick green flash", "A short green flash now and then."),
)


@dataclass(frozen=True)
class LightState:
    key: str  # led_status_t name without LED_STATUS_, or OFF / LIVE_PUNCH
    rhythm: str
    colour: str  # what a person would call the colour
    rgb: RGB  # the "on" colour (before the board's 96/255 dimming)
    rgb2: Optional[RGB]  # second colour for SWAP, dim colour for HEARTBEAT
    priority: int
    meaning: str
    what_to_do: Tuple[str, ...]
    severity: Severity


_WAIT = Severity.INFO
_GOOD = Severity.SUCCESS
_WARN = Severity.WARNING
_BAD = Severity.ERROR

LIGHT_STATES: Tuple[LightState, ...] = (
    LightState(
        "HEALTHY",
        STEADY,
        "Green",
        (0, 255, 0),
        None,
        10,
        "Everything is working. Attendance is being sent.",
        ("Nothing to do.",),
        _GOOD,
    ),
    LightState(
        "ZKT_AUTHENTICATED",
        STEADY,
        "Light blue",
        (0, 255, 255),
        None,
        50,
        "The board is connected to the attendance machine and is getting ready.",
        ("Wait a few minutes. The light turns green when everything works.",),
        _WAIT,
    ),
    LightState(
        "FATAL",
        STEADY,
        "Red",
        (255, 0, 0),
        None,
        100,
        "The board has stopped. It has no working settings.",
        (
            "Connect the board to this computer.",
            "Choose Check or fix a board, then Fix settings.",
            "If it stays red, call head office.",
        ),
        _BAD,
    ),
    LightState(
        "WIFI_CONNECTING",
        SLOW,
        "Blue",
        (0, 0, 255),
        None,
        30,
        "The board is trying to join the Wi-Fi. Its own setup Wi-Fi may also be open.",
        (
            "Wait 2 minutes.",
            "If it keeps blinking, use a phone to join the board's own Wi-Fi with the details "
            "below. Then choose this site's Wi-Fi.",
        ),
        _WAIT,
    ),
    LightState(
        "BLOCKED_IDENTITY",
        SLOW,
        "Purple",
        (255, 0, 255),
        None,
        70,
        "Some punches belong to people head office cannot match yet. Head office is handling it.",
        ("Nothing to do here.", "Tell head office if it lasts more than a day."),
        _WAIT,
    ),
    LightState(
        "ZKT_FAILURE",
        SLOW,
        "Yellow",
        (255, 220, 0),
        None,
        75,
        "The board found the attendance machine, but the machine did not accept its comm key. "
        "Or a planned restart of the machine did not work.",
        (
            "Check the comm key in the attendance machine's menu.",
            "Then connect the board here and choose Check or fix a board.",
        ),
        _WARN,
    ),
    LightState(
        "TRUTH_REPAIR",
        SLOW,
        "Amber",
        (255, 170, 0),
        None,
        72,
        "The board is comparing its records with the attendance machine.",
        ("Wait.", "Do not unplug the board."),
        _WAIT,
    ),
    LightState(
        "ORDS_FAILURE",
        SLOW,
        "Orange",
        (255, 80, 0),
        None,
        80,
        "The board cannot send attendance to the HR system. The punches are kept safe on the "
        "board.",
        ("Check the internet at this site.", "Call head office if it lasts more than 30 minutes."),
        _WARN,
    ),
    LightState(
        "LOCAL_FAILURE",
        SLOW,
        "Red-orange",
        (255, 24, 0),
        None,
        85,
        "The board has a problem with its own storage.",
        (
            "Unplug the board, wait 10 seconds and plug it back in.",
            "If the light comes back, call head office.",
        ),
        _BAD,
    ),
    LightState(
        "RECOVERY_REBOOT",
        FAST,
        "Red",
        (255, 0, 0),
        None,
        90,
        "The board is restarting the attendance machine, as planned. "
        "This happens around 2 am, 12 noon and 10 pm.",
        ("Do nothing. Wait about 2 minutes.",),
        _WAIT,
    ),
    LightState(
        "BOOTING",
        BREATHING,
        "White",
        (255, 255, 255),
        None,
        20,
        "The board is starting up. It may also be checking its saved records.",
        ("Wait. Do not unplug the board.", "If it lasts more than 15 minutes, call head office."),
        _WAIT,
    ),
    LightState(
        "ZKT_DISCOVERING",
        BREATHING,
        "Light blue",
        (0, 255, 255),
        None,
        40,
        "The board is on the Wi-Fi and is looking for the attendance machine.",
        (
            "Check that the attendance machine is switched on and on the same network.",
            "If it lasts more than 5 minutes, call head office.",
        ),
        _WAIT,
    ),
    LightState(
        "SYNCING",
        BREATHING,
        "Purple",
        (255, 0, 255),
        None,
        60,
        "The board is copying records between the attendance machine and head office.",
        ("Wait.", "Do not unplug the board."),
        _WAIT,
    ),
    LightState(
        "BACKLOG",
        HEARTBEAT,
        "Amber",
        (255, 160, 0),
        (20, 12, 0),
        55,
        "Some punches are waiting to be sent. On a Hikvision machine it can also mean the "
        "machine is not answering properly.",
        (
            "Check the internet at this site.",
            "For Hikvision, check that the machine is on and its password has not changed.",
            "Call head office if it lasts more than an hour.",
        ),
        _WARN,
    ),
    LightState(
        "STORAGE_REPAIR",
        SWAP,
        "White and blue",
        (255, 255, 255),
        (0, 0, 255),
        25,
        "The board is repairing its saved records.",
        ("Do not unplug the board.", "Wait up to 15 minutes."),
        _WARN,
    ),
    LightState(
        "ZKT_FLAPPING",
        SWAP,
        "Orange and dark blue",
        (255, 120, 0),
        (0, 40, 90),
        74,
        "The connection to the attendance machine keeps dropping.",
        (
            "Check the attendance machine's power and network cable.",
            "Move the board closer to the Wi-Fi router.",
        ),
        _WARN,
    ),
    LightState(
        "OFF",
        OFF,
        "No light",
        (0, 0, 0),
        None,
        0,
        "The board has no power, or it did not start.",
        (
            "Check that the cable is pushed in at both ends.",
            "Unplug the board, wait 3 seconds and plug it back in.",
            "Connect it to this computer and choose Check or fix a board. "
            "If this app cannot find it, the board is faulty.",
        ),
        _BAD,
    ),
    LightState(
        "LIVE_PUNCH",
        FLASH,
        "Green",
        (0, 255, 0),
        None,
        0,
        "Someone just punched in or out. This is normal.",
        ("Nothing to do.",),
        _GOOD,
    ),
)

# Several of these look alike on a real board (scaled to 96/255 they are
# all shades of orange), so the guide says so next to them.
LOOK_ALIKE = frozenset({"ZKT_FAILURE", "TRUTH_REPAIR", "ORDS_FAILURE", "LOCAL_FAILURE"})

# The board's own Wi-Fi may be open in these states, so the guide shows its name and
# password next to the answer (the person may be at the site, far from the last screen).
HOTSPOT_STATES = frozenset({"WIFI_CONNECTING"})

BY_KEY: Dict[str, LightState] = {state.key: state for state in LIGHT_STATES}


def states_for(rhythm: str) -> Tuple[LightState, ...]:
    return tuple(state for state in LIGHT_STATES if state.rhythm == rhythm)


def _pulse(phase: int) -> int:
    return (phase if phase < 1000 else 2000 - phase) * 180 // 1000 + 32


def colour_at(state: LightState, tick_ms: int) -> RGB:
    """The light's colour ``tick_ms`` into its pattern, as render_status draws it."""
    phase = tick_ms % 2000
    slow_on = (phase % 1000) < 500
    fast_on = (tick_ms % 300) < 150
    heartbeat_on = phase < 120 or 260 <= phase < 380
    red, green, blue = state.rgb
    if state.rhythm == STEADY:
        return state.rgb
    if state.rhythm == SLOW:
        return state.rgb if slow_on else (0, 0, 0)
    if state.rhythm == FAST:
        return state.rgb if fast_on else (0, 0, 0)
    if state.rhythm == BREATHING:
        level = _pulse(phase)
        return (red * level // 255, green * level // 255, blue * level // 255)
    if state.rhythm == HEARTBEAT:
        return state.rgb if heartbeat_on else (state.rgb2 or (0, 0, 0))
    if state.rhythm == SWAP:
        return state.rgb if fast_on else (state.rgb2 or (0, 0, 0))
    if state.rhythm == FLASH:
        # A punch shows as a 250 ms green flash; repeat it every 2.5 s here.
        return state.rgb if tick_ms % 2500 < 250 else (0, 0, 0)
    return (0, 0, 0)
