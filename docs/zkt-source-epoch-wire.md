# ZKT source epoch wire binding

A source epoch can change while the terminal's numeric generation remains the
same. `source_epoch` now carries the existing canonical lowercase UUID from
`add_terminal_source_epochs.epoch_id`. It is separate from the integer database
`source_epoch_id`; no old identifiers, digests or stored checkpoint formats change.

ADD includes the UUID in reconciliation and source-probe assignments, coverage
bootstrap/recovery messages, and committed source acknowledgements. Anchors,
chunks, manifests, probes, credit releases and tail chunks echo it. ADD requires
it for ZKT 2.6.16 and 2.7+, and for any custody-enabled ZKT connector even
when its reported firmware version is stale. Older legacy clients remain accepted.
Every supplied epoch is checked against the locked job or coverage and its
terminal/generation binding before replay acknowledgement or state mutation.
A stale request cannot invalidate the replacement coverage. Probe recovery
acknowledges the accepted old epoch; the accompanying inactive coverage message
announces the newly created epoch. No Oracle completion is asserted.

The firmware has bounded, independently executable wire parsers. They reject
invalid UUIDs, truncated serials, fractional/overflowing cursors, malformed
required digests and invalid numeric limits. Source probes do not read the
ordinary assignment's optional cursor. A source message cannot silently omit an
epoch after an allocation failure. The ACK wait checks message ID, expected ACK
type and epoch; typed chunk/tail consumers also check their existing range,
serial, generation and chain evidence. Generic transport ACKs do not satisfy an
epoch-bound source request. Diagnostic cursor updates require the matched ACK.

The current checkpoint is retained unchanged for reader compatibility. Its
stored boolean and numeric generation cannot prove a UUID. On a 2.6.16/2.7.0
ZKT boot, tail certification therefore stays false until fresh ADD coverage or a
bound manifest ACK supplies the UUID and the runtime checkpoint commits.
Checkpoint failure or source invalidation clears that volatile authority. This
does not gate raw live journal preservation during an ADD outage. Hikvision's
boot behavior and the deployed legacy firmware contract remain unchanged.

Verification covers actual ADD commit/replay and bootstrap, all six source
request types, same-generation recovery, rejection before coverage invalidation,
actual C parser/dispatcher execution, allocation faults, coverage commit failure,
legacy compatibility, and both ESP-IDF families. This component does not qualify
physical terminal layouts, establish live/history matches, sign an artifact, or
complete HIL. Physical power interruption and endurance remain NOT_PERFORMED.
