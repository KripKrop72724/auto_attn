# Bounded catalog transaction component

The existing synchronous catalog/command replacement API remains unchanged.
`ft_work_begin` and `ft_work_step` provide an additional path for the single
storage owner: verification reads at most 4,096 bytes in a call and closes every
file before returning. The owner can schedule attendance work between calls.
This component is not yet wired into catalog production or activation.

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

The remaining integration must give catalog writes/activation/recovery one
owner, preserve the caller's timeout and retained-operation identity, prioritize
live append/receipt work, and report optional catalog refusals independently.
No bridge or writer release is enabled by this component.
