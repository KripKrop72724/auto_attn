# ZKT compact journal v1

Implementation status: component development, not an enabled writer or qualified
release. The bridge, transport, semantic occurrence matcher and per-device gates
must be complete before production activation. Physical interruption and flash
endurance tests are **NOT_PERFORMED**.

## Identities and cryptography

The byte protocol is little endian and does not persist native C structures.
A dedicated 32-byte master and 16-byte capture epoch live in encrypted NVS.
They do not change with transport credentials. A validated terminal identifier
is bound to that state; changing the terminal cannot silently rebind retained
observations. Missing or corrupt state never causes automatic key replacement
when journal files or retirement state exist.

HKDF-SHA256 derives an AES-256-GCM key with the capture epoch as salt and the
ASCII domain `ZKT-ATTENDANCE-JOURNAL-AES256GCM-V1` followed by the zero-padded
81-byte terminal identifier as information. Each 12-byte nonce is `ZJ01`
followed by the 64-bit capture sequence. The complete metadata and record
header are authenticated associated data. The authentication tag is 16 bytes.

Sequence allocation commits and reads back an exclusive upper bound before
using any sequence in its reserved block of 256. Reboot burns unused values.
An uncertain reservation poisons the allocator until persisted state is read
again. Exhaustion is refused. Segment identities consume the same allocator.
The dedicated key remains stable while the epoch and terminal form its key
derivation scope. It must never be regenerated merely to clear a health fault.

## Files and record layout

Segment names contain a fixed prefix, 16 lowercase hexadecimal identity digits
and `.j`. Files are created exclusively and never overwritten on recovery.
Segments are at most 64 KiB; the in-memory index holds at most 256 segments.
The existing partition and shared storage admission budget still apply.

The 240-byte segment metadata contains a format marker/version, segment
identity, capture epoch, terminal serial, decoder profile and decoder version.
Canonical zero padding is checked. Its immutable bytes authenticate every
observation in that segment.

| Record portion | Bytes | Contents |
|---|---:|---|
| Authenticated plaintext header | 24 | Marker, exact length, raw format, time quality, sequence, original encoded time, raw length |
| Encrypted facts | 40 | Capture UTC seconds, monotonic uptime, optional source epoch/ordinal, identity revision |
| Encrypted source evidence | 1–512 | Original raw observation bytes |
| Authentication tag | 16 | AES-GCM tag |

A 40-byte source observation occupies 120 bytes before segment/filesystem
overhead. Names and CNIC values are not repeated as extra JSON fields; original
source bytes remain encrypted even when they contain identifiers. This size
is not a seven-day capacity certificate. Measure real workload, filesystem
overhead, existing queues and all reserves for each device.

## Durability and retirement

The storage owner synchronizes and closes an append before returning durable
success. No network operation runs under a storage lock. Capture tasks submit
copies to a bounded mailbox; RAM admission is not preservation. The caller
must not acknowledge terminal custody before durable success or a separately
committed and explicit recovery obligation.

Delivery peeks do not retire data. A token includes the segment, exact byte
extent, sequence and SHA-256 of the bytes. Settlement re-reads the item and
rejects a changed/stale token. The websocket adapter correlates the pending
message ID and checks the typed, committed per-item receipt, observation ID,
payload digest and custody disposition. Generic ACKs cannot authorize retirement.
Before changing its checkpoint, the storage task reconstructs the current
item's canonical payload and compares its identity/digest with that receipt's
expected item. The low-level file library still relies on its trusted caller
to provide this verified proof.

An 80-byte retirement checkpoint contains its revision, position, last settled
sequence and ADD receipt digest, protected against accidental corruption by
CRC32 inside encrypted NVS. It commits and reads back before the reader
advances. Uncertain commits force reloading persisted state. A lost response
can replay the same capture, never invent a replacement identity.

At most one completely settled segment is reclaimed per maintenance request.
Live writes never rewrite an entire outbox. Startup inventories files once;
an empty queue read does not rescan the filesystem. Admission for new writes
does not block reads, retirement or reclamation when space is full.

## ADD wire contract

Each request carries one immutable item in `zkt_observation_batch` schema 1.
The firmware writes JSON keys in canonical order and verifies that cJSON's
parse/print round trip preserves them. Capture sequence, segment identity,
capture seconds and monotonic milliseconds use decimal strings to avoid
binary64 precision loss. Source ordinals and encoded terminal time are bounded
32-bit numbers. An out-of-range civil capture time retains its original seconds
with a null formatted time; it does not replace the terminal's original value.

`JOURNAL_EXCEPTION` items identify the exact retained segment extent and raw
digest. ADD encrypts and receipts those opaque bytes with an explicit exception
reason. They never become inferred attendance. Ordinary observations and
exceptions share transactional replay-stable custody; source matching and Oracle
completion remain separate.

