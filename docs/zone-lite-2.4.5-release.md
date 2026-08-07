# Zone Lite 2.4.5 corrective HIL release

Zone Lite 2.4.5 preserves the coordinated 2.4.4 self-healing reconciliation
protocol and corrects its HIL-discovered source-probe intake guard. The 2.4.4
connector compared probe assignments, which intentionally carry no normal
source cursor, against the last committed source cursor and rejected them as
stale. 2.4.5 exempts only authenticated `source_probe_assignment` messages from
that normal-cursor comparison; ordinary reconciliation assignments retain the
existing regression guard.

The revoked 2.4.4 image remains immutable. Karachi's reconciliation operation,
source evidence, divergence observations, epoch, and 5,100 checkpoint are not
reset. Publish 2.4.5 as a new exact-MAC `HIL_ONLY` release, update only Karachi,
and resume the same paused operation. Promotion still requires the final
partial chunk, confirmed divergence recovery, complete Oracle assurance, stable
device health, throughput evidence, and the 24-hour canary soak.
