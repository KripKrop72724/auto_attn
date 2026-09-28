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

print("G3_DELETE_PREFLIGHT_JSON=" + json.dumps({
    "frozen_event_count": len(IDS),
    "references": [row for row in references if row["count"]],
    "user_triggers": triggers,
}, separators=(",", ":")))
