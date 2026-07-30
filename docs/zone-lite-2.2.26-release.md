# Zone Lite 2.2.26 corrective release

## Purpose

Zone Lite 2.2.26 prevents a large ADD identity catalog from blocking the ESP
WebSocket event callback during first boot. The defect was exposed when the
SWAT 2.2.25 canary with 35 users succeeded but the first 731-user nationwide
target safely returned `BOOTLOADER_ROLLBACK` before it could publish
`BOOTED_PENDING`.

## Firmware changes

- Complete inbound ADD WebSocket messages are transferred to a dedicated,
  bounded worker queue before JSON parsing, encryption, or durable storage.
- The WebSocket event callback only validates and reassembles fragments, so a
  large identity catalog cannot starve the transport event task.
- JSON is parsed directly from the already NUL-terminated reassembly buffer,
  removing one full-payload allocation and copy.
- Queue exhaustion remains fail-closed: the complete message is freed and
  rejected without corrupting the currently persisted identity catalog.

## Rollout gates

1. Build and contract tests must pass.
2. Publish 2.2.26 as an immutable HIL-only candidate for `ZONE-SWAT-01`.
3. SWAT must boot, report runtime health, preserve at least 26,436 punches and
   35 users, return to `ONLINE` or `LIVE_CAPTURE`, and maintain clean Oracle
   delivery.
4. Promote only the exact SWAT-tested bytes.
5. Retry one 700-plus-user zone first. It must publish `BOOTED_PENDING` and
   `RUNTIME_HEALTHY`, preserve counts, and reach `SUCCEEDED`.
6. Continue one zone at a time. Stop on any rollback, count regression,
   connectivity regression, or unsafe Oracle delivery state.
