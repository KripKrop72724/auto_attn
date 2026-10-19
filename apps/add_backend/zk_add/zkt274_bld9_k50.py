"""One reviewed K50 exposure for the published, signed zone-lite-2.7.4.

The owner asked on 19 October 2026 to OTA-test 2.7.4 on ZONE-BLD9-01, a K50
terminal. It is not the nationwide "BLD9 01" (connector 2f5cedd8, a G3 in
LF-ZONE-BLD9-01) and it is not one of the 17: the nationwide inventory, its
denominator and every signed manifest's ordered list stay unchanged.

Only the exact 2.7.4 bytes may expose it, as an 18th target appended to the
signed 17 in the HIL marker. The device runs 2.5.2 from its factory partition,
which 2.7.4 refuses as a rollback predecessor, so it first takes the exact
signed 2.4.12 into an OTA slot. Only those two exact releases may pass the
fleet offer guard for this one device, and both still wait for a free slot.
"""
from __future__ import annotations

from typing import Any

from zk_add.hil_2615_cities import SIGNED_BRIDGE_IDENTITIES
from zk_add.hil_scope import HilTarget, target_matches
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_bridge_contract import signed_hil_targets

ZONE = "ZONE-BLD9-01"
TARGET = HilTarget(
    connector_id="9d8c589f-9735-4b58-9277-a385a60d0c9b",
    mac="ac:27:6e:a3:33:28", terminal_serial="ANM4261301077",
)
# Same building as the nationwide BLD9 01 and 02: one upgrade at a time there.
LOCATION = "BLD9"
# (release ID, version, source SHA, signed artifact SHA, application SHA).
RELEASE_IDENTITY = (
    "zone-lite-2.7.4", "2.7.4", "87e14fe6e9756cd65a27e64ac1a7420fe35aa816",
    "9a3e2226b7abff11cc264472356c186d3b9cce87a0df059aeb0a9965f1f095c5",
    "3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac",
)
# (installed factory version, its exact application digest as ADD reported it
# on 19 October 2026, bridge version). After the bridge, 2.4.12 in ota_0 is an
# ordinary qualified 2.7.4 predecessor.
FACTORY_PREDECESSOR = (
    "2.5.2", "c8c4356bb6a7fd4dc781e893ad5290593599b2d167be30c271bd1d5db61934f1", "2.4.12",
)
BRIDGE_IDENTITY = SIGNED_BRIDGE_IDENTITIES[FACTORY_PREDECESSOR[2]]
# The state each release must hold to offer bytes to this device.
OFFER_RELEASES = {RELEASE_IDENTITY: "HIL_ONLY", BRIDGE_IDENTITY: "AVAILABLE"}


def exposed_targets() -> list[dict]:
    return [*signed_hil_targets(), TARGET.model_dump()]


def exposure(identity: tuple, raw: Any) -> list[HilTarget] | None:
    """The 18 ordered targets, only for the exact 2.7.4 bytes and list."""
    if tuple(identity) != RELEASE_IDENTITY or raw != exposed_targets():
        return None
    return [*(target.identity for target in TARGETS), TARGET]


def offer_location(release: Any, connector: Any) -> str | None:
    """The fleet guard's location for this exact device and release only."""
    from zk_add.ota import _application_sha256

    if (release is None or connector.firmware_family != "zkt" or connector.zone_id != ZONE
            or not target_matches(TARGET, connector)):
        return None
    identity = (release.release_id, release.version, release.git_sha,
                release.image_sha256, _application_sha256(release))
    return LOCATION if OFFER_RELEASES.get(identity) == release.state else None


def reserved_location(connector_id: str) -> str | None:
    """Where this device's occupied upgrade slot is, like a nationwide target's."""
    return LOCATION if connector_id == TARGET.connector_id else None