Retirement proof is SHA-256 of canonical JSON containing the domain
`zkt-add-custody-v1`, observation ID, payload digest, receipt UUID and custody
disposition. It binds the committed ADD receipt to the local item. It is not an
Oracle completion certificate. Independent Python/C fixtures cover 64-bit
values, all base64 padding cases, raw source references and opaque extents.
The actual socket dispatcher is tested against delayed/duplicate replies,
generic ACKs, mismatched content and allocation failures.

## Recovery boundaries

Interrupted appends leave their bytes in place and start a fresh writer segment.
Complete authenticated records remain readable after reboot even if the caller
did not receive local success. Invalid framing, truncated tails and failed
authentication become bounded opaque evidence items. Candidate framing is
re-authenticated before interpretation; malformed earlier bytes cannot hide a
later valid record. Opaque evidence needs an explicit committed ADD receipt
before it can be reclaimed.

A corrupt or impossible retirement checkpoint first becomes an immutable
88-byte evidence segment (`ZJCPE001` followed by the original 80 bytes). Only
after synchronization and close succeed can the owner commit a cursor that
replays from the earliest retained segment. Replay keeps original capture
identities. A restart between those steps reuses and synchronizes the complete
evidence segment; partial evidence files stay preserved. The evidence has its
own `CHECKPOINT` custody exception, and recovery stays pending until its ADD
receipt has committed. Recovery writes use the shared capacity reserve.

An unavailable checkpoint read, unavailable/corrupt key or counter, inconsistent
segment identity, or insufficient evidence capacity still fails closed without
formatting or deleting retained files. No key or nonce is reconstructed. This
procedure replays retained bytes; it does not reconstruct externally lost bytes
or infer Oracle completion from a checkpoint.
The low-level library cannot establish whether externally missing files were
physically lost. Physical fault qualification remains separate.

The ESP adapter currently owns journal operations only. Catalog and legacy
queue ownership, capture/delivery qualification, signed bridge integration,
delivery matching and current-incident recovery must be integrated and tested
before activation. Existing firmware behavior remains gated until then.

## Durable reader capability

The owner refuses `APPEND` until its bounded `READER_CHECK` operation confirms
compatibility. Reads, committed custody settlement and checkpoint recovery do
not require writer authorization. The check uses this boot's opened journal,
recent transport progress and persistence health; storage recovery invalidates
the cached writer permission. The check does not recursively acquire the shared
filesystem mutex or hold one across a network operation.

A 192-byte, little-endian `ZJREAD01` blob in encrypted NVS binds the validated
2.6.16 bridge's application digest and OTA slot to the terminal digest, journal
capture epoch, canonical partition-layout digest and supported journal/root/
retirement formats. It includes a monotonic generation, exact capability bits,
canonical zero padding and CRC32. The platform adapter reads actual ESP image
digests, partitions, secure-boot state and OTA validation; remote declarations
cannot supply them. The blob contains no encryption key or attendance bytes.

The bridge commits and reads back the exact proof. Repeating an unchanged proof
does not write flash. An unavailable, damaged or rebound proof cannot be
silently replaced. Existing proof also prevents a missing encryption root from
being regenerated, even when no segment remains. Generation exhaustion refuses
renewal. CRC covers accidental corruption; secure boot and encrypted NVS are
the trust boundary, not a claim that CRC is authentication.

A 2.7.0 writer requires that exact validated bridge in a separate, nonoverlapping
OTA slot with matching terminal, epoch and storage layout. An unconfirmed bridge,
factory slot, changed image or unknown format refuses writing. Attesting the
bridge does not enable its journal writer. These are local compatibility checks;
they do not establish model correctness, migration completion, seven-day
capacity, signed artifact qualification or HIL acceptance. The gated boot
controller and pre-erase install interlock are implemented; complete bridge
migration and an operational compatible-rollback path remain release blockers.

Every fresh or resumed ZKT OTA checks compatibility before opening its download.
The only journal-preserving install edge currently allowed is a validated,
attested 2.6.16 bridge installing exact 2.7.0 into the other OTA slot. The storage
owner rereads the proof and actual current image, layout, terminal and root
epoch, with local reader, transport and persistence checks. This read-only check
cannot create proof, clear an incident or grant writer permission. The download
starts after all local locks are released. A running 2.7.0 refuses to erase its
certified bridge, including an attempted reinstall of that bridge; selecting an
already-verified rollback image without erasing it needs a separate operation.
Future versions require an explicit, qualified compatibility transition.

The OTA caller waits at most five seconds for the bounded owner operation.
Accepted checks can finish after timeout, but their results cannot authorize a
later assignment. Failed reply abandonment retains one ticket until it can be
released; retries cannot fill the mailbox. OTA checks have no access to the
three slots reserved for live capture/retirement. Legacy versions refuse direct
2.7.0 installation and reject any retained journal namespace or orphaned journal
file. Legacy absence checks scan at most 1,024 directory entries, hold only the
local storage mutex, and refuse on unavailable or uncertain reads. Hikvision
does not call this ZKT policy.

