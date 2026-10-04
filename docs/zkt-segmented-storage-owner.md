# Retained segmented queues on the ZKT storage task

Boot mounts SPIFFS at `/storage`. The journal runtime and legacy OTA evidence
scan now share that actual mount constant. Their earlier development prefix,
`/spiffs`, was never mounted; native component tests using temporary directories
did not expose the startup failure. A regression executes the production boot
initialization and runtime adapter, comparing the journal, OTA, command and
catalog paths to the captured VFS mount. No partition or retained filename is
changed by this correction, and no candidate with the old prefix was qualified.

Exact 2.6.16 and 2.7.0 development images route the existing segmented queue
API through the journal storage task. Append, peek, settlement, depth and
generation requests have no direct fallback when that task is unavailable or
quiescing. The storage task identifies itself through FreeRTOS task identity;
only that task invokes the existing queue implementation directly. Other
firmware versions and Hikvision retain their previous execution path.

The boot/app task initializes and verifies the existing queues before journal
startup. After the owner starts, periodic queue recovery and persistence probes
also run there. Shared admission, queue tokens, encrypted-NVS checkpoints and
the existing segmented file format are unchanged. The [ADD flat-file handoff](zkt-add-legacy-storage-owner.md)
uses the same copied transport. Oracle/blocked flat files still require handoff.

An owner recovery request audits at most one retained record from one lane.
Lanes rotate even when one fails; live requests can run between these steps.
Unchanged completed audits reuse their verified result. A changed checkpoint
generation invalidates that evidence and requires another audit. Bootstrap
retains its existing bounded pass across the lanes.

An eight-kilobyte record uses two fixed owner buffers allocated in PSRAM, one
for append assembly and one for read snapshots. Each mailbox request or reply
copies at most 512 payload bytes. Buffers never reference caller memory. A
monotonic transfer identity binds chunks to their lane, policy and absolute
deadline; incomplete or replaced RAM assemblies cannot become durable success.
Only the final queue append result establishes persistence. The owner consumes
the assembly before attempting the append, including when its result is
uncertain. A complete replay may duplicate preserved evidence; ADD's existing
record identities must still settle it safely.

Append, read and metadata/retirement clients each retain at most one unfinished
reply. A new operation first collects that reply and then performs its own
request; it cannot inherit an earlier record's success, absence or token.
Client calls have a ten-second total allowance. An already executing file/NVS
operation can finish after that allowance; its result remains collectable.
Starting an expired operation is refused. RAM admission and copied reads do
not authorize retirement. Existing callers still verify the matching ADD
custody or Oracle disposition before submitting their exact queue token, and
the queue revalidates that token before checkpointing.

Mailbox admission preserves the three live-journal slots. Live append and
retirement keep their existing priority and bounded fairness. Queue requests
cannot hold a mailbox or filesystem lock over a network call. Owner quiescence
finishes accepted work and rejects new requests, including a late producer's
final append request. An unfinished RAM producer has never received durable
acceptance and remains a caller recovery obligation.

Host validation exercises real segmented files/checkpoints, the actual queue
entry points and the actual owner thread. It covers maximum-size records,
capacity refusal, failed retirement checkpoints, wrong/replayed tokens, lost
append and retirement replies, concurrent caller refusal, partial producers,
changed transfer identities, deadlines and counter exhaustion. Existing queue
fault tests continue covering file and NVS failures. These results do not
qualify ESP flash latency, stack/PSRAM headroom or physical interruption.

This is an ownership component, not a completed legacy migration. The
flat-file Oracle queues, per-item migration evidence, qualified source
interpretation, signed bridge/candidate and release gates remain open. No
connector is activated or upgraded by this change. Physical qualification is
**NOT_PERFORMED**.
