"""Durable raw ISAPI custody; an evidence receipt is not Oracle completion."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.db import Base
from zk_add.crypto import encrypt_text
from zk_add.models import Connector, utc_column
from zk_add.hikvision_protocol import normalize_observation, SourceIdentityError
from zk_add.hikvision_probe import decode_body, ProbeError
from zk_add.hikvision_delivery import deliver_observation


class HikvisionEvidence(Base):
    __tablename__ = "add_hikvision_evidence"
    __table_args__ = (UniqueConstraint("connector_id", "observation_sha256",
                                     name="uq_add_hikvision_evidence_observation"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    terminal_serial: Mapped[str] = mapped_column(String(120))
    source_epoch: Mapped[str] = mapped_column(String(64))
    observation_sha256: Mapped[str] = mapped_column(String(64))
    event_uid: Mapped[str | None] = mapped_column(String(64), index=True)
    immutable_digest: Mapped[str | None] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16))
    disposition: Mapped[str] = mapped_column(String(40), index=True)
    raw_encrypted: Mapped[str] = mapped_column(Text)
    captured_epoch: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[object] = utc_column()


class ObservationIn(BaseModel):
    schema_version: Literal[1]
    source_protocol: Literal["hikvision-isapi-v1"]
    terminal_serial: str = Field(min_length=1, max_length=120)
    source_epoch: str = Field(max_length=64)
    observation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    channel: Literal["STREAM", "HISTORY", "PUSH", "POLL"]
    raw: str = Field(min_length=1, max_length=8192)
    captured_epoch: int = Field(ge=0, le=4_294_967_295)

    @field_validator("raw", "terminal_serial", "source_epoch")
    @classmethod
    def safe_text(cls, value):
        if "\x00" in value:
            raise ValueError("NUL is not valid stored source text")
        return value


def preserve_observation(session: Session, connector: Connector, payload: ObservationIn) -> dict:
    if connector.firmware_family != "hikvision":
        raise ValueError("FIRMWARE_FAMILY_MISMATCH")
    terminal = connector.zkt_device
    if not terminal or terminal.confirmed_serial != payload.terminal_serial:
        raise ValueError("TERMINAL_SERIAL_MISMATCH")
    if hashlib.sha256(payload.raw.encode()).hexdigest() != payload.observation_sha256:
        raise ValueError("SOURCE_OBSERVATION_DIGEST_MISMATCH")
    existing = session.scalar(select(HikvisionEvidence).where(
        HikvisionEvidence.connector_id == connector.id,
        HikvisionEvidence.observation_sha256 == payload.observation_sha256,
    ))
    if existing:
        if existing.terminal_serial != payload.terminal_serial or existing.source_epoch != payload.source_epoch:
            raise ValueError("SOURCE_OBSERVATION_BINDING_CONFLICT")
        if existing.disposition in {"QUALIFICATION_PENDING", "UNCLASSIFIED_SOURCE"} and payload.source_epoch:
            try:
                data = decode_body(payload.raw.encode())
                observation = normalize_observation(data, terminal_serial=payload.terminal_serial,
                                                    source_epoch=payload.source_epoch)
            except (SourceIdentityError, ProbeError, json.JSONDecodeError):
                pass
            else:
                deliver_observation(session, connector, existing, observation, data)
                session.flush()
        return {"observation_sha256": payload.observation_sha256, "durable": True}
    observation = None
    data = None
    disposition = "QUALIFICATION_PENDING"
    try:
        data = decode_body(payload.raw.encode())
        if not payload.source_epoch:
            disposition = "SOURCE_EPOCH_REQUIRED"
        else:
            observation = normalize_observation(data, terminal_serial=payload.terminal_serial,
                                                source_epoch=payload.source_epoch)
    except (SourceIdentityError, ProbeError, json.JSONDecodeError):
        disposition = "UNCLASSIFIED_SOURCE"
    if observation:
        conflicting = session.scalar(select(HikvisionEvidence.id).where(
            HikvisionEvidence.connector_id == connector.id,
            HikvisionEvidence.event_uid == observation.event_uid,
            HikvisionEvidence.immutable_digest != observation.immutable_facts_digest,
        ).limit(1))
        if conflicting:
            disposition = "SOURCE_FACT_CONFLICT"
            for row in session.scalars(select(HikvisionEvidence).where(
                HikvisionEvidence.connector_id == connector.id,
                HikvisionEvidence.event_uid == observation.event_uid,
            )):
                row.disposition = disposition
    evidence = HikvisionEvidence(
        connector_id=connector.id, terminal_serial=payload.terminal_serial,
        source_epoch=payload.source_epoch, observation_sha256=payload.observation_sha256,
        event_uid=observation.event_uid if observation else None,
        immutable_digest=observation.immutable_facts_digest if observation else None,
        source_event_id=str(observation.serial_no) if observation else None,
        channel=payload.channel, disposition=disposition, raw_encrypted=encrypt_text(payload.raw),
        captured_epoch=payload.captured_epoch,
    )
    session.add(evidence)
    session.flush()
    deliver_observation(session, connector, evidence, observation, data)
    session.flush()
    # Caller sends this receipt only after the surrounding DB transaction commits.
    return {"observation_sha256": payload.observation_sha256, "durable": True}
