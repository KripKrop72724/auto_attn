"""Exact experimental first-OTA policy; pure data shared with the signer.

An installed digest is a target pin, not a claim that the old image's full
signature or physical fallback has already been independently verified.
"""
from copy import deepcopy

FACTORY_BRIDGE_VERSION = "2.6.22"
FACTORY_TRIAL_MARKER = ":FACTORY_TRIAL=1"
FACTORY_TRIAL_SECONDS = 3600
FACTORY_ADDRESS = 0x20000
FACTORY_SIZE = 0x280000
FACTORY_LAYOUT_SHA256 = "add0fe4dc7cea9719b75868b60f74ba705daff49cca94a77a2d911f18b7c40df"
FACTORY_TARGETS = (
    {"connector_id": "a1ff7b24-4dcb-4dde-ad41-1a8401c7b006", "mac": "e0:72:a1:d6:f3:28",
     "terminal_serial": "PGB1254700027", "onboarding_generation": 4,
     "factory_version": "2.5.2",
     "factory_application_sha256": "e068ee75073e3198f7894f04a249169eef96feb996d9b7db7e5a8c7a04829e91"},
    {"connector_id": "2f5cedd8-e314-47b0-8074-bc4bb8a603cc", "mac": "ac:27:6e:a3:0a:08",
     "terminal_serial": "PGB1261300034", "onboarding_generation": 1,
     "factory_version": "2.5.2",
     "factory_application_sha256": "191b63c5f18485a9aa7f705f77679f932e79f326e1bf87e3c4bb237d249fb786"},
    {"connector_id": "a886e2d9-204d-425c-bc8f-ded85fc89874", "mac": "ac:27:6e:a5:4c:d8",
     "terminal_serial": "PGB1254700036", "onboarding_generation": 2,
     "factory_version": "2.5.2",
     "factory_application_sha256": "00dcc3514b997570fcdf7495f7b8a85302bcff6c2670120d13245f93c0424e8b"},
)


def factory_trial_signing_contract():
    """Known installed target pins; no operational or runtime pass is granted."""
    return {"schema_version": 1, "targets": deepcopy(list(FACTORY_TARGETS)),
            "require_3fl_writer_hil": True}


def validate_factory_trial_contract(value):
    expected = factory_trial_signing_contract()
    if (not isinstance(value, dict) or value != expected
            or type(value.get("schema_version")) is not int
            or value.get("require_3fl_writer_hil") is not True
            or not isinstance(value.get("targets"), list)
            or any(not isinstance(target, dict) or type(target.get("onboarding_generation")) is not int
                   for target in value["targets"])):
        raise ValueError("Factory trial requires its exact signed three-device policy.")
    return value


def factory_trial_targets():
    return [{key: target[key] for key in ("connector_id", "mac", "terminal_serial")}
            for target in FACTORY_TARGETS]


def factory_trial_exposure(raw):
    """Only these three, in order; other14 remain in the nationwide denominator."""
    expected = factory_trial_targets()
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(expected) or raw != expected[:len(raw)]:
        raise ValueError("Factory trial exposure requires an exact ordered factory-target prefix.")
    return raw
