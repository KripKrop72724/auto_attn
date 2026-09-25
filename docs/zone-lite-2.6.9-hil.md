# ZKT Zone Lite 2.6.9 HIL candidate

2.6.9 is a signed, exact-scope retry for the five ordered ZKT HIL devices in
`deploy/add/hil-targets-2.6.9.json`. Swat is first. The release remains
`HIL_ONLY`; no nationwide campaign or production promotion is authorized by an
OTA download or boot alone.

## Why 2.6.8 was rejected

Swat's 2.6.8 deployment reached `BOOTED_PENDING` but timed out on boot health
and rolled back to the exact signed 2.6.6 image. The target reported
`storage_local_failure_source=zone_lite.c:7145`, while persistence was verified,
storage recovery was complete, and read/write failure counters were zero.
That line latched `LOCAL_FAILURE` when `qs_peek(QS_ORDS)` returned a non-success
result. The queue adapter's one-second lane or shared budget-lock timeout
returned `DQ_IO` without recording a storage failure. A concurrent identity
catalog commit can hold the shared budget lock long enough to cause this
contention. The target's boot gate correctly withheld confirmation and the
rollback preserved the working image.

2.6.9 returns `DQ_PENDING` for a queue peek delayed by lock contention. The
ORDS and ADD readers retry that result without latching a storage fault. Real
queue I/O and corruption results still latch the failure and block boot
confirmation. The five signed predecessor image identities include 2.4.12,
2.5.2, 2.6.6, 2.6.7, and 2.6.8; the current Swat rollback image is 2.6.6.

## Rollout gate

For each device, require a successful boot-confirmed deployment, healthy
durable storage, running delivery workers, preserved queue/source state, live
attendance receipt evidence, and the formal `HIL_ACCEPTED` PASS before
advancing the next target. The physical PIN/card versus biometric canary was
verified on the SLICTOWER G3 only. Swat and Peshawar require their own terminal
model checks before the full credential-cleanup claim can be accepted.
