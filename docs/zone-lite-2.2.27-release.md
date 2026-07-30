# Zone Lite 2.2.27 corrective release

## Purpose

Zone Lite 2.2.27 makes the verified ADD identity catalog durable on terminals
with large identity histories. The catalog is now committed as an atomic,
encrypted record stream instead of creating a second catalog-sized plaintext
and ciphertext copy in internal heap.

## Safety contract

- The previously committed catalog remains intact until every encrypted row of
  the replacement catalog has been written, flushed, synced, and atomically
  renamed.
- Boot health remains fail-closed until the fresh catalog is committed and its
  generation is observed by the runtime.
- Legacy single-object encrypted catalogs remain readable and are migrated on
  the next catalog update or local tombstone mutation.
- Lookup is bounded to one encrypted identity row at a time.
- A failed catalog commit is surfaced as
  `IDENTITY_CATALOG_PERSIST_FAILED`; no partial catalog is activated.

## Rollout

1. Build the exact green main commit as an immutable HIL-only candidate.
2. OTA only `ZONE-SWAT-01` and require the normal boot, catalog, truth, and
   Oracle-delivery gates.
3. Promote the exact SWAT-tested bytes.
4. Re-run the large-catalog proof on `ZONE-PESHAWAR-06`; require 731 users and
   at least 88,147 terminal punches after boot.
5. Continue one zone at a time only after each device is
   `ONLINE`, `OTA_READY`, `CERTIFIED`, snapshot-complete, and free of unsafe or
   retrying Oracle delivery.
