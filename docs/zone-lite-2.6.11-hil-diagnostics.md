# Zone Lite 2.6.11 persistence diagnostic HIL patch

The signed 2.6.10 image downloaded and booted on SLICTOWER 13FL, but failed the required SPIFFS and NVS persistence proof and remained `BOOTED_PENDING`. This release retains the same attendance and credential behavior and exact five-device HIL scope. It reports the specific failed proof step and retries an interrupted proof at most twice before latching a durability fault. Boot confirmation still requires a successful filesystem write, sync, read, delete, NVS commit, and NVS readback. An established storage fault remains latched.

The storage contract adds only the signed 2.6.10 application digest as a qualified predecessor. The original 2.4.12, 2.5.2, and prior HIL application digests remain exact. Swat, SLICTOWER 13FL, and SLICTOWER 3FL can start independent HIL campaigns only under the reviewed identity scope in `deploy/add/hil-targets-2.6.11.json`. Peshawar remains gated by formal first-stage acceptance. No nationwide promotion follows publication or boot confirmation alone.

SLICTOWER 3FL still needs the exact one-device 2.5.2 bridge from factory 2.4.12 before a direct 2.6.x OTA. Its bridge campaign is pending until the connector requests an authenticated offer. Do not bypass the OTA predecessor guard or mark 3FL passed while that request has not occurred. The offline spare is excluded.
