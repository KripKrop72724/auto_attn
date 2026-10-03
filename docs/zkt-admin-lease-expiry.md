# ZKT local administrator lease expiry

A valid wall clock alone is insufficient to expire a temporary administrator
lease: a backward correction or frozen clock can extend the privilege. The ZKT
runtime now retains a boot-local, 64-bit monotonic deadline alongside the
existing durable lease obligation. Either deadline can make revocation due.
Backward wall or monotonic time, a changed obligation, or a missing uptime
anchor also makes it due. That decision stays latched until verified revocation
and a successful durable clear.

New grants still persist an obligation before changing terminal privilege and
start the requested duration after verified elevation. Replaying an existing
grant retains its earlier monotonic bound. A restored lease has no trustworthy
boot-local anchor: on the next authenticated terminal session it is due for
revocation, even if its stored wall-clock expiry is in the future. It cannot be
renewed by replaying a grant command. No NVS layout change is required.

The gateway checks this obligation immediately after obtaining its initial
user table and before its ordinary credential-policy/publication work. It also
checks at the start of each live loop before socket reads, configuration yield,
commands and background work. ADD connectivity is not required for local
revocation. A failed terminal read/write/reread or failed checkpoint retains the
obligation for retry; an ACK without a verified privilege change is insufficient.

Revocation still requires reachable terminal service and a safe session
boundary. This does not promise execution at the exact deadline while a terminal
operation is in progress or the terminal is unavailable. Existing session and
operation bounds continue to apply. Corrupt or unreadable durable lease state,
historical enrollment reuse, and complete scheduling/physical qualification
remain separate release work; these clock tests do not resolve those concerns.

The host tests execute the production grant, watchdog and persistence adapters
with memory/undefined-behavior sanitizers. They cover all durations from one to
600 seconds, uptime beyond the 32-bit wrap boundary, invalid/overflow values,
clock reversal and freeze, ESP reboot, replay, unavailable terminal service,
missing users, unverified terminal writes and failed durable clearing.
