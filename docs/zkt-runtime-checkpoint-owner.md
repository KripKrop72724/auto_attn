# ZKT runtime checkpoint ownership

Exact ZKT 2.6.16 and 2.7.0 images route cursor, history, scheduled restart and
temporary administrator lease checkpoints through the journal storage task.
The gateway no longer writes `zone_lite/runtime_v1` directly on those versions.
An unavailable or quiescing owner causes a failed save and conservative recovery;
it does not enable a direct-write fallback. Older firmware and Hikvision keep
their existing path. No firmware version or activation gate changes here.

The owner copies the complete request into its bounded mailbox and serializes
it with journal operations under the shared local-storage lock. Checkpoints
cannot consume the three slots reserved for live capture and retirement. They
use the fair background lane; deadline expiry refuses a queued write before
mutation. An NVS operation already executing can exceed the caller's five-second
wait. No task is deleted to enforce that wait. No network request occurs while
the owner holds the storage lock.

The terminal-session caller retains at most one outstanding ticket. A timeout
does not abandon its result or permit another save over unfinished work. A late
successful result updates the last committed cache, even if the subsequent save
fails; it cannot report success for different requested facts. The gateway then
keeps source recovery required and restores the last committed source cursor.
Lease elevation still depends on a successful current save, and clearing a
lease still follows verified terminal revocation. A reentrant caller is refused.

The retained `runtime_v1` format and CRC are unchanged. The owner reads and
validates the entire old blob before writing, assigns the next generation from
actual NVS state, commits, and verifies an exact readback. This avoids reusing a
generation when NVS committed but the caller did not receive success. Missing
previously observed state, invalid CRC/length, an unsupported history schema,
generation regression and generation exhaustion all fail closed. The operation
does not delete, format, fall back to obsolete keys or overwrite corrupt evidence.
Legacy keys can establish the first version-one checkpoint only while that blob
is absent and no version-one generation was previously observed.

Runtime NVS preservation can proceed during journal corruption or a full SPIFFS
partition once the owner has started. Its NVS errors are reported by the owner
separately from attendance filesystem write failures. A runtime save cannot
clear journal recovery requirements. Quiescence refuses new requests and drains
all accepted checkpoint work, including a request whose caller abandoned its
reply, before acknowledging completion.

Native and Linux host tests exercise the actual checkpoint adapter, owner
thread and gateway save path. They inject failed open/read/set/commit/readback,
a commit followed by an error, changed readback, corrupted/short checkpoints,
missing state, exhausted generations, expired requests, lost polling, repeated
timeouts, reentry, full storage, a corrupt journal and shutdown with queued work.
The tests also verify that a new-image save cannot fall back to direct NVS.

This is one storage-ownership component. The [catalog owner handoff](zkt-bounded-file-transactions.md) is implemented in
gated builds. Legacy queue/command handoff, corrupt-lease evidence recovery, enrollment reuse, physical power-cut
testing, firmware resource/latency qualification and signed bridge qualification
remain separate release requirements. Physical qualification is **NOT_PERFORMED**.
