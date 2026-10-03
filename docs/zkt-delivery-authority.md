# ZKT delivery authority and bridge capture

Exact 2.7.0 images transfer new-capture responsibility to the compact journal
only after the storage owner proves the exact compatible rollback reader.
The owner commits and reads back an irreversible authority bit in the existing
encrypted journal root before allocating capture sequences. Commit errors and
unavailable readback revoke permission until root recovery; retries reuse the
committed state. No network call occurs within this operation.

The initial 2.6.16 bridge retains legacy capture only after its actual local
reader and authority state are verified. It cannot initiate cutover. Once ADD
authority is persisted, rollback to the validated bridge starts the same raw
capture and journal-delivery path as the writer. Unknown, stale or regressed
authority never enables legacy fallback. A pending bridge proves local reader
and transport operation before OTA health confirmation, then attests its
validated image before enabling capture. A build without raw capture support
cannot attest a compatible bridge or pass the new images' local boot gate.

The reader proof requires capability mask `0x3f`, including the new authority
and bridge-capture capability. Earlier development proofs are incompatible.
The root size, journal encryption identities, immutable record format,
retirement format and partition layout do not change. No published bridge or
writer artifact existed when this contract was introduced.

Live routing follows runtime authority instead of only a compile flag. After
cutover, raw packet persistence precedes protocol acknowledgement and the old
live decoder/enqueue path is not called. Source history emits raw custody
evidence, and legacy full-history sweeps cannot create new Oracle queue items.
Retained legacy queues and their existing identities remain available to their
existing readers; their complete handoff and receipt-bound migration are still
required. Hikvision and existing firmware versions retain their prior paths.

Diagnostics expose actual authority separately from reader/writer permission.
ADD accepts bridge validation, quiescence and authority-hold phases; missing or
stale ownership is displayed as unverified. New journal runtime declarations
cannot pass HIL using the old ESP Oracle-worker contract. Full journal worker,
queue and migration telemetry qualification remains open.

Verification exercises actual NVS/root, owner-thread, boot/runtime and live
dispatch code with sanitizers. Faults include commit before/after failure,
unavailable readback, corrupt authority, lost readiness, disabled capture,
bridge reboot/rollback and legacy/Hikvision isolation. Independent byte fixtures
bind the reader capability mask. These are software tests, not physical power
or flash-endurance qualification.

This change does not activate a connector, publish firmware or qualify the
release. Full storage ownership, migration, profile-qualified interpretation,
Oracle verification, capacity, signed builds and the planned HIL/soak gates
remain prerequisites. Physical qualification is **NOT_PERFORMED**.
