# Zone Lite 2.5.3 Quetta-only ZKT protocol diagnostic

Zone Lite 2.5.3 is a read-only diagnostic image for the single ESP in
`ZONE-QUETTA-01`. It is not a nationwide firmware release and must never be
promoted. The ordinary firmware project remains 2.5.2; the dedicated workflow
overrides the application version and enables `ZONE_LITE_QUETTA_DIAGNOSTIC`
only for these separately signed bytes.

The diagnostic consumes one normal sealed COMM Key recovery envelope so the
candidate is never stored in source, workflow inputs, manifests, logs, or
screenshots. It probes the preferred address, the previous TCP candidate, and
the ESP's local `/24`, then reports the deepest verified stage:

| Result | Meaning |
| --- | --- |
| `ZKT_TCP_4370_UNREACHABLE` | No attempted host accepted TCP on the configured ZKT port. |
| `ZKT_CONNECT_NO_PROTOCOL_RESPONSE` | TCP opened, but no valid legacy `CMD_CONNECT` response arrived. |
| `ZKT_CONNECT_RESPONSE_REJECTED` | A legacy-framed connect response used an unsupported status. |
| `ZKT_AUTH_NO_PROTOCOL_RESPONSE` | The terminal challenged for a key but did not return a valid auth response. |
| `ZKT_AUTH_RESPONSE_REJECTED` | The terminal challenged for a key and rejected the candidate. |
| `ZKT_SERIAL_READ_FAILED` | Authentication completed, but `~SerialNumber` was not readable. |
| `ZKT_SERIAL_EMPTY` | The legacy serial option was readable but empty. |
| `ZKT_SERIAL_MISMATCH` | A responding authenticated terminal did not match the pinned serial. |
| `ZKT_AUTH_NOT_REQUIRED` | The pinned terminal answered without challenging for a COMM Key. |
| `COMM_KEY_DIAGNOSTIC_VERIFIED_NO_COMMIT` | The candidate authenticated the pinned terminal; no configuration was committed. |

Safe evidence includes only stage booleans, protocol response codes, aggregate
host counts, and whether the existing preferred/previous candidates were
available. It excludes the COMM Key, sealed material, IP addresses, terminal
serials, tokens, and hashes.

## Release and execution gate

1. Merge the reviewed commit to `main` after CI is green.
2. Run **Quetta read-only ZKT protocol diagnostic** with that exact commit, the
   Quetta **ESP MAC** (not the ZKT MAC), and confirmation `QUETTA READ ONLY`.
3. Confirm ADD catalogs version 2.5.3 as `HIL_ONLY` for exactly that ESP. The
   package must contain both `.hil-only.json` and `.diagnostic-only.json`.
4. Stage one ESP-only COMM Key recovery operation for the pinned Quetta
   terminal, then start a campaign containing only `ZONE-QUETTA-01`.
5. Do not power-cycle the ZKT terminal. The ESP may reboot as part of normal OTA.
6. Read the terminal command's final diagnostic code and allow the ESP to
   return to the known-good 2.4.12 image if normal boot health cannot be proven.
7. Cancel or close the diagnostic campaign. Never invoke a canary or production
   promotion workflow for 2.5.3; the promotion script rejects its permanent
   diagnostic marker.

The expected OTA deployment may finish as `BOOT_HEALTH_TIMEOUT` followed by a
safe bootloader rollback because the read-only image deliberately does not save
the candidate key. The diagnostic command result, not OTA promotion success, is
the acceptance evidence for this one-shot investigation.
