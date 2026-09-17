import hashlib
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector, ZKTDevice
from zk_add.crypto import decrypt_text
from zk_add.settings import settings
from zk_add.hikvision_evidence import HikvisionEvidence, ObservationIn, preserve_observation


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-only-hikvision-lookup-key")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        connector = Connector(connector_id="h", hardware_id="00:11:22:33:44:55",
                              zone_id="z", zone_name="z", device_id="d", display_name="d",
                              firmware_family="hikvision", terminal_vendor="hikvision",
                              terminal_protocol="isapi")
        session.add(connector)
        session.flush()
        connector.zkt_device = ZKTDevice(connector_id=connector.id, serial="terminal",
                                         confirmed_serial="terminal")
        session.commit()
        yield session, connector
    engine.dispose()


def observation(**changes):
    raw = json.dumps({"serialNo": 123, "major": 5, "minor": 75,
                      "employeeNoString": "00111", "time": "2026-09-17T10:00:00+05:00",
                      **changes})
    return ObservationIn(schema_version=1, source_protocol="hikvision-isapi-v1",
                         terminal_serial="terminal", source_epoch="verified-epoch",
                         observation_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                         channel="HISTORY", raw=raw, captured_epoch=1790000000)


def test_custody_is_encrypted_idempotent_and_not_attendance_release(db):
    session, connector = db
    payload = observation()
    receipt = preserve_observation(session, connector, payload)
    session.commit()
    assert receipt["durable"] is True
    assert preserve_observation(session, connector, payload) == receipt
    assert session.scalar(select(func.count()).select_from(HikvisionEvidence)) == 1
    evidence = session.scalar(select(HikvisionEvidence))
    assert evidence.disposition == "QUALIFICATION_PENDING"
    assert evidence.raw_encrypted != payload.raw
    assert decrypt_text(evidence.raw_encrypted) == payload.raw


def test_conflicting_facts_preserve_both_observations_and_hold_both(db):
    session, connector = db
    preserve_observation(session, connector, observation())
    preserve_observation(session, connector, observation(employeeNoString="different"))
    session.flush()
    rows = list(session.scalars(select(HikvisionEvidence)))
    assert len(rows) == 2
    assert rows[0].event_uid == rows[1].event_uid
    assert {r.disposition for r in rows} == {"SOURCE_FACT_CONFLICT"}


@pytest.mark.parametrize("change", ["family", "serial", "digest"])
def test_bad_binding_never_receives_custody_receipt(db, change):
    session, connector = db
    payload = observation()
    if change == "family":
        connector.firmware_family = "zkt"
    elif change == "serial":
        payload.terminal_serial = "replacement"
    else:
        payload.observation_sha256 = "0" * 64
    with pytest.raises(ValueError):
        preserve_observation(session, connector, payload)
    assert session.scalar(select(func.count()).select_from(HikvisionEvidence)) == 0
