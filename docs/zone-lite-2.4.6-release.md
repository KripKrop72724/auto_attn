# Zone Lite 2.4.6 setup AP reliability release

Zone Lite 2.4.6 preserves the 2.4.5 reconciliation behavior and fixes a
physical-HIL-discovered recovery portal failure. When station credentials were
unreachable, immediate reconnect scans repeatedly occupied the shared Wi-Fi
radio after the SoftAP started. The firmware reported that recovery mode was
active, but nearby clients could not reliably discover its beacon.

While the setup portal is active, disconnect events now remain owned by the
portal and station recovery is limited to one bounded probe per minute. This
gives the SoftAP a quiet discovery window. Closing the portal immediately
restores station-only connection behavior. The existing automatic recovery
delay, manual BOOT-button activation, authenticated setup flow, encrypted NVS,
pending-credential rollback, and secure-boot requirements are unchanged.

Publish the first image as an exact-MAC `HIL_ONLY` candidate and verify the AP
with an independent Wi-Fi scan before any wider promotion.
