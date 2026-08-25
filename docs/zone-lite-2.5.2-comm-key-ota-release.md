# Zone Lite 2.5.2 memory-safe COMM Key recovery release

Zone Lite 2.5.2 supersedes 2.5.1 after the first production staged-recovery boot
in Quetta reached `BOOTED_PENDING`, acknowledged the queued configuration
operation, and then returned to the proven 2.4.12 image without applying the
revision or reporting runtime health. The bootloader preserved service by
falling back; the nationwide rollout remained fail-closed.

The observed sequence is consistent with the remaining 2.5.1 internal-RAM
allocation boundary. That boot path allocates the established uploader and
gateway tasks, then allocates a new 12 KiB COMM Key worker stack. A connector
with more runtime state can lack that final contiguous internal-memory block
even while aggregate PSRAM telemetry is healthy. The controlled recovery reboot
then causes the unconfirmed image to fall back, which matches the Quetta
evidence. The allocation branch is an evidence-backed diagnosis rather than a
serial-console measurement; no Quetta serial console was available remotely.

2.5.2 removes that additional task and stack. Pending `APPLY_CONFIG` work is
serialized through the existing 24 KiB gateway task before ordinary terminal
discovery. Configuration and live terminal sessions still cannot overlap; the
sealed key, exact serial authentication, encrypted-NVS commit, revision checks,
and zeroization rules are unchanged.

## Mandatory gates

1. Build the exact reviewed 2.5.2 commit with ESP-IDF 5.5.3 and pass all firmware,
   backend, frontend, source-ledger, and Oracle preflight checks.
2. Run a fresh exact-MAC Swat HIL. Require download, signed-image verification,
   candidate boot, runtime health, `SUCCEEDED`, normal attendance capture, a
   complete certified snapshot, and non-regressing user and punch counts.
3. Publish and promote only those exact signed bytes. Keep 2.5.1 available only
   as historical evidence; do not offer it to another device.
4. Close the failed Quetta 2.5.1 campaign, create a one-device 2.5.2 recovery
   campaign, and perform one coordinated ESP-only power cycle after the image is
   `READY_TO_BOOT`.
5. Require the staged operation to reach applied revision 1, authenticate the
   pinned Quetta terminal serial, restore `ONLINE`/`LIVE_CAPTURE`, complete the
   terminal snapshot, and finish the OTA deployment as `SUCCEEDED`.
6. Start the serial nationwide workflow only after Quetta is accepted and no
   active or paused firmware campaign remains. Batch size remains one zone.

No COMM Key value, sealed envelope, credential, or managed-key material belongs
in release logs, commands, manifests, screenshots, or audit reasons.
