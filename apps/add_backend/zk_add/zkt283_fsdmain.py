"""The reviewed FSDMAIN HIL scope of zone-lite-2.8.3 (7 November 2026).

LF-ZONE-FSDMAIN's uFace800 terminals hold 2,500 users. 2.8.2 and earlier keep
at most 2048, so the user snapshot is truncated (USER_SNAPSHOT_TRUNCATED),
the credential policy cannot be enforced and the ESP disconnects, again and
again. 2.8.3 is 2.8.2 holding 4096 users, with exactly 2.8.2's predecessors.
None of the FSDMAIN ESPs is one of the nationwide 17; the 17, their order and
every signed list stay unchanged, and 2.8.3 keeps that rule for any other list.

2.8.3 may instead be published HIL_ONLY to an exact ordered prefix of the
FSDMAIN ESPs (at most three), each pinned by its ESP hardware ID and its
owner-confirmed terminal serial, as 2.8.2's LHRCENTRAL scope. Its connector
ID is the one ADD shows for that ESP when the release is dispatched.

One owner-approved exception (7 November 2026): an FSDMAIN ESP on 2.8.1 never
stays connected long enough to acknowledge PIN_TERMINAL_SERIAL, so its binding
stays PENDING_DEVICE_ACK and can only reach CONFIRMED once 2.8.3 runs. For this
scope only, a target also matches while its binding is PENDING_DEVICE_ACK; the
serial, confirmed serial and expected serial must still all be the pin's.
hil_scope.target_matches, and every other release and scope, are unchanged.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

from zk_add.hil_scope import HilTarget

RELEASE_ID = "zone-lite-2.8.3"
VERSION = "2.8.3"
MAX_TARGETS = 3
BINDING_STATES = frozenset({"CONFIRMED", "PENDING_DEVICE_ACK"})


class Pin(NamedTuple):
    name: str
    mac: str
    terminal_serial: str


# In zone order; the same pins as deploy/add/hil-pins-2.8.3-fsdmain.json.
PINS = (
    Pin("FSDMAIN-01", "30:30:f9:17:87:2c", "CKPG231060360"),
    Pin("FSDMAIN-02", "8c:bf:ea:17:95:d0", "AF4C204260255"),
    Pin("FSDMAIN-03", "30:30:f9:1f:90:10", "AF4C225060506"),
)
# ADD's connector IDs are lowercase UUIDs.
CONNECTOR_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def exposure(identity: tuple, raw: Any) -> list[HilTarget] | None:
    """The exposed FSDMAIN targets, only for zone-lite-2.8.3.

    None when the list is not this scope (it does not start with FSDMAIN-01);
    the nationwide prefix rule then decides. A list that starts with it must be
    an exact ordered prefix of the pins, or it is refused.
    """
    if (tuple(identity[:2]) != (RELEASE_ID, VERSION) or not isinstance(raw, list) or not raw
            or not isinstance(raw[0], dict) or raw[0].get("mac") != PINS[0].mac):
        return None
    if len(raw) > min(len(PINS), MAX_TARGETS):
        raise ValueError("The 2.8.3 FSDMAIN scope has at most three targets, each one of its pins.")
    targets = [HilTarget.model_validate(row) for row in raw]
    if (raw != [target.model_dump() for target in targets]
            or len({target.connector_id for target in targets}) != len(targets)
            or any((target.mac, target.terminal_serial) != (pin.mac, pin.terminal_serial)
                   or not CONNECTOR_ID.fullmatch(target.connector_id)
                   for target, pin in zip(targets, PINS))):
        raise ValueError("The 2.8.3 HIL scope must be an exact ordered prefix of the FSDMAIN ESPs.")
    return targets


def matches(target: HilTarget, connector: Any) -> bool:
    """hil_scope.target_matches, except that the binding may still await the device's ACK."""
    terminal = connector.zkt_device
    return bool(
        connector.active
        and not connector.is_spare
        and connector.connector_id == target.connector_id
        and (connector.hardware_id or "").lower() == target.mac
        and terminal is not None
        and terminal.serial == target.terminal_serial
        and terminal.confirmed_serial == target.terminal_serial
        and terminal.expected_serial == target.terminal_serial
        and terminal.terminal_binding_state in BINDING_STATES
    )


def _targets(release: Any) -> list[HilTarget]:
    if (release is None or release.state != "HIL_ONLY"
            or (release.release_id, release.version) != (RELEASE_ID, VERSION)):
        return []
    try:
        return exposure((release.release_id, release.version),
                        (release.manifest or {}).get("_hil_targets")) or []
    except ValueError:
        return []


def target_admits(release: Any, target: HilTarget, connector: Any) -> bool:
    """``target`` is an exposed FSDMAIN target of the HIL_ONLY 2.8.3 and is this connector."""
    return target in _targets(release) and matches(target, connector)


def admits(connector: Any, release: Any) -> bool:
    """An exact FSDMAIN target of the HIL_ONLY zone-lite-2.8.3.

    The fleet offer guard keeps every other HIL_ONLY image to the nationwide 17.
    """
    return any(matches(target, connector) for target in _targets(release))
