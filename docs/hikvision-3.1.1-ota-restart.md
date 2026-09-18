# HIK Zone Lite 3.1.1

The first remote HIK 3.0.9 → 3.1.0 campaign proved signed download, image
verification and OTA slot selection, but exposed a pre-existing restart gate:
`add_connector_claim_ota_restart` required ZKT session state even on HIK builds.
The old image waits indefinitely at `READY_TO_BOOT / WAITING_FOR_ZKT_SAFEPOINT`.
It has no supported ADD ESP-reboot command; a power-cycle is necessary to boot
the already verified image. Publishing another image cannot change code that
is currently running and blocked in that wait.

3.1.1 separates the HIK restart gate. The OTA task acquires and retains the
terminal-request mutex and source-uploader mutex until reset. This waits for
in-flight polling, terminal mutations, history/checkpoint writes and source
receipt settlement, then prevents a new operation racing reboot. The gate also
requires initialized workers, the restored poll checkpoint and verified healthy
storage. It does not depend on a reachable external terminal or ZKT fields.
Durable queued observations may safely replay after boot. ZKT gates stay intact.
The HIK wait reason is `WAITING_FOR_HIK_SAFEPOINT`.

An ESP still running 3.0.9 or 3.1.0 needs a power-cycle after its verified download;
the automatic restart fix applies to subsequent updates initiated from 3.1.1.
For the already-staged 3.1.0 device, that means booting 3.1.0, downloading 3.1.1
through ADD, and power-cycling once more. Never report `READY_TO_BOOT` as a
completed OTA: version, application hash, slot and final ADD acknowledgement
must all be verified.
