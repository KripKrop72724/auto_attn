"""Approved nationwide ZKT denominator and observation waves (3 October 2026)."""
from dataclasses import dataclass

from zk_add.hil_scope import HilTarget


@dataclass(frozen=True)
class NationwideTarget:
    identity: HilTarget
    name: str
    location: str
    model: str
    wave: str
    hours: int
    prerequisite: str | None = None


def _target(connector, mac, serial, name, location, model, wave, prerequisite=None):
    return NationwideTarget(HilTarget(connector_id=connector, mac=mac, terminal_serial=serial),
                            name, location, model, wave, 168 if wave in {"B", "D"} else 72,
                            prerequisite)


TARGETS = (
    _target("2ca9a4c2-5ae4-4330-8d14-840223672897", "a4:cb:8f:d4:66:64", "PGB1261200074", "SLICTOWER 3FL", "SLICTOWER", "G3", "A"),
    _target("ef1b6fe9-592b-4cf3-95e7-9c6b600f7812", "ac:27:6e:a5:47:64", "AEXH232260005", "Swat 01", "SWAT", "SilkBio-101TC/ID", "B"),
    _target("ab5f934a-5430-4ba2-a86d-5ac849276e0d", "a4:cb:8f:d4:61:ac", "CKPG221260316", "Faisalabad 02", "FAISALABAD", "uFace800 Plus/ID", "B"),
    _target("0e991162-5ab9-467a-a952-4dc4c420691c", "ac:27:6e:a3:10:0c", "RKQ4245100152", "Multan 01", "MULTAN", "uFace800", "B"),
    _target("06d8706c-8b9e-4a48-b8c7-a76d16da6ed4", "ac:27:6e:a3:de:e8", "AF4C211861133", "Multan 02", "MULTAN", "uFace800/ID", "B"),
    _target("bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e", "e0:72:a1:d7:05:c4", "CJH9211060009", "Peshawar 02", "PESHAWAR", "MB40-VL/ID", "B"),
    _target("510baddb-8eff-4817-bc48-549ee34bbd0f", "ac:27:6e:a4:4e:d4", "PGB1261300022", "BLD5 01", "BLD5", "G3", "C"),
    _target("beb5f8eb-e4f3-4620-8f1a-cfa460bb1b73", "ac:27:6e:a5:5a:20", "CKPG221260245", "Faisalabad 01", "FAISALABAD", "uFace800 Plus/ID", "C"),
    _target("9726fe6c-2905-447a-9041-2fa606660058", "ac:27:6e:a3:16:d4", "AEH2232460004", "Lahore 01", "LAHORE", "G3", "C"),
    _target("474fd36e-6c75-4e0d-9c1f-cc97c4a442f8", "ac:27:6e:a3:07:f8", "OCN6060066052700045", "Quetta 01", "QUETTA", "uFace800/ID", "C"),
    _target("233dac02-eb1b-4598-a876-e3a7b1ecfd54", "e0:72:a1:d5:08:a0", "CJH9211060002", "Peshawar 06", "PESHAWAR", "MB40-VL/ID", "C"),
    _target("4567587c-29ee-4e59-92a4-6c36650a84aa", "e0:72:a1:d6:3c:7c", "PGB1261200077", "SLICTOWER 13FL", "SLICTOWER", "G3", "D", "PERSISTENCE_AND_BOOT_PROOF"),
    _target("8727e27a-77be-41c7-bb9e-5a7f31e4ae67", "ac:27:6e:a4:54:b4", "CKPG221260408", "Lahore 02", "LAHORE", "uFace800 Plus/ID", "D", "STORAGE_AND_RECONCILIATION"),
    _target("7d6fadcc-f93e-4c35-b4cc-852af1ffac0c", "a4:cb:8f:d4:67:70", "RKQ4254900154", "Karachi 01", "KARACHI", "uFace800", "D", "CONTACT_AND_OUTAGE_DIAGNOSIS"),
    _target("a1ff7b24-4dcb-4dde-ad41-1a8401c7b006", "e0:72:a1:d6:f3:28", "PGB1254700027", "G&P BLD8", "BLD8", "G3", "D", "EXACT_FACTORY_BRIDGE"),
    _target("2f5cedd8-e314-47b0-8074-bc4bb8a603cc", "ac:27:6e:a3:0a:08", "PGB1261300034", "BLD9 01", "BLD9", "G3", "D", "EXACT_FACTORY_BRIDGE"),
    _target("a886e2d9-204d-425c-bc8f-ded85fc89874", "ac:27:6e:a5:4c:d8", "PGB1254700036", "BLD9 02", "BLD9", "G3", "D", "EXACT_FACTORY_BRIDGE"),
)
BY_ID = {target.identity.connector_id: target for target in TARGETS}
assert len(BY_ID) == 17


def validate_upgrade_batch(targets: list[HilTarget], *, passed: set[str],
                           active: set[str], paused: bool) -> None:
    """Used by qualification tooling; never silently remove a blocked connector."""
    if paused:
        raise ValueError("ROLLOUT_PAUSED")
    requested = {target.connector_id for target in targets}
    if not requested or len(requested) != len(targets) or requested & active:
        raise ValueError("INVALID_UPGRADE_BATCH")
    if not (requested | passed | active) <= BY_ID.keys():
        raise ValueError("TARGET_OUTSIDE_APPROVED_SCOPE")
    if len(requested | active) > 2:
        raise ValueError("MAXIMUM_TWO_UPGRADES")
    for target in targets:
        if target != BY_ID[target.connector_id].identity:
            raise ValueError("EXACT_TARGET_IDENTITY_MISMATCH")
    locations = [BY_ID[key].location for key in requested | active]
    if len(set(locations)) != len(locations):
        raise ValueError("ONE_UPGRADE_PER_LOCATION")
    next_wave = next((target.wave for target in TARGETS if target.identity.connector_id not in passed), None)
    if any(BY_ID[key].wave != next_wave for key in requested):
        raise ValueError("PREVIOUS_WAVE_INCOMPLETE")
