# Retained factory bundle provenance audit

Production execution status: **NOT_PERFORMED**. Synthetic tests validate the
audit procedure; they do not establish that the archive exists or that any
installed device matches it.

The manually dispatched `factory-provenance-audit.yml` workflow reads only the
retained `zone-lite-2.5.2-27c3bb80eb20` bundle from the fixed Windows factory
store. It runs from `main` in the protected `firmware-production` environment.
Its only confirmation is `AUDIT EXACT PUBLISHED 2.5.2 BUNDLE`; it accepts no
path, image, device, command, or credential input.

The launcher requires a locally cached, digest-pinned ESP-IDF 5.5.3 container
and the exact reviewed verifier hash. It refuses reparse points, symlinks,
unexpected file types and oversized inputs. The container has read-only
mounts, no network, no added capabilities, a non-root user, bounded memory,
CPU and process counts, and a 60-second verifier deadline. Host subprocess
output and execution time are also bounded; raw stderr is never exported.

Verification covers the signed manifest and its exact source identity, all
image hashes, complete padded application and bootloader secure-boot
signatures, the ESP32-S3 application descriptor and validation digest, the
seven-partition layout and empty initial OTA data. The pinned public key's
PEM and SPKI digests bind the manifest and image signatures. They do not
establish an installed device's eFuse trust or key-revocation state.

The report compares the verified application digest against the three
previously observed connector digests, bound to their exact connector IDs:
G&P BLD8, BLD9 01 and BLD9 02. A mismatch remains a mismatch. The workflow
neither changes an admission allowlist nor qualifies a predecessor image.

Only metadata is uploaded as `exact-factory-provenance-metadata`, retained
for seven days. It includes the GitHub source/run identity, verifier and
runtime identity, cryptographic results, fixed layout and device comparisons.
It excludes firmware bytes, provisioning data, employee data and credentials.

`ARCHIVE_VERIFIED` describes the archive alone. Installed security, partition
state, rollback feasibility, attendance preservation and HIL acceptance need
their own device-bound evidence. Every report explicitly retains
`installed_security_or_rollback_verified: false` and
`hil_qualification: NOT_ASSERTED`. A refusal, missing archive or unmatched
image must be investigated without altering the archive or widening scope.

Run the isolated synthetic checks with:

```console
python -m pytest tests/unit/test_factory_provenance_audit.py
python scripts/check_repository_contract.py
```

The ESP secure-boot cross-check also runs against `espsecure` when that pinned
SDK dependency is available; otherwise that one cross-check is visibly skipped.
No test invokes Docker against the factory store or changes a real connector.
