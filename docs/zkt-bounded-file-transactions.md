# Bounded catalog transaction component

The existing synchronous catalog/command replacement API remains unchanged.
`ft_work_begin` and `ft_work_step` provide an additional path for the single
storage owner: verification reads at most 4,096 bytes in a call and closes every
file before returning. The owner can schedule attendance work between calls.
Exact bridge and writer images route catalog staging, activation, deletion and
transaction recovery through this owner. Legacy firmware and Hikvision retain
their existing path.

The caller owns the state and must serialize every mutation of the three
distinct active, staging and backup paths until the operation finishes or is
recovered. Paths are copied into bounded storage. No network call, heap
allocation or retained file handle belongs to the state machine. Other files
can be used between steps. This does not authorize changing a partially scanned
candidate, sharing the same paths with another producer, or accepting a partial
verification as complete.

Replacement flushes, syncs, closes and verifies the candidate before committing
the prepared generation. It then preserves the old active file as a backup,
activates the candidate, commits the new generation and verifies it before
reclamation. Missing, altered or ambiguous generations remain failures with
their operation and captured error. Nonempty, shortened and empty candidates
must match their exact committed length and digest. A partial or uncertain NVS
commit cannot report completion.

The NVS transaction format remains version 1 with the same prepared/committed
phases. An unchanged `ft_recover` reader can recover a process interrupted after
any step. A first uncommitted stage still requires an explicit replacement
intent; recovery alone cannot promote it. Interrupted scans restart from
preserved bytes rather than trusting a volatile partial digest.

Host fault tests interrupt every step, inject each instrumented I/O boundary
and both outcomes of failed NVS commits, and then run the unchanged reader.
They assert a recoverable generation, a maximum read size per step and no open
handle after returning. These are software recovery tests. A filesystem or NVS
primitive may still stall, including during SPIFFS garbage collection; the
component does not establish a flash-latency percentile or power-cut guarantee.

The owner's mailbox keeps the original catalog ticket queued between steps.
Live appends and receipt checkpoints retain priority; later catalog mutations
cannot overtake the transaction. Delivery reads and runtime checkpoints also
receive turns between catalog steps, so the oldest catalog ticket cannot
monopolize lower-priority work. Each producer gets a boot-local generation
token and exact append offset. Writes copy at most 512 encrypted bytes per
request; partial writes, flush/sync/close failures, altered lengths and stale
tokens prevent activation. The mailbox still reserves three slots for live work.
Resetting a retained optional producer can reclaim its space at the write
ceiling; a new file still requires metadata admission. Failed producer cleanup
keeps the original failure reason. Optional catalog admission or I/O failure
does not mark attendance persistence
as failed. Its result contains the captured operation/error.

A command deadline is checked before its first operation. Accepted recovery or
activation continues after the caller's wait expires, including after its reply
is abandoned. The connector retains at most one unfinished reply, defers further
catalog use until it is collected, and invalidates obsolete RAM aliases when an
activation may have changed the active file. The regular supervisor collects
late completions. Catalog requests never keep a filesystem handle or admission
lock across a caller wait. The initial catalog restore can defer until the owner
has started; the startup path cannot write catalog NVS independently.

Catalog semantic readers still execute under the connector's catalog lock after
an owner recovery/read barrier. They do not mutate files, and a pending owner
activation blocks those reads. Moving the remaining legacy attendance queues
and command persistence to the owner, measuring catalog latency under flash
pressure, and qualifying all rollback/capacity paths remain open. No bridge or
writer release is enabled by this implementation.
