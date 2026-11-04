"""The reviewed LHRCENTRAL HIL scope of zone-lite-2.8.2 (4 November 2026).

LF-ZONE-LHRCENTRAL-01, -02 and -03 cannot run a full reconcile: their ZKTeco
MB40-VL/ID terminals (platform ZAM180_TFT) return 49-byte attendance records,
which 2.8.1 and earlier refuse as SOURCE_RANGE_LAYOUT_INVALID. 2.8.2 reads
them. None of the three ESPs is one of the nationwide 17, so the 17's prefix
rule cannot reach them; the 17, their order and every signed list stay
unchanged, and 2.8.2 keeps that rule for any other list.

2.8.2 may instead be published HIL_ONLY to an exact ordered prefix of these
three ESPs. Each is pinned by its ESP hardware ID and its owner-confirmed
terminal serial. Its connector ID is the one ADD shows for that ESP when the
release is dispatched: onboarding keys a connector by hardware ID and never
rebinds it. ADD's exact target match still needs that connector, active, not
a spare, with its terminal binding CONFIRMED to the same serial.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

from zk_add.hil_scope import HilTarget, target_matches

RELEASE_ID = "zone-lite-2.8.2"
VERSION = "2.8.2"


class Pin(NamedTuple):
    name: str
    mac: str
    terminal_serial: str


# In zone order.
PINS = (
    Pin("LHRCENTRAL-01", "ac:27:6e:a3:44:68", "COVP232060159"),
    Pin("LHRCENTRAL-02", "14:c1:9f:46:97:d0", "COVP232060174"),
    Pin("LHRCENTRAL-03", "a4:cb:8f:d4:55:28", "COVP232060177"),
)
# ADD's connector IDs are lowercase UUIDs.
CONNECTOR_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def exposure(identity: tuple, raw: Any) -> list[HilTarget] | None:
    """The exposed LHRCENTRAL targets, only for zone-lite-2.8.2.

    None when the list is not this scope (it does not start with LHRCENTRAL-01);
    the nationwide prefix rule then decides. A list that starts with it must be
    an exact ordered prefix of the three pins, or it is refused.
    """
    if (tuple(identity[:2]) != (RELEASE_ID, VERSION) or not isinstance(raw, list) or not raw
            or not isinstance(raw[0], dict) or raw[0].get("mac") != PINS[0].mac):
        return None
    if len(raw) > len(PINS):
        raise ValueError("The 2.8.2 LHRCENTRAL scope has at most three targets.")
    targets = [HilTarget.model_validate(row) for row in raw]
    if (raw != [target.model_dump() for target in targets]
            or len({target.connector_id for target in targets}) != len(targets)
            or any((target.mac, target.terminal_serial) != (pin.mac, pin.terminal_serial)
                   or not CONNECTOR_ID.fullmatch(target.connector_id)
                   for target, pin in zip(targets, PINS))):
        raise ValueError("The 2.8.2 HIL scope must be an exact ordered prefix of the LHRCENTRAL ESPs.")
    return targets


def admits(connector: Any, release: Any) -> bool:
    """An exact LHRCENTRAL target of the HIL_ONLY zone-lite-2.8.2.

    The fleet offer guard keeps every other HIL_ONLY image to the nationwide 17.
    """
    if (release is None or release.state != "HIL_ONLY"
            or (release.release_id, release.version) != (RELEASE_ID, VERSION)):
        return False
    try:
        targets = exposure((release.release_id, release.version), (release.manifest or {}).get("_hil_targets"))
    except ValueError:
        return False
    return bool(targets) and any(target_matches(target, connector) for target in targets)
