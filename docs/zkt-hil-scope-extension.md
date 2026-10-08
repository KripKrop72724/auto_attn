# Extending signed ZKT HIL exposure

The **Extend exact ZKT journal HIL exposure** workflow changes only the ordered
exposure marker of an already published experimental `2.6.23`, `2.6.22`, or
`2.7.0` release. It does not create a campaign, admit a device, or establish a
HIL pass. ADD retains its first-canary, qualified-reader, factory-trial,
reservation, revocation, concurrency, and per-device health requirements.

## Review and preview

Run the workflow from current `main`, with all required CI checks successful.
Supply the signed release's exact source, application, and full signed binary
SHA values, its current prefix count, and a larger requested prefix count.
The workflow derives device identities from the tracked inventory; it does not
accept caller-supplied device lists. Ordinary bridge and writer releases use
the nationwide ordered 17-device scope. Factory `2.6.22` uses only the three
approved factory targets, in their fixed order. The nationwide denominator
remains 17.

First select `preview` and type `PREVIEW-<version>-HIL-<old>-TO-<new>`, replacing
the placeholders with the exact inputs. Review the retained metadata artifact.
A successful preview preserves every marker and package byte and creates no
previous-marker backup. It verifies current green main, firmware-source
ancestry, the actual running ADD container instance, the deployed trust and
contract validator, and the existing unrevoked HIL catalog row. Missing catalog
rows are refused; this workflow never imports a release to make it eligible.

## Apply and inspect

Select `apply` with the same reviewed identities and counts and type
`APPLY-<version>-HIL-<old>-TO-<new>`. The wrapper repeats all checks immediately
before replacement. The existing ordered-scope implementation atomically
replaces only `.hil-only.json` and preserves its exact old bytes in one
`.hil-scope-before-*.json` backup. The signed manifest, signature, and binary
must remain unchanged. An already changed prefix is refused rather than
treated as a successful retry.

The postcheck reads the existing catalog without writing it. Its result is
`CATALOG_CURRENT` when ADD has already observed the new marker, or
`CATALOG_REFRESH_PENDING` when the catalog still has the exact prior prefix.
The normal ADD catalog read owns that refresh. Neither state grants campaign
eligibility. Any other prefix, revoked row, changed backend instance, changed
main, or changed signed package fails the operation.

## Interrupted or failed outcomes

The metadata artifact records `NOT_ATTEMPTED`,
`ATTEMPTED_OUTCOME_UNCERTAIN`, or `APPLIED` separately from the final status.
A postcheck can fail after an atomic replacement; `APPLIED` does not become a
successful rollout claim. Inspect the exact current marker, prior backup,
artifact, and ADD revocation state before another attempt. Do not blindly
retry, restore a revoked release, or overwrite the retained evidence.

The `add-production` concurrency group coordinates repository workflows, and
the store lock serializes cooperating calls to this wrapper. Neither is a
global lock against arbitrary Docker or database administration. Container
identity and database revocation are checked before and after the operation;
ADD's server-side admission remains authoritative at assignment and download.

## Verification

Python tests cover exact inventory/prefix derivation, incomplete and non-green
GitHub checks, changed main, signed metadata, catalog import metadata,
revocation, and the explicitly pending refresh state. The PowerShell wrapper
tests execute the real planner and atomic marker writer with synthetic signed
contracts; only GitHub and deployed-container boundaries are mocked. They
cover previews, exact backups, immutable packages, concurrent changes, and
failures after replacement. CI runs these tests under native Windows
PowerShell 5.1 as well as PowerShell on Linux. Local tests are not a production
execution or field qualification.
