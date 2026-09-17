"""Publish profile inventories only after two complete, matching bounded scans."""
import hashlib
import json

from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.db import Base
from zk_add.models import Connector
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.time_utils import utc_now


class HikvisionProfileScan(Base):
    __tablename__ = "add_hikvision_profile_scans"
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(32))
    phase: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    data_encrypted: Mapped[str] = mapped_column(Text)
    last_page_digest: Mapped[str] = mapped_column(String(64))


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
    session.refresh(connector, with_for_update=True)
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
            if first != second:
                raise ValueError("HIKVISION_PROFILE_SCAN_CHANGED")
            users = []
            for employee, profile in second.items():
                raw = data["second"][employee]
                users.append(UserSnapshotRow(
                    uid=employee, user_id=employee, name=profile["name"],
                    privilege=0 if profile.get("userType") == "normal" and
                    profile.get("localUIRight") is False else 14,
                    terminal_identity_fingerprint=hashlib.sha256(
                        f"{page.terminal_serial}\n{employee}".encode()).hexdigest(),
                    terminal_state_fingerprint=hashlib.sha256(raw.encode()).hexdigest(),
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
