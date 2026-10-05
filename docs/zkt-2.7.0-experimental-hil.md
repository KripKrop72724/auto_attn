# ZKT 2.7.0 experimental remote HIL

On 5 October 2026 the release owner confirmed that authenticated ADD access is
the available external resource and requested HIL rollout without waiting for
an isolated Oracle 19c/ORDS system or independently supplied terminal fixtures.
Use the implemented protocols, automated synthetic tests, code review and ADD
evidence to prepare the experimental release. Do not require the owner to
provide further bench resources. The optional
[DBA/bench handoff](zkt-2.7.0-qualification-handoff.md) remains a procedure for
future evidence, not a prerequisite to this experimental HIL channel.

Unperformed external and physical checks stay `NOT_PERFORMED` or `NOT_ASSERTED`.
Engineering judgement may select an implementation or accept a documented
experimental limitation; it cannot create a passing test result. Experimental
HIL is separate from production qualification. Actual field results determine
whether a campaign succeeds.

## Implementation and release sequence

1. Finish and test the code needed for preservation, delivery, migration and
   rollback. Passing compilation alone does not finish an unimplemented path.
2. Build and sign the compatibility bridge from a green exact main commit.
   Restrict its package to exact HIL targets and supported predecessor images.
   Its initial authority remains the legacy delivery path. Including journal
   capture support does not itself switch delivery authority.
3. Verify bridge boot and local storage through ADD. Retain the exact image as
   the writer's compatible rollback image. Complete retained-queue custody and
   the ADD integration before enabling the writer.
4. Build the final 2.7.0 candidate reproducibly, sign it using the existing ADD
   host vault, and publish it as an immutable HIL-only artifact. Preserve all
   test results and stated limitations with that exact artifact identity.
5. Start eligible online targets through the controlled waves. Keep at most
   two simultaneous upgrades and one per physical location. An online connector
   whose terminal is unavailable still needs capture readiness. All 17 active
   ZKT connectors remain in the nationwide denominator.
6. Use ADD boot/image evidence, ordinary source/custody/Oracle traces and the
   approved bounded remote recovery tests to determine results. Missing traces,
   unresolved identity and failed boot/recovery remain visible; time windows
   and field outcomes cannot be marked complete in advance.

The lack of a physical bench, independent model fixtures or isolated matching
Oracle system does not by itself prevent the experimental HIL publication.
Concrete defects that can lose records, invent identity, break rollback or stop
the required delivery path must be fixed before enabling that path. A stopped
or held path is not an implemented successful delivery path.

Production Oracle remains read-only for this development work: no package/route
installation, repair, replacement or synthetic production punches. Ordinary
attendance continues through the authorized application delivery path. Any
verification supported only by the existing Oracle interface must be labelled
with its actual scope; it cannot be presented as the expanded projection proof.

ADD implements this distinction with immutable delivery intents. An explicitly
registered experimental intent uses `ORACLE_UID_MEMBERSHIP_V1` and the installed
`raw-captures/check` interface. Before any send, ADD commits the frozen payload
and rechecks its source/identity evidence. It checks membership before sending
and again after a response or timeout. A successful POST alone cannot retire
the outbox. A committed membership receipt records the request, response and
payload digests with confirmation path `ADD_ZKT_UID_ONLY_V1`; raw-content and
daily-time proof remain unasserted. Missing or malformed membership responses
retain a retry obligation.

The default `ORACLE_RAW_DAY_TIMES_V2` contract remains separate. Endpoint failure
never changes an existing intent's verification scope. No existing attendance
UID, Oracle key, frozen payload or content receipt is migrated to the weaker
contract. Migration `0050` retains both kinds of evidence across backend
rollback and requires no Oracle schema or data change. The caller must still
establish a canonical source occurrence and verified identity before delivery;
this adapter is not authority to assign an employee or activate capture.

## Current state

The [implementation register](zkt-2.7.0-implementation.md) records completed
components and remaining integration. This policy document does not sign an
artifact, enable a writer or create a campaign. Physical power-loss and flash
endurance qualification remain `NOT_PERFORMED`.

