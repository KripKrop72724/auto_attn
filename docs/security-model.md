# ADD and Zone Lite security model

## Protected assets

- Employee name, CNIC, shift status, attendance linkage, and tombstones.
- Dashboard administrator session and password verifier.
- Fleet root, connector bootstrap secrets, rotating device tokens, Wi-Fi/ORDS/ZKT credentials.
- Commands that alter users, grant privilege, restart a terminal, or delete an identity.
- Audit integrity and the guarantee that user deletion does not delete attendance.

## Trust boundaries

The browser is untrusted input and receives masked identity data only. ADD is the authorization and
audit boundary. Zone Lite is the only principal allowed to speak the ZKT protocol. The branch LAN,
public internet, and terminal are treated as unreliable. PostgreSQL, Redis, the runner environment,
and fleet-root storage remain server-side and are never exposed to the frontend image.

## Authentication and authorization

- One named administrator signs in with an Argon2id password verifier in the protected environment.
- Sessions use HttpOnly, Secure, SameSite cookies with idle and absolute expiry. Mutations require a
  matching CSRF token; login is rate limited.
- User mutations, restart, delete, and administrator-lease operations require a recent password
  step-up. The API performs authorization and creates an audit entry before dispatch.
- ESP onboarding uses a per-MAC HKDF secret. Timestamp skew and nonce uniqueness prevent replay.
- Connector HTTP/WebSocket traffic uses a rotating token and HMAC request binding. Old/new token
  overlap is ten minutes, then the old credential expires.
- A connector can act only for its own ID and certified terminal. Duplicate serial claims quarantine
  every claimant.

## Encryption and minimization

- Backend PII and command expected/desired/payload state use Fernet encryption at rest.
- CNIC lookup uses a separate keyed digest over all thirteen normalized digits; responses expose
  only the final four digits and non-PII terminal user IDs as duplicate-match evidence.
- Audit and log payloads use recursive key-based redaction. ORDS payload is generated only at send
  time and is not retained as plaintext in the outbox.
- Duplicate-CNIC alias approval stores only the keyed CNIC lookup, internal row IDs, deterministic
  membership token, classification, and audit reason. It never copies plaintext CNIC into the
  resolution record and never rewrites historical attendance.
- Zone Lite uses ESP-IDF encrypted NVS. XTS material is derived through a per-device HMAC key stored
  in read/write-protected ESP32-S3 eFuse BLOCK_KEY0.
- Persistent command inbox, lease deadline, identity tombstones, connector token, and site secrets
  are encrypted. Temporary provisioning CSV/key/NVS material is created in an OS temporary directory
  and removed after flash readback verification.
- PostgreSQL and Redis have no host ports. Public exposure is limited to 8095/8096 behind TLS.

## Command safety

Every mutating command has a UUID, idempotency key, target connector/serial/user, creation and expiry
time, row version, encrypted precondition, encrypted desired state, and state transitions. The ESP
persists the command before execution. Duplicate delivery returns the persisted result; it does not
execute twice.

Fresh terminal reads occur immediately before and after every user write. Partial snapshots,
uncertified record size, serial change, stale version, unexpected name/privilege, offline/flapping
state, or ambiguous command replay fail closed. Transient protocol failure produces a durable retry
rather than tight reconnect. Cancellation is a handshake: queued work can cancel; already running
work reports that it is running and must finish verification.

Some legacy ZKT records contain non-UTF-8 bytes in their user-ID or name fields. Zone Lite never
weakens the precondition to accommodate them. Firmware 2.1.2 and later publish keyed HMAC
fingerprints for the exact raw terminal identifier and mutable state bytes. ADD binds a mutation to
those opaque fingerprints, and the ESP compares them against a fresh read before writing. The
identifier fingerprint is checked again after reread. The bootstrap secret never leaves encrypted
NVS, and the fingerprints expose neither the malformed bytes nor CNIC/name contents.

Delete has an additional invariant: the attendance count must be identical before and after ZKT
`CMD_DELETE_USER`. The backend database has no attendance-delete operation in the user command path.

Duplicate-CNIC resolution is deliberately an ADD-only metadata operation. It issues no ESP/ZKT
command. Every approval is bound to the complete snapshot's exact member IDs and row versions,
requires password step-up plus `SAME EMPLOYEE`, and becomes stale on membership change. Revocation
blocks new ambiguous punches again; previously captured terminal facts are never removed.

## eFuse ceremony

Burning an eFuse is irreversible and is not authorized by a general request to flash firmware. The
operator must:

1. read and record the detected Wi-Fi MAC and current `espefuse` summary;
2. confirm BLOCK_KEY0 is empty/writeable and KEY_PURPOSE_0 is `USER`;
3. obtain explicit approval naming that exact MAC;
4. invoke the provisioner with `--confirm-efuse-burn-for <exact-mac>`;
5. verify KEY_PURPOSE_0 becomes `HMAC_UP` and provisioning readback hashes match.

An existing unreadable HMAC block is never assumed to belong to this fleet. The explicit
`--trust-existing-derived-hmac` option is allowed only when prior Zone Lite provisioning evidence
proves that the same protected fleet root derived it.

Normal provisioning requires `ADD_FLEET_ROOT_SECRET` explicitly and never falls back to the PII
lookup key. Exceptional recovery may separate the production onboarding root from a proven original
NVS HMAC root only for an already-locked exact MAC. It requires
`--trust-existing-derived-hmac` plus `--confirm-split-root-recovery-for <exact-mac>`, refuses empty
eFuses, rewrites only encrypted NVS, and does not alter the production fleet root.

## Secret handling

Production values live in the runner's protected env file or encrypted Actions secret. CI creates
random disposable values at runtime. Gitleaks scans the committed worktree; a repository-contract
guard rejects retired products and unexpected workflow surfaces. Logs, Actions output, release
metadata, and documentation must never contain plaintext credentials or unmasked CNIC.

Compromise response:

- Administrator password: replace its Argon2id hash and invalidate sessions.
- Connector token: increment onboarding generation/re-onboard; bounded overlap expires the old one.
- One ESP bootstrap secret: securely reprovision that MAC.
- Fleet root: treat as fleet-wide rotation requiring staged reprovisioning; do not silently change it.
- PII Fernet key: follow an explicit dual-key data migration; changing it directly makes existing
  encrypted rows unreadable.
