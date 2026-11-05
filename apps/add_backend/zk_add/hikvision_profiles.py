"""Publish profile inventories only after two complete, matching bounded scans."""
import hashlib
import json
import logging

from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.db import Base
from zk_add.models import Connector
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.time_utils import utc_now

logger = logging.getLogger(__name__)


class HikvisionProfileScan(Base):
    __tablename__ = "add_hikvision_profile_scans"
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(32))
    phase: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    data_encrypted: Mapped[str] = mapped_column(Text)
    last_page_digest: Mapped[str] = mapped_column(String(64))


# The profile facts ADD reads. A read-only terminal may report other fields
# that change between two reads of an unchanged user (Jhelum's DS-K1T321MFWX);
# they cannot stop the snapshot, and its state fingerprint covers only these.
STABLE_PROFILE_KEYS = ("employeeNo", "name", "userType", "localUIRight")
VOLATILE_FLAG = "hikvision_profile_volatile_fields"
# Profiles whose user commands (firmware profile_command_version 5) verify these
# stable facts instead of the whole record; their snapshots always fingerprint
# them, even when write-enabled.
STABLE_FACT_PROFILES = frozenset({"ds-k1t321mfwx-v4.61.0-20260828-unverified-v1"})


def stable_fingerprint(profile: dict) -> str:
    """The digest the ESP computes over the same facts (sorted, compact, UTF-8)."""
    return hashlib.sha256(json.dumps(_stable(profile), sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _stable(profile: dict) -> dict:
    return {key: profile.get(key) for key in STABLE_PROFILE_KEYS}


def _changed_fields(first: dict, second: dict) -> list[str]:
    """Field names (never values) that differ while the stable facts match.

    Raises when the inventories differ in membership or in a stable fact.
    """
    if set(first) != set(second):
        raise ValueError("HIKVISION_PROFILE_SCAN_CHANGED_MEMBERS")
    fields = set()
    for employee, before in first.items():
        after = second[employee]
        if not isinstance(before, dict) or not isinstance(after, dict) or _stable(before) != _stable(after):
            raise ValueError("HIKVISION_PROFILE_SCAN_CHANGED_FACTS")
        fields |= {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    return sorted(str(key)[:40] for key in fields if str(key).isascii())[:20]


class ProfilePage(BaseModel):
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    terminal_serial: str = Field(min_length=1, max_length=120)
    phase: StrictInt = Field(ge=1, le=2)
    position: StrictInt = Field(ge=0, le=3000)
    total: StrictInt = Field(ge=0, le=3000)
    records: list[str] = Field(max_length=20)


def accept_profile_page(session: Session, connector: Connector, payload: dict) -> dict:
    from zk_add.service import _replace_user_snapshot

    page = ProfilePage.model_validate(payload)
    terminal = connector.zkt_device
    if (connector.firmware_family != "hikvision" or not terminal
            or terminal.confirmed_serial != page.terminal_serial
            or terminal.serial != page.terminal_serial):
        raise ValueError("HIKVISION_PROFILE_BINDING_REQUIRED")
    if page.position + len(page.records) > page.total or (page.total and not page.records):
        raise ValueError("HIKVISION_PROFILE_PAGE_INCOMPLETE")
    if not page.total and page.position:
        raise ValueError("HIKVISION_PROFILE_PAGE_INCOMPLETE")
    digest = hashlib.sha256(page.model_dump_json().encode()).hexdigest()
    # Serialize publication and staging for this connector. SQLite safely ignores
    # FOR UPDATE; production PostgreSQL holds it until custody commits.
    session.refresh(connector, with_for_update={"key_share": True})
    scan = session.get(HikvisionProfileScan, connector.id)
    if scan and scan.snapshot_id == page.snapshot_id and scan.last_page_digest == digest:
        return {"snapshot_id": page.snapshot_id, "published": scan.phase == 3}
    if not scan or scan.snapshot_id != page.snapshot_id:
        if page.phase != 1 or page.position != 0:
            raise ValueError("HIKVISION_PROFILE_SCAN_START_REQUIRED")
        if scan is None:
            scan = HikvisionProfileScan(connector_id=connector.id)
            session.add(scan)
        scan.snapshot_id = page.snapshot_id
        scan.phase = 1
        scan.position = 0
        scan.total = page.total
        scan.data_encrypted = encrypt_json({"first": {}, "second": {}})
    if (scan.phase != page.phase or scan.position != page.position or scan.total != page.total):
        raise ValueError("HIKVISION_PROFILE_SCAN_CHANGED")
    data = decrypt_json(scan.data_encrypted)
    current = data["first" if page.phase == 1 else "second"]
    for raw in page.records:
        if len(raw.encode()) > 4096:
            raise ValueError("HIKVISION_PROFILE_TOO_LARGE")
        profile = json.loads(raw)
        employee = profile.get("employeeNo") if isinstance(profile, dict) else None
        name = profile.get("name") if isinstance(profile, dict) else None
        if (not isinstance(employee, str) or not employee.isascii() or not employee.isdigit()
                or len(employee) > 32 or employee in current
                or not isinstance(name, str) or "\x00" in name or len(name.encode()) > 128):
            raise ValueError("HIKVISION_PROFILE_INVALID_OR_DUPLICATE")
        current[employee] = raw
    scan.position += len(page.records)
    scan.last_page_digest = digest
    published = False
    if scan.position == scan.total:
        if len(current) != scan.total:
            raise ValueError("HIKVISION_PROFILE_INCOMPLETE")
        if page.phase == 1:
            scan.phase = 2
            scan.position = 0
        else:
            first = {key: json.loads(raw) for key, raw in data["first"].items()}
            second = {key: json.loads(raw) for key, raw in data["second"].items()}
            capability = dict(terminal.capability_profile or {})
            stable_profile = ((capability.get("hikvision_health") or {}).get("capability_profile")
                              in STABLE_FACT_PROFILES)
            volatile = capability.get(VOLATILE_FLAG)
            if first != second:
                # A write-qualified exact profile must read back exactly.
                if capability.get("user_write") is True and not stable_profile:
                    raise ValueError("HIKVISION_PROFILE_SCAN_CHANGED")
                fields = _changed_fields(first, second)
                if sorted(set(volatile or []) | set(fields)) != volatile:
                    volatile = sorted(set(volatile or []) | set(fields))[:20]
                    capability[VOLATILE_FLAG] = volatile
                    terminal.capability_profile = capability
                    logger.warning("Hikvision profile fields change between reads for %s: %s",
                                   connector.connector_id, ",".join(volatile))
            users = []
            for employee, profile in second.items():
                raw = data["second"][employee]
                users.append(UserSnapshotRow(
                    uid=employee, user_id=employee, name=profile["name"],
                    privilege=0 if profile.get("userType") == "normal" and
                    profile.get("localUIRight") is False else 14,
                    terminal_identity_fingerprint=hashlib.sha256(
                        f"{page.terminal_serial}\n{employee}".encode()).hexdigest(),
                    terminal_state_fingerprint=stable_fingerprint(profile) if volatile or stable_profile
                    else hashlib.sha256(raw.encode()).hexdigest(),
                ))
            _replace_user_snapshot(session, connector=connector, snapshot=UserSnapshotRequest(
                snapshot_id=page.snapshot_id, complete=True, stable=True,
                reason="HIKVISION_MATCHING_PROFILE_SCANS", observed_at=utc_now(), users=users,
            ))
            scan.phase = 3
            data = {"first": {}, "second": {}}
            published = True
    scan.data_encrypted = encrypt_json(data)
    session.flush()
    return {"snapshot_id": page.snapshot_id, "published": published}
