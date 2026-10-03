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

A corrupt retirement checkpoint, unavailable key or inconsistent segment
identity fails closed without formatting or deleting files. Automatic
checkpoint reconstruction from ADD evidence is not implemented in this stage.
The low-level library cannot establish whether externally missing files were
physically lost. Physical fault qualification remains separate.

The ESP adapter currently owns journal operations only. Catalog and legacy
queue ownership, capture/delivery activation, bridge reader proof,
delivery matching and current-incident recovery must be integrated and tested
before activation. Existing firmware behavior remains gated until then.

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
integration-test switch remains off in release workflows. Runtime startup
still requires persisted reader compatibility; without startup, a writer
build refuses live ACK rather than falling back to legacy delivery.