## Compatibility bridge package

Two controlled 2.6.16 canary attempts on 5 October rolled back before ADD
received a bridge heartbeat. Compiling the released startup adapters with
Xtensa stack-usage output found a 7,936-byte nested command-recovery path in a
4,096-byte supervisor. The repaired bounded client scratch and mutex-protected
command scratch reduce that path to 1,984 bytes; catalog recovery measures
2,240 bytes. CI requires the measured paths plus a 1,024-byte callee reserve
to fit. This is compiler evidence, not a field crash diagnosis or measured
stack high-watermark.

The replacement also preserves the legacy-inventory generation across the
app's successful once-per-second recovery audit and persistence probe. These
checks cannot add attendance or establish an empty queue. Pending, failed or
unverified checks still revoke the proof; admitted appends and retirements
invalidate it before execution. A regression test reproduces the former
generation churn and verifies 90 repeated checks without weakening failure
invalidation. This allows two fresh telemetry samples to prove the same
unchanged empty inventory before migration.

The replacement is a separately versioned **2.6.17** bridge with its own
application descriptor and versioned V3 marker. Preserve the signed 2.6.16
package and failed deployment history; never republish repaired bytes under
that identity. The release scanner continues to validate the old signed
package while publication, exact-target scope, predecessor checks and recovery
tests also cover 2.6.17. A replacement reader may renew a valid older proof
only for the same terminal, capture epoch and layout. The old proof alone
cannot authorize a writer or OTA transition.

