"""Exact 2.4.12 bridges for the Life ZKT ESPs that run their own 2.5.2 build.

BLD9 01, BLD9 02 and G&P BLD8 are in the nationwide 17 with the
EXACT_FACTORY_BRIDGE prerequisite. The second LF-ZONE-BLD1-01 ESP, a G3 ZKT
onboarded on 28 October 2026, is outside it. Each was USB-bootstrapped with
its own signed 2.5.2 in the factory partition, a digest no published release
carries, so 2.7.x refuses it as a predecessor. Like Lahore 01/02 and the BLD9
K50, each first takes the exact published 2.4.12 into an OTA slot; from there
the ordinary 2.7.x predecessor checks apply.

These are kept apart from the 2.6.15 city cohort so its signed HIL exposure
is unchanged.
"""

from zk_add.hil_scope import HilTarget


LIFE_TARGETS = {
    "LF-ZONE-BLD9-01": HilTarget(connector_id="2f5cedd8-e314-47b0-8074-bc4bb8a603cc",
        mac="ac:27:6e:a3:0a:08", terminal_serial="PGB1261300034"),
    "LF-ZONE-BLD9-02": HilTarget(connector_id="a886e2d9-204d-425c-bc8f-ded85fc89874",
        mac="ac:27:6e:a5:4c:d8", terminal_serial="PGB1254700036"),
    "LF-ZONE-G_PBLD8-01": HilTarget(connector_id="a1ff7b24-4dcb-4dde-ad41-1a8401c7b006",
        mac="e0:72:a1:d6:f3:28", terminal_serial="PGB1254700027"),
    "LF-ZONE-BLD1-01": HilTarget(connector_id="d4b8e5ee-8539-4719-b92f-a9458f6600d3",
        mac="14:c1:9f:46:ae:1c", terminal_serial="PGB1261300043"),
}

# (installed factory version, its exact application digest as ADD reported it
# on 29 October 2026, bridge version).
LIFE_FACTORY_PREDECESSORS = {
    "LF-ZONE-BLD9-01": ("2.5.2", "191b63c5f18485a9aa7f705f77679f932e79f326e1bf87e3c4bb237d249fb786", "2.4.12"),
    "LF-ZONE-BLD9-02": ("2.5.2", "00dcc3514b997570fcdf7495f7b8a85302bcff6c2670120d13245f93c0424e8b", "2.4.12"),
    "LF-ZONE-G_PBLD8-01": ("2.5.2", "e068ee75073e3198f7894f04a249169eef96feb996d9b7db7e5a8c7a04829e91", "2.4.12"),
    "LF-ZONE-BLD1-01": ("2.5.2", "0cbd3350d316fe4c9c21d334d593c499b2eee6b671e84aec9e28b4591e78feb3", "2.4.12"),
}
