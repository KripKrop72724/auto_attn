#!/usr/bin/env python3
"""Read-only production impact check for the 37 frozen G3 event IDs.

Prints only counts and schema identifiers. Never changes attendance or emits PII.
"""

import json
from sqlalchemy import bindparam, text
from zk_add.db import engine


IDS = (
    370966, 371108, 371589, 371765, 372010, 372527, 372534, 372663,
    373639, 374118, 374126, 374145, 374157, 375522, 376022, 376688,
    385134, 387168, 387515, 387534, 387579, 387611, 387626, 388098,
    388102, 388697, 388709, 389258, 389291, 389737, 389934, 390002,
    390125, 390163, 390180, 390695, 390703,
)
EXPECTED_SERIAL = "PGB1261200074"


def count_matches(connection, table_name, column_name, values):
    quote = connection.dialect.identifier_preparer.quote
    query = text(
        f"select count(*) from {quote(table_name)} where {quote(column_name)} in :values"
    ).bindparams(bindparam("values", expanding=True))
    return connection.scalar(query, {"values": values}) or 0


with engine.connect() as connection:
    with connection.begin():
        connection.execute(text("set transaction read only"))
        rows = connection.execute(
            text("""
                select id, event_uid, user_id, device_serial,
                       oracle_confirmed_at
                  from add_attendance_events
                 where id in :ids
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": IDS},
        ).mappings().all()
        if len(rows) != len(IDS) or {row["id"] for row in rows} != set(IDS):
            raise ValueError("The frozen ADD event set is incomplete")
        if any(
            row["user_id"] != "100"
            or row["device_serial"] != EXPECTED_SERIAL
            or row["oracle_confirmed_at"] is None
            for row in rows
        ):
            raise ValueError("An event's frozen identity or Oracle status changed")
        uids = tuple(row["event_uid"] for row in rows)
        if len(set(uids)) != len(IDS):
            raise ValueError("Frozen event UIDs are not unique")

        fks = connection.execute(text("""
            select con.conrelid::regclass::text as table_name,
                   att.attname as column_name,
                   con.confdeltype as delete_action
              from pg_constraint con
              join unnest(con.conkey) with ordinality as cols(attnum, pos)
                on true
              join pg_attribute att
                on att.attrelid = con.conrelid and att.attnum = cols.attnum
             where con.contype = 'f'
               and con.confrelid = 'add_attendance_events'::regclass
             order by table_name, column_name
        """)).mappings().all()
        uid_columns = connection.execute(text("""
            select table_name, column_name
              from information_schema.columns
             where table_schema = current_schema()
               and table_name like 'add\\_%' escape '\\'
               and column_name like '%event_uid%'
               and table_name <> 'add_attendance_events'
             order by table_name, column_name
        """)).mappings().all()
        references = []
        for row in fks:
            references.append({
                "table": row["table_name"],
                "column": row["column_name"],
                "kind": "foreign_key",
                "delete_action": row["delete_action"],
                "count": count_matches(connection, row["table_name"], row["column_name"], IDS),
            })
        for row in uid_columns:
            references.append({
                "table": row["table_name"],
                "column": row["column_name"],
                "kind": "event_uid",
                "count": count_matches(connection, row["table_name"], row["column_name"], uids),
            })
        triggers = connection.execute(text("""
            select tgname
              from pg_trigger
             where tgrelid = 'add_attendance_events'::regclass
               and not tgisinternal
             order by tgname
        """)).scalars().all()
        historical = connection.execute(
            text("""
                select attendance_event_id, source_epoch_id, generation,
                       source_kind, canonical_source, disposition, ordinal,
                       raw_record_digest, created_at
                  from add_terminal_record_manifest
                 where attendance_event_id in :ids
                 order by attendance_event_id, created_at
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": IDS},
        ).mappings().all()
        epochs = connection.execute(text("""
            select e.id, e.sequence, e.state, e.parent_epoch_id,
                   e.terminal_generation, e.activated_at, e.superseded_at,
                   e.created_at
              from add_terminal_source_epochs e
             where e.id in :ids
             order by e.id
        """).bindparams(bindparam("ids", expanding=True)), {
            "ids": tuple({row["source_epoch_id"] for row in historical if row["source_epoch_id"] is not None}) + (595,)
        }).mappings().all()
        current_digests = set(connection.execute(text("""
            select raw_record_digest from add_terminal_record_manifest
             where source_epoch_id = 595 and canonical_source = true
        """)).scalars().all())
        comparison = connection.execute(text("""
            select source_epoch_id, raw_record_digest
              from add_terminal_record_manifest
             where source_epoch_id in (592, 593, 594, 595)
               and canonical_source = true
        """)).mappings().all()
        event_sources = connection.execute(
            text("""
                select source, count(*) as count
                  from add_attendance_events
                 where id in :ids
                 group by source
                 order by source
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": IDS},
        ).mappings().all()

print("G3_DELETE_PREFLIGHT_JSON=" + json.dumps({
    "frozen_event_count": len(IDS),
    "references": [row for row in references if row["count"]],
    "user_triggers": triggers,
    "event_sources": [dict(row) for row in event_sources],
    "historical_manifest": {
        "linked_event_ids": sorted({row["attendance_event_id"] for row in historical}),
        "canonical_linked_event_ids": sorted({
            row["attendance_event_id"] for row in historical if row["canonical_source"]
        }),
        "epochs": sorted({row["source_epoch_id"] for row in historical if row["source_epoch_id"] is not None}),
        "source_kinds": sorted({row["source_kind"] for row in historical if row["source_kind"] is not None}),
        "dispositions": sorted({row["disposition"] for row in historical if row["disposition"] is not None}),
        "first_created_at": min((row["created_at"] for row in historical), default=None).isoformat() if historical else None,
        "last_created_at": max((row["created_at"] for row in historical), default=None).isoformat() if historical else None,
        "epoch_details": [
            {
                "id": epoch["id"],
                "sequence": epoch["sequence"],
                "state": epoch["state"],
                "parent_epoch_id": epoch["parent_epoch_id"],
                "terminal_generation": epoch["terminal_generation"],
                "activated_at": epoch["activated_at"].isoformat() if epoch["activated_at"] else None,
                "superseded_at": epoch["superseded_at"].isoformat() if epoch["superseded_at"] else None,
                "linked_events": len({row["attendance_event_id"] for row in historical if row["source_epoch_id"] == epoch["id"]}),
                "linked_records": sum(row["source_epoch_id"] == epoch["id"] for row in historical),
                "raw_digest_in_latest": sum(row["source_epoch_id"] == epoch["id"] and row["raw_record_digest"] in current_digests for row in historical),
                "first_ordinal": min((row["ordinal"] for row in historical if row["source_epoch_id"] == epoch["id"]), default=None),
                "last_ordinal": max((row["ordinal"] for row in historical if row["source_epoch_id"] == epoch["id"]), default=None),
            }
            for epoch in epochs
        ],
        "distinct_raw_digests_in_latest": len({row["raw_record_digest"] for row in historical if row["raw_record_digest"] in current_digests}),
        "recent_epoch_source_comparison": [
            {
                "epoch_id": epoch_id,
                "source_records": sum(row["source_epoch_id"] == epoch_id for row in comparison),
                "raw_digest_in_latest": sum(row["source_epoch_id"] == epoch_id and row["raw_record_digest"] in current_digests for row in comparison),
            }
            for epoch_id in (592, 593, 594, 595)
        ],
    },
}, separators=(",", ":")))