The replacement was built and signed from the seven-job CI-passing source
`42de8ac5b38c9229203e0c779d151af834b28123` in
[HIL candidate run 37284425786](https://github.com/KripKrop72724/auto_attn/actions/runs/37284425786).
The downloaded package's RSA-PSS manifest signature, field signing key, compiled
contract, binary digest and actual ESP application-validation hash were checked.
The writer's compiled reader version and packaging pins now use these exact
2.6.17 bytes. Publication does not prove a successful field boot; ADD still
requires the installed compatible reader and fresh healthy evidence before
assigning the writer.

HIL publication first verifies the exact signed manifest using the running ADD
container's installed family and storage-contract validators and configured
public key. This preflight uses bounded temporary metadata files, executes no
database operation and never copies firmware credentials into the container.
A backend without the required contract must be deployed before publication;
otherwise the package remains outside the live store. Publication shares the
ADD deployment lock, so backend replacement cannot race this check. Signature,
image and quarantine verification still apply independently. This ordering
prevents a valid new package from breaking an older backend's release scanner.

The 2.6.16 HIL build includes journal readers and capture support, with legacy
delivery authority on first installation. It carries one compiled V3 bridge
marker and an exact signed storage contract. ADD verifies the ESP application
descriptor, marker, manifest signature, full artifact hash and explicit HIL
quarantine before registering it. An initial installation requires a verified
OTA-slot predecessor with one of the pinned 2.4.12, 2.5.2 or 2.6.15 application
digests. Factory partitions remain excluded until their exact transition is
implemented. A version string alone cannot authorize an installation.

The signed manifest declares all 17 ordered connector/MAC/terminal identities.
The initial quarantine can expose only its leading canary; subsequent marker
extensions must retain that exact prefix and cannot change the signed image or
manifest. ADD advances to the next exposed target only after the preceding
target's successful deployment and matching artifact HIL acceptance. This
release-specific scope leaves legacy campaigns' shared configuration intact.
An unavailable target stays pending; no acceptance is inferred from absence.
For the replacement 2.6.17 reader, bridge preparation advances only after a
separate `BRIDGE_READINESS_V1` observation. Start it through the HIL-run API
with that profile, then invoke `complete-bridge` after its 15-minute interval.
The server reads its own authenticated telemetry, checks the exact signed
image and current boot, sample continuity, reader and storage health, workers,
legacy queue recovery, unchanged failure counters and completed source tail.
An incomplete interval, reset, stale current state or missing evidence cannot
produce `BRIDGE_READY`. The stored observation and exact release identity must
back the event before the next bridge target becomes eligible.

`BRIDGE_READY` is preparation evidence, not `HIL_ACCEPTED`. Oracle delivery,
writer HIL, recovery fault injection and physical qualification remain explicitly
unasserted or unperformed in that result. The 2.7.0 writer still requires its
own custody handoff and HIL acceptance; a bridge result cannot satisfy them.
Both publisher
and promoter reject general production availability for this experimental
package, and ADD rejects missing quarantine. Runtime reader attestation, storage
recovery and persisted delivery authority remain independent of packaging.
This bridge contract does not enable 2.7.0 writer registration or supply a
completed migration certificate.

Local verification covers the real ESP-IDF build at two independent paths,
byte-identical unsigned artifacts, disposable RSA-3072 bootloader/application
signatures, the signing script's manifest construction, RSA-PSS verification by
ADD, invalid predecessor/capability packages and publication/promotion refusal.
The hosted CI gate additionally exercises production Windows PowerShell 5.1.
Local test keys and CI setup credentials are never field signing credentials.

## Source-to-attendance integration

The bounded source inspector can create a canonical attendance row and an
ADD-owned Oracle intent in one transaction. Migration `0051` adds separate
cutover and derived-binding tables; it never rewrites original raw manifests,
source-chain dispositions, occurrence aliases or earlier interpretations.
Identical bytes at separate ordinals remain distinct punches. An unchanged
recovery-prefix ordinal reuses its ancestor's event and intent. Changed,
missing, cyclic or unrelated ancestry stays held with a reason.

Creation requires a persisted, protected source cutover binding the connector,
terminal, model, source epoch, first new ordinal, layout, decoder, writer image,
boot and migration evidence. Neither a firmware version nor a plausible decode
creates that permit. The release handoff must still prove and issue it; this
migration creates no field permits. Records before its boundary retain their
existing legacy reconciliation obligation. This is necessary to avoid minting
new identities for delayed legacy records.

Experimental source layouts use the repository decoder for the six declared
model profiles and retain `NOT_ASSERTED` qualification. Complete interpretation
coverage and matching raw bytes are rechecked. Historical attendance UIDs are
never enrollment IDs: the eight-byte layout remains an identity-reference hold.
The ordinary identity/continuity/manual-approval rules still decide whether a
new attendance is deliverable. A hold receives a durable outbox and owned intent
without resolving an employee. UID membership receipts retain their narrower
scope; attendance creation alone never becomes Oracle completion.

The tests exercise the real raw-custody ingress, source worker, identity gates,
delivery claim and membership adapter with synthetic data. They do not issue a
field cutover, qualify a terminal model, or complete live/source matching.

Once a journal runtime is ready and has committed source coverage, its source
tail scheduler attempts at most one existing bounded range per gateway turn,
then waits two seconds plus up to 250 ms jitter. Live packet preservation,
lease expiry and commands retain their earlier positions in that turn. An ADD
outage suppresses these scans while local raw capture continues. Failures back
off with jitter to at most 60 seconds, with deadlines retained across terminal
session reconnects. A failed count read retains the previous observed count;
it cannot publish a fabricated zero and trigger source regression handling.
Legacy delivery keeps its existing audit interval. This scheduling change does
not establish measured end-to-end latency or change an already signed image.

The 2.7.0 gateway now preserves its first consistent terminal count and the raw
digest of the preceding ordinal in encrypted NVS, through the storage owner.
The owner binds this immutable boundary to the capture epoch, terminal digest
and actual writer image. It verifies the commit by reading it back; a lost reply
or a later, larger terminal count returns the original boundary. Invalid,
corrupted or differently bound state stays held and is never recreated.

The gateway reads only the four-byte prepared-buffer header and one final
record, accepts an exact count/length agreement, and releases the prepared
buffer before waiting for persistence. A failed release cannot authorize a
boundary. Empty terminals have an explicit zero boundary and no inferred
layout. This capture works without ADD connectivity and retries with bounded
backoff while live preservation remains active. Its caller retains one pending
owner request after a timeout instead of discarding uncertain work.

Diagnostics report the persisted boundary separately from migration, with
`migration_certified: false`. This is source evidence, not a completed legacy
handoff or permission to deliver attendance. The backend still needs to verify
the matching source anchor and legacy custody before issuing a cutover permit.
The already signed 2.6.16 artifact is unchanged. Host sanitizers cover uncertain
commits, all stored-byte corruptions, binding changes, saturated storage,
concurrent callers, changed snapshot sizes and terminal-release failures.

ADD retains this boundary in the typed heartbeat contract and binds its sample
to the authenticated envelope's boot and sequence. The custody status API
independently checks the current writer image, terminal binding, fresh sample,
active source coverage and the encrypted original anchor bytes. A matching
metadata digest with changed or unavailable raw bytes remains held. The device
detail panel ages the boundary's own heartbeat separately from the API query.
This check is read-only and cannot create a source cutover, enable journal
custody, resolve identity, or assert migration or Oracle completion.

## Experimental writer handoff

The 2.7.0 package carries one compiled V4 writer marker and an exact signed
journal contract. Its only packaged predecessor is the immutable 2.6.17 bridge
application `f803361f8c3e1ed03f57814793942ebda45bbbd10cd0c1e77239602f5e6e32a1`
and signed artifact `be23f88bfc8e2a6f7233ab2f8903c53ac9deaf1433ad39608e7f00f7e4f2bc71`.
ADD requires that connector's successful bridge installation, current secure
OTA-slot identity, recovered storage and live compatible-reader telemetry.
A failed or rolled-back bridge cannot authorize the writer. The actual runtime
independently checks persisted reader proof and the retained rollback slot.
If the bridge needs different bytes, publish a new identity and update the
explicit reader contract; do not overwrite an existing signed release.

After publishing the signed writer in its exact HIL quarantine, an authenticated
administrator may POST `{release_id, idempotency_key}` to
`/api/v1/devices/{connector_id}/zkt-custody/enable`. This enables bounded ADD raw
custody after the bridge checks; it grants no attendance or Oracle authority.
The normal campaign still performs its independent scope and upgrade checks.

After installation, the matching POST to `zkt-custody/handoff` verifies the signed
release, exact active writer image, installation record, immutable source anchor
against encrypted ADD source evidence, and two authenticated telemetry samples
10–90 seconds apart. Both must show the same 64-bit owner generation, all 13
legacy storage domains empty, no pending owner/read/append request, recovered
storage, ready reader/writer, and idle retained legacy delivery workers. A newer
sample must be at most 45 seconds old. A telemetry claim alone cannot create the
receipt, and neither unknown counts nor a version string replaces those checks.

The source cutover, encrypted evidence receipt and administrator audit commit
in one database transaction. Retrying a lost response with the same actor/key
returns the same receipt. Conflicting requests cannot move the boundary. ADD
revisits existing held source work using a bounded durable cursor; original
source bytes, identity holds and legacy Oracle keys remain retained. Migration
0052 preserves the handoff on application/database downgrade.

GET `zkt-custody` exposes `legacy_handoff` separately from `source_boundary`.
Its scope is `LOCAL_LEGACY_ABSENCE_AND_SOURCE_BOUNDARY_V1`: it establishes the
recorded local transition, not historical completeness, decoder qualification,
current hardware health or Oracle completion. Missing or changed retained proof
stays held. Empty terminal boundaries without a known layout and eight-byte
layouts without supported textual user identity remain held for this path.

These interfaces do not automatically grant a field handoff, report HIL success,
or replace actual post-installation source/identity/delivery evidence.

A signed writer with complete ordinal custody may enter observation while its
source interpretation is `SOURCE_CAPTURE_CERTIFIED_RAW_PENDING`. This requires
the matching ADD-authority runtime and the existing cursor, chain, completed
reconciliation and preservation checks. The observation stores custody and
Oracle states independently; it does not turn raw custody into valid attendance
or an Oracle delivery pass. Legacy releases cannot claim this exception.
