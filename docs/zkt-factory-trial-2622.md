# Exact first-OTA trials for the three factory connectors

2.6.22 is a separate experimental compatibility reader for G&P BLD8, BLD9 01,
and BLD9 02. The signed manifest retains all 17 ZKT identities and additionally
pins these three connectors, terminal serials, onboarding generations, and
observed 2.5.2 application digests in `factory_trial`. The operational HIL marker
may expose only an ordered prefix of those three. The other 14 rows remain in
the nationwide register as `NOT_APPLICABLE_TO_THIS_BRIDGE`; this does not count
them as passed or remove them from the final writer's 17-device scope.

The normal bridge's predecessor exception remains closed. The 22 manifest has
minimum bootstrap version 2.5.2 and empty generic predecessor lists. Only the
exact factory policy authorizes a first-OTA trial. Its compiled reader marker
adds `:VERSION=2.6.22:FACTORY_TRIAL=1`; publication verifies both the signed policy
and actual image marker.

## Dependencies and reservation

Before a trial can be created, ADD requires the exact signed final writer's
reader matrix to include this 22 artifact, the qualified nonfactory reader's
stored 3FL `BRIDGE_READY`, and the same writer's completed, sealed 3FL full HIL
verdict with the exact retained reader evidence. The current implementation
binds that nonfactory entry to 2.6.23. Historical 21 evidence cannot substitute
for exact 23 readiness or the writer's retained 23 proof. A failed, unsigned or
revoked 23 cannot satisfy it. The reader matrix remains blocked until separately
reviewed exact artifact pins are installed; these changes populate no matrix
and assume no field readiness.

ADD checks the exact registered connector/terminal/generation, current factory
image and partition, fresh connectivity, stable terminal, security capability,
and existing safety holds. An unresolved installation, reconciliation hold,
reported persistence fault, or ADD custody/writer history prevents a trial.
The legacy 2.5.2 firmware lacks diagnostics v2: absent queue/persistence evidence
is preserved as unknown, with pretrial preservation `NOT_ASSERTED`. It is never
converted into a zero queue or a health pass. A demonstrated fault still blocks.

A campaign stores a sealed `FACTORY_TRIAL_RESERVED` event with an unpredictable
challenge, exact deployment/release, old boot, source baseline when available,
terminal count, dependency evidence, and a 3,600-second deadline. Scope-preview
validation and the existing connector locks prevent changing this binding while
creating a campaign. Assignment and every download recheck the reservation,
same old boot, non-regressing source/count, current dependencies, exposure and
revocation/cancellation. The trial does not bypass global or physical-location
upgrade limits.

## Local proof and authenticated receipt

The old firmware retains its normal deployment checkpoint; it need not understand
new challenge fields. The new reader verifies its exact local factory fallback
before legacy capture. Factory fallback is permitted only while the new reader
is pending local validation. Local fallback does not depend on the network.
Once local validation marks 22 valid, the reader must persist and read back a
permanent fallback-revocation checkpoint before reporting normal boot progress.

The authenticated device endpoint is:

`GET|POST /device/v2/firmware/deployments/{deployment_id}/factory-trial`

GET returns the exact trial, challenge, generation and absolute deadline, also
as integer `expires_epoch`. It requires a fresh authenticated heartbeat from a
different boot running the exact signed 22 image after the complete download.
POST accepts only the bounded `FactoryTrialProof` schema: matching identities,
canonical partition-layout hash, complete signed factory image digest/size,
local checkpoint digest, explicit security checks, and proof state. Signed image
size must cover whole 4 KiB sectors, including the signature sector.

Proof states are `FACTORY_VERIFIED` and `FACTORY_FALLBACK_REVOKED`. The first
received report may already be revoked; ADD does not invent an earlier report.
If both are received, immutable facts must agree and the revocation checkpoint
must differ. Exact retransmissions return the same receipt without duplicate
events. Changed reports, stale/wrong boots, expired trials, cancellation or
revocation fail closed. The response is returned only after the receipt commits;
the client must match every echoed identity and proof state.

These events are **authenticated device software proof**, not independent
hardware attestation. Physical qualification remains `NOT_PERFORMED`.

## Completion and recovery

Normal 22 `BOOTED_PENDING`, `RECONCILING`, and `SUCCEEDED` progress requires the
stored revoked proof for that current boot. Bridge observation separately pins
that receipt and checks the complete 15-minute evidence window. An initial
factory-verification report alone cannot produce `BRIDGE_READY` or enable the
writer. Historical permanent revocation survives a later legitimate reader boot;
writer admission still requires fresh current-boot reader health and exact
rollback evidence.

An old-factory return is failure/recovery evidence, not successful qualification
or proof of the return's cause. It cannot settle a trial as passed. Expiry,
missing evidence, or a changed binding holds progression for review; no automatic
job cancellation, cursor advance, trial extension, factory erasure, forced
rollback or admission override is provided.

Focused tests cover signed catalog/quarantine validation, real campaign and
download binding, unknown legacy evidence, proof replay, strict wire types,
current boot/revocation gates, authenticated HTTP nonce/commit behavior, and
isolated PostgreSQL proof/cancellation races. The V5 matrix integration must also
pass on the combined source before publishing or using this path.
