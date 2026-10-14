# BLD1 temporary HIK write exception — 14 October 2026

The owner requested an urgent, single-terminal exception. This is not model
qualification or a global enablement of HIK writes.

Scope: connector `dacdee7c-f824-40cc-b4c0-3d873653f8db`, ESP
`ac:27:6e:a3:0a:58`, zone `LF-ZONE-BLD1-01`, device `1`, terminal serial
`G04600476`, reported profile `ds-k1t341amf-v3.2.30-20210406-unverified-v1`.
Requires firmware `3.1.1-bld1fix4`, profile command version 3, confirmed matching
terminal identity and a complete, stable user snapshot.

An authenticated administrator must PUT the terminal serial, enabled flag,
reason, idempotency key and step-up password to
`/api/v1/devices/{connector_id}/hikvision-write-exception`. The approval is audited.
Never put passwords in scripts, source, shell arguments, logs or this document.

The exception expires at **2026-10-15 16:00 UTC (21:00 PKT)**. ADD checks expiry
when accepting commands; the device independently checks expiry at execution.
Revocation uses the same endpoint with `enabled: false` and a new idempotency key.
Revocation prevents new commands; an already accepted command remains subject to
its existing command lifecycle and the firmware expiry check.

Regular-user creation, profile edits and whole-profile deletion become available
only after approval and receipt of the exact firmware capability. Administrator
role changes and administrator leases remain disabled. Existing identity,
fingerprint, confirmation, audit and readback safeguards are unchanged. Whole
profile deletion removes enrolled credentials and still needs the normal explicit
operator confirmation. No user mutation is performed by the installer or by
activating this exception. Attendance policy and qualification remain unchanged.

The Windows installer 1.0.4 bundles signed firmware from commit
`b72fea185a5c681cd6c6e58dbd408a391a34f531`. It preserves the installed flash layout,
NVS settings, keys, bootloader and attendance storage. Signature/layout checks,
host tests and Windows package self-tests do not establish hardware qualification.
No create/edit/delete hardware qualification was performed for this exception.
