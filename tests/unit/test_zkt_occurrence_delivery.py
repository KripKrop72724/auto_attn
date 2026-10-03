"""Source custody cannot turn a collapsed legacy UID into distinct deliveries."""
import pytest
from sqlalchemy import select

from test_zkt_custody import custody as custody, observation, batch, count
from zk_add.models import (AttendanceEvent, OrdsOutbox, TerminalSourceEpoch, TerminalRecordManifest,
                           ZktCustodyWork, ZktOccurrenceAlias, ZktObservationLink)
from zk_add.time_utils import utc_now
from zk_add.zkt_custody import settle_observations, source_occurrence_delivery_hold
from zk_add.zkt_custody_work import advance_work


def sources(db, connector, *, shared=True):
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    now = utc_now()
    events = [AttendanceEvent(event_uid=f"original-event-{i}", connector_id=connector.id,
        zkt_device_id=connector.zkt_device.id, device_serial="TEST01", user_id="1007",
        device_event_time=now, captured_at=now, source="LIVE", status="1", punch="0",
        ords_status="ACKED_CHECK", oracle_confirmed_at=now,
        raw_event={"original": True}) for i in range(3)]
    db.add_all(events)
    db.flush()
    db.add_all(OrdsOutbox(attendance_event_id=event.id, status="ACKED_CHECK", acknowledged_at=now)
               for event in events)
    rows = []
    values = []
    for ordinal in range(3):
        value = observation(ordinal + 1, raw_format="SOURCE_RECORD",
                            occurrence={"source_epoch": epoch.epoch_id, "ordinal": ordinal})
        row = TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=ordinal,
            canonical_source=True, raw_record_digest=value["raw_digest"], terminal_record_key=value["raw_digest"],
            occurrence_index=ordinal + 1, disposition="EVENT",
            attendance_event_id=events[0 if shared and ordinal == 1 else ordinal].id)
        db.add(row)
        rows.append(row)
        values.append(value)
    db.commit()
    result = settle_observations(db, connector, batch(*values))
    db.commit()
    return epoch, events, rows, values, result


def test_same_second_distinct_ordinals_with_shared_legacy_event_remain_held(custody):
    db, connector = custody
    _, events, manifests, values, result = sources(db, connector)
    assert len({item["occurrence_id"] for item in result["items"]}) == 3
    assert advance_work(db) == 3
    work = db.scalars(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).all()
    assert [row.state for row in work] == ["HELD_OCCURRENCE", "HELD_OCCURRENCE", "SOURCE_ASSOCIATED"]
    assert all(row.reason_code == "LEGACY_EVENT_SHARED_BY_SOURCE_OCCURRENCES"
               and row.owner == "ADD_RECONCILIATION" and row.next_attempt_at is None for row in work[:2])
    assert count(db, ZktOccurrenceAlias) == count(db, ZktObservationLink) == 3
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 3
    assert [row.event_uid for row in events] == [f"original-event-{i}" for i in range(3)]
    assert all(row.ords_status == "ACKED_CHECK" and row.oracle_confirmed_at and row.raw_event == {"original": True}
               for row in events)
    assert all(row.status == "ACKED_CHECK" for row in db.scalars(select(OrdsOutbox)))
    assert [row.attendance_event_id for row in manifests] == [events[0].id, events[0].id, events[2].id]
    replay = settle_observations(db, connector, batch(values[0]))
    assert replay["items"][0]["receipt_id"] == result["items"][0]["receipt_id"]
    assert replay["items"][0]["occurrence_id"] == result["items"][0]["occurrence_id"]
    assert advance_work(db) == 0


def test_distinct_same_second_attendance_links_do_not_collapse_on_equal_facts(custody):
    db, connector = custody
    _, events, _, _, result = sources(db, connector, shared=False)
    assert len({row.device_event_time for row in events}) == 1
    assert advance_work(db) == 3
    assert all(row.state == "SOURCE_ASSOCIATED" for row in db.scalars(select(ZktCustodyWork)))
    assert all(source_occurrence_delivery_hold(db, connector, item["occurrence_id"]) is None
               for item in result["items"])
    assert result["oracle_completion"] == "NOT_ASSERTED"


@pytest.mark.parametrize("change,reason", [
    ("alias_digest", "SOURCE_OCCURRENCE_LINK_CONFLICT"),
    ("manifest_scope", "SOURCE_OCCURRENCE_LINK_CONFLICT"),
    ("manifest_canonical", "SOURCE_OCCURRENCE_LINK_CONFLICT"),
    ("event_link", "SOURCE_ATTENDANCE_LINK_CHANGED"),
    ("event_scope", "SOURCE_ATTENDANCE_BINDING_UNVERIFIED"),
    ("event_device", "SOURCE_ATTENDANCE_BINDING_UNVERIFIED"),
    ("event_serial", "SOURCE_ATTENDANCE_BINDING_UNVERIFIED"),
    ("event_missing_serial", "SOURCE_ATTENDANCE_BINDING_UNVERIFIED"),
])
def test_stale_or_rebound_delivery_links_cannot_pass_inspection(custody, change, reason):
    db, connector = custody
    _, events, manifests, _, result = sources(db, connector, shared=False)
    alias = db.scalar(select(ZktOccurrenceAlias).order_by(ZktOccurrenceAlias.id))
    if change == "alias_digest":
        alias.raw_digest = "a" * 64
    elif change == "manifest_scope":
        manifests[0].connector_id = connector.id + 1
    elif change == "manifest_canonical":
        manifests[0].canonical_source = False
    elif change == "event_link":
        manifests[0].attendance_event_id = events[1].id
    elif change == "event_scope":
        events[0].connector_id = connector.id + 1
    elif change == "event_device":
        events[0].zkt_device_id = connector.zkt_device.id + 1
    elif change == "event_serial":
        events[0].device_serial = "ANOTHER-TERMINAL"
    else:
        events[0].device_serial = None
    db.flush()
    assert source_occurrence_delivery_hold(db, connector, result["items"][0]["occurrence_id"]) == reason
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 3


def test_recovery_prefix_and_noncanonical_copies_are_not_same_epoch_occurrence_collisions(custody):
    db, connector = custody
    epoch, events, manifests, _, result = sources(db, connector, shared=False)
    recovery = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1,
                                   sequence=2, parent_epoch_id=epoch.id)
    db.add(recovery)
    db.flush()
    for canonical in (True, False):
        db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST01", generation=1, source_epoch_id=recovery.id if canonical else epoch.id,
            ordinal=0 if canonical else 10, canonical_source=canonical,
            source_kind="RECOVERY_PREFIX" if canonical else "HISTORICAL",
            raw_record_digest=manifests[0].raw_record_digest, terminal_record_key=manifests[0].terminal_record_key,
            occurrence_index=1, disposition="EVENT", attendance_event_id=events[0].id))
    db.flush()
    assert source_occurrence_delivery_hold(db, connector, result["items"][0]["occurrence_id"]) is None
    assert advance_work(db) == 3
    assert all(row.state == "SOURCE_ASSOCIATED" for row in db.scalars(select(ZktCustodyWork)))


def test_absent_occurrence_link_is_not_a_positive_delivery_claim(custody):
    db, connector = custody
    assert source_occurrence_delivery_hold(db, connector, "f" * 64) == "SOURCE_OCCURRENCE_LINK_CONFLICT"
