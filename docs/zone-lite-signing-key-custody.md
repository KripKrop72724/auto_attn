# Zone Lite signing-key custody

## Authority boundary

The trusted ADD Windows host is the sole private-key authority for Zone Lite firmware. The ADD web application is not a signing service and cannot read the keys. The PostgreSQL database stores release and key identifiers only. The `/firmware` mount stores signed release packages only.

The private keys are available exclusively to the dedicated self-hosted GitHub runner account through Windows DPAPI `CurrentUser`. GitHub's `firmware-signing` environment supplies reviewer authorization, not key bytes.

## Vault contents

The protected directory is `%ProgramData%\StateLife\AttendanceDeviceDashboard\firmware-signing`.

| File | Sensitivity | Purpose |
| --- | --- | --- |
| `key-N.dpapi` | Secret ciphertext | DPAPI-protected RSA-3072 private key |
| `key-N.entropy` | Restricted companion | DPAPI optional entropy required for decryption |
| `key-N-public.pem` | Public | Release verification and eFuse fingerprinting |
| `vault-manifest.json` | Public metadata | Key number, key ID, lifecycle state, runner identity, creation time |

The directory disables inherited ACLs and grants full control only to the runner identity, `SYSTEM`, and local administrators. Do not grant the ADD container identity, database identity, web administrator, or ordinary deployment operator access.

## Creation

1. Confirm the physical ESP MAC with `esptool read-mac`.
2. Confirm Secure Boot is disabled and the required eFuse key blocks are available.
3. Run `Zone Lite signing key bootstrap` from the exact green `main` commit.
4. Enter the exact MAC and type `CREATE ADD FIRMWARE VAULT`.
5. Approve the protected `firmware-signing` environment.
6. The hosted job builds an unsigned candidate with ESP-IDF 5.5.3.
7. The ADD runner creates or reuses the DPAPI vault and signs the bootloader with all three keys.
8. The ADD runner signs the application only with key 1.
9. Download the one-day artifact and verify its target MAC and SHA-256 values before physical flashing.

The operation is idempotent. Once `vault-manifest.json` exists, it must reuse the same three keys. Missing or unreadable vault components are incidents, not permission to regenerate.

## Routine release signing

The `Zone Lite signed firmware release` workflow verifies an exact green `main` SHA and successful physical HIL run. Its hosted job creates an unsigned application. The protected ADD runner then:

1. Loads the vault manifest and requires exactly one active key.
2. Decrypts that key with DPAPI under the runner account.
3. Signs the application with ESP Secure Boot V2 RSA-PSS.
4. Creates and RSA-PSS-signs the canonical release manifest.
5. Overwrites and deletes temporary plaintext key material.
6. Uploads only the signed image, manifest, signature, public key, and checksums.
7. Atomically publishes the package to ADD's read-only firmware store.

## Backup and recovery

DPAPI `CurrentUser` protection depends on the Windows account profile and DPAPI master keys. Back up all of the following as one recovery unit:

- ADD host system state.
- Dedicated runner account profile.
- DPAPI master-key material protected by Windows.
- Firmware-signing vault directory.
- GitHub environment and runner configuration documentation.

Quarterly, restore the backup into an isolated host and prove that the active key can sign a disposable image whose public fingerprint matches `vault-manifest.json`. Never test recovery against a production ESP.

If the production host is lost and recovery cannot decrypt the vault, existing devices remain operational but no new trusted firmware can be issued. Do not generate a replacement fleet key and do not revoke existing keys until an incident plan accounts for every bootstrapped device.

## Rotation and revocation

Key 1 starts active; keys 2 and 3 are reserves already trusted by the factory bootloader. Rotation is a dedicated release:

1. HIL-test an application signed by the selected reserve key.
2. Roll it out to one Peshawar canary and complete the observation period.
3. Roll out one device at a time per zone.
4. Change vault lifecycle metadata only after all reachable devices prove the reserve signature.
5. Revoke an old eFuse digest only through a separately reviewed firmware release and incident-approved campaign.

Never rotate merely because a key is old. Rotate for a planned cryptoperiod or a reviewed compromise response, with rollback implications documented first.

## Incident evidence

Preserve the GitHub workflow run, exact commit SHA, vault manifest, public fingerprints, signed package hashes, device MAC, eFuse summary, and release/campaign audit events. Never attach private vault files or decrypted PEM material to an issue, email, chat, or support ticket.
