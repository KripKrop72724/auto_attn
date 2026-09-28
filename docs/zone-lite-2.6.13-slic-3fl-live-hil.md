# Zone Lite 2.6.13 SLICTOWER 3FL live HIL patch

The signed 2.6.12 HIL image boots on SLICTOWER 3FL, but fresh G3 punches have produced `LIVE_PACKET_FORMAT_REJECTED`. The G3 retained those punches, and ADD recovered them later through a source scan. Recovery did not prove real-time capture or ordinary Oracle delivery: the observed ID 19 event was held as `BLOCKED_IDENTITY`. The 2.6.12 3FL campaign is paused and must not be accepted as passing HIL.

The 2.6.13 patch permits a first 36-byte live frame when exactly one interpretation validates: one extended record or three compact 12-byte records. A malformed or ambiguous frame still triggers source recovery. On rejection, the connector logs the frame length and session shape hint, without logging the user payload. The shape remains scoped to the authenticated G3 session.

The package is signed and published `HIL_ONLY` to the same five exact devices. Its storage contract adds the immutable signed 2.6.12 application digest as a qualified rollback image, while retaining the seven earlier qualified predecessors. 2.6.11 is excluded because its manifest signature failed validation and it was never deployed.

Trial 3FL alone first. Require successful OTA boot and healthy storage, followed by a fresh biometric punch that ADD records from `LIVE` without a `LIVE_PACKET_FORMAT_REJECTED` warning and delivers through the ordinary identity/Oracle path. Keep the recovered, blocked ID 19 event in review; do not force it to Oracle as a substitute for live HIL evidence. If the new image fails, verify rollback to the exact signed 2.6.12 image and preserve the G3 source. SLICTOWER 13FL remains blocked by its ESP persistence fault until hardware inspection.
