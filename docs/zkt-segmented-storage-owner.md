# Retained segmented queues on the ZKT storage task

Exact 2.6.16 and 2.7.0 development images route the existing segmented queue
API through the journal storage task. Append, peek, settlement, depth and
generation requests have no direct fallback when that task is unavailable or
quiescing. The storage task identifies itself through FreeRTOS task identity;
only that task invokes the existing queue implementation directly. Other
firmware versions and Hikvision retain their previous execution path.

The boot/app task initializes and verifies the existing queues before journal
startup. After the owner starts, periodic queue recovery and persistence probes
also run there. Shared admission, queue tokens, encrypted-NVS checkpoints and
the existing segmented file format are unchanged. Old flat-file attendance
queues still require a separate handoff.

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
flat-file ADD/Oracle queues, per-item migration evidence, qualified source
interpretation, signed bridge/candidate and release gates remain open. No
connector is activated or upgraded by this change. Physical qualification is
**NOT_PERFORMED**.
