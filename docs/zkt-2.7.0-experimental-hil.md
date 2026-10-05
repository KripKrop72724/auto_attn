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

## Current state

The [implementation register](zkt-2.7.0-implementation.md) records completed
components and remaining integration. This policy document does not sign an
artifact, enable a writer or create a campaign. Physical power-loss and flash
endurance qualification remain `NOT_PERFORMED`.

## Compatibility bridge package

The 2.6.16 HIL build includes journal readers and capture support, with legacy
delivery authority on first installation. It carries one compiled V3 bridge
marker and an exact signed storage contract. ADD verifies the ESP application
descriptor, marker, manifest signature, full artifact hash and explicit HIL
quarantine before registering it. An initial installation requires a verified
OTA-slot predecessor with one of the pinned 2.4.12, 2.5.2 or 2.6.15 application
digests. Factory partitions remain excluded until their exact transition is
implemented. A version string alone cannot authorize an installation.

Publication requires ordered connector/MAC/terminal identities. Both publisher
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
