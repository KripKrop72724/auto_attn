# ADD flat-file queues on the storage task

Exact 2.6.16/2.7.0 development images now send ADD live and historical flat-file
appends, reads, generation restoration, retirement and their existing encrypted
NVS checkpoints through the storage task. Both single-row producers and the
bulk producer use the copied queue transport. The delivery task verifies its
existing ADD acknowledgement or exact raw-evidence receipt before submitting a
retirement token. Network calls run after owner operations release all locks.

The owner reuses the two existing 8 KiB PSRAM transfer buffers. Queue domain,
lane, transfer identity and absolute deadline bind each 512-byte copy; a
segmented-queue transfer cannot become a flat-file transfer. Unfinished replies
remain bounded and must be collected before the next caller's request. No
direct filesystem fallback is permitted when the owner is unavailable or
quiescing. Only the storage task can enter the production flat-file adapters.

Startup defers flat-file recovery to that owner. A request verifies at most
8 KiB of an already consumed prefix before yielding; another queue can make
progress between requests. No whole-file row-count scan is added. Counts for
restored nonempty files remain unknown until established by drainage, and
telemetry reads the owner's cached byte measurement. Once all retained file
generations are verified empty, repeated peeks perform no file read or stat.
Every append attempt that can modify a file invalidates this empty evidence,
including short writes and failures in flush, sync or close.

Active, backup and temporary generations retain their original names and are
drained separately. Reclamation first commits the existing safe retirement
checkpoint. A failed restore after the old active file is removed leaves a
recovery obligation; the next operation retries restoration before empty can
be trusted. Incomplete tails remain readable as raw evidence and cannot be
treated as attendance. Queue limits and shared admission continue applying.
Bulk producers submit one bounded row at a time, so a partially accepted batch
may safely replay its existing event IDs; this component makes no throughput
qualification claim.

Other ZKT versions and Hikvision retain their existing delivery and producer
paths. Host tests execute the actual adapters with real flat files and injected
short-write, flush, sync, close, cursor-rename, generation-restore, stat and NVS
failures. They cover identical retained rows, all surviving generations,
maximum records, stale tokens, lost replies, empty caching, lane independence,
and bounded prefix recovery. Separate tests execute the production routing in
both family modes and dispatch these copied operations on the actual owner
thread, including refusal before startup and after quiescence.

This change completes ADD flat-file ownership, not the release's complete
legacy handoff or migration proof. Subsequent owner adapters also cover
Oracle/blocked flat files and retained quarantine generations. Per-item
migration records, source-profile qualification, compatible signed images,
ESP capacity/latency qualification and field acceptance remain open. Physical
power interruption and endurance qualification are **NOT_PERFORMED**.
