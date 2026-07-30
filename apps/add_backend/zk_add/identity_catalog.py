from __future__ import annotations

from uuid import uuid4


IDENTITY_CATALOG_CHUNK_ROWS = 64


def identity_catalog_messages(catalog: dict) -> list[dict]:
    rows = list(catalog.get("rows") or [])
    catalog_id = uuid4().hex
    messages = [
        {
            "schema_version": "3",
            "type": "identity_catalog_begin",
            "catalog_id": catalog_id,
            "rows_count": len(rows),
        }
    ]
    for offset in range(0, len(rows), IDENTITY_CATALOG_CHUNK_ROWS):
        messages.append(
            {
                "schema_version": "3",
                "type": "identity_catalog_chunk",
                "catalog_id": catalog_id,
                "offset": offset,
                "rows": rows[offset : offset + IDENTITY_CATALOG_CHUNK_ROWS],
            }
        )
    messages.append(
        {
            "schema_version": "3",
            "type": "identity_catalog_commit",
            "catalog_id": catalog_id,
            "rows_count": len(rows),
        }
    )
    return messages
