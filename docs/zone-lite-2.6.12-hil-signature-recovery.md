# Zone Lite 2.6.12 HIL signature recovery

The 2.6.11 HIL package was signed and published on 28 September 2026, but ADD rejected its manifest with `InvalidSignature` and returned HTTP 500 for the entire release catalog. The image hash matched its manifest and the signature verified against the manifest's raw bytes. The raw JSON differed from ADD's recursively sorted canonical JSON because the new `2.6.10` predecessor key followed `2.6.9` in the nested image map. No 2.6.11 campaign was created.

On the production host, the package remains intact under `firmware/2.6.11`. Its `manifest.json` was renamed to `manifest.invalid.json` so the active release scanner ignores this invalid package. This restored the 83 existing releases and both OTA channel displays. The 2.6.11 release identity is abandoned and must not be republished or deployed.

2.6.12 carries the same persistence diagnostic firmware behavior and the same seven qualified predecessor images as 2.6.11. The signing step now canonicalizes JSON recursively before signing and verifies the signature with the vault public key. Publication rejects a noncanonical manifest before it enters the live store. The package remains `HIL_ONLY` for the same five exact devices: Swat, SLICTOWER 13FL, SLICTOWER 3FL, and both Peshawar terminals.

First trial 13FL alone. Require OTA boot confirmation, healthy flash and NVS persistence evidence, preserved attendance, and physical biometric versus card/PIN checks before HIL acceptance. The 3FL factory-partition connector must first confirm its exact 2.5.2 bridge campaign; a pending assignment is not a successful bridge. Keep national promotion closed until all five independent HIL gates pass.