The app task starts the reader only for exact 2.6.16/2.7.0 ZKT application
identities, a provisioned binding, secure boot, encrypted NVS and an OTA slot.
Legacy versions and Hikvision do not activate it. Failed constructors retry
with two-to-sixty-second backoff; a successful start is never retried merely
because its health snapshot is unavailable. Delivery starts during checkpoint
recovery so the archived checkpoint can obtain its custody receipt. Reader
proof requests have five-second caller deadlines and retain accepted work.

A pending bridge proves local reader operation before OTA validation; after
validation it can persist its bridge attestation. Only a writer build with a
successful compatible-reader check starts raw capture. Recovery clears the
owner's permission, and each packet also checks the runtime's current gate.
Changed bindings stay held until reboot. Missing/stale worker evidence and
storage operations exceeding fifteen seconds block local boot health without
killing or restarting a possible lock owner. These are software guard bounds,
not measured ESP latency or hardware qualification.

Diagnostics retain startup phase, independent reader/writer readiness, actual
worker starts, attempts and reader-check results. ADD displays their evidence
age against the parent boot/sample, including 32-bit uptime wrap. A local
startup pass cannot assert source coverage or Oracle completion.

## Local bridge selection component

The storage owner accepts `ZJ_SELECT_READER` with an exact expected bridge
application digest and a local monotonic deadline no more than five seconds
away. The ESP adapter verifies the current writer, retained reader attestation,
terminal/key epoch, partition layout, actual bridge hash, application family,
version and OTA state. It then selects the existing slot and reads back boot
selection. It never downloads or writes an application partition.

The target build configuration uses secure boot and encrypted NVS with
anti-rollback disabled. Selection refuses anti-rollback builds because the
pinned ESP-IDF setter can erase an image rejected by its security-version check.
No security configuration or eFuse is changed by this operation.

Selecting a validated bridge makes its OTA state `NEW`. A retry may recognize
that same selected-but-unbooted image only after rechecking the complete
attestation and expected hash. It does not rewrite boot selection or grant
writer permission. `PENDING_VERIFY`, `INVALID`, `ABORTED`, unknown state, changed
proof and wrong boot slot all remain holds. Failed selection or readback is
reported as uncertain. Both success and uncertainty revoke cached writer
permission; a late mailbox request whose deadline expired cannot select a slot.

This is an internal component, with no remotely callable operation yet. Before
using it, the coordinator must persist the approved exact-artifact intent,
serialize with OTA/configuration changes, retain uncertain outcomes, reach a
capture safepoint and verify the resulting boot. The ADD campaign interface,
durable coordinator and field qualification remain incomplete. No device has
been rolled back by this implementation work.

Verification: all 190 firmware regressions passed, along with seven targeted
Linux GCC ASan/UBSan tests and both unsigned ESP-IDF family builds. Fault cases
cover missing/changed proof, changed terminal identity, wrong digest/slot,
expired requests, malformed descriptors, rejected/unconfirmed images,
anti-rollback configuration, selection before/after a lost response, failed
readback, and owner permission invalidation. Physical interruption of otadata
writes and field rollback remain unperformed.

## Raw live packet capture

Raw formats 4 (`LIVE_PACKET`) and 5 (`PACKET_FRAGMENT`) preserve the full ZKT
header and payload before semantic interpretation. Format 4 contains up to
512 source bytes. Larger packets, bounded to 65,536 bytes, use a 60-byte fragment
header: `ZJF1`, a 16-byte random group ID, little-endian 32-bit total length and
offset, and the complete packet's 32-byte SHA-256. Each chunk carries at most
452 source bytes. Fragment grouping also requires the authenticated connector,
terminal identity and capture epoch; the random group ID alone is insufficient.

All fragments must durably commit before a protocol ACK. ADD must have every
nonconflicting extent and verify the complete digest before interpreting a
packet. Partial captures remain explicit evidence; they cannot become partial
attendance. A timeout preserves accepted owner work and withholds ACK. The
capture caller retains an unreleased reply ticket until the owner accepts
abandonment, preventing repeated timeouts from leaking request slots.

The capture call has a 15-second deadline, with a five-second deadline per
owner append, and uses no ADD/Oracle network call. These are recovery bounds,
not measured live-latency guarantees. The normal and both interleaved protocol
paths call the capture hook before ACK under `ZONE_LITE_JOURNAL_WRITES`. This
integration-test switch remains off in release workflows. Raw writer startup
requires persisted reader compatibility and the runtime permission must still
be fresh for each packet. An unready writer refuses live ACK without silently
falling back to legacy delivery.
