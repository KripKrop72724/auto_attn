# Zone Lite 2.4.12 nationwide captive-portal release

Zone Lite 2.4.12 is the production-canary successor to the physically verified
2.4.11 dual-stack captive-portal correction. It contains the same request-path
fix: native IPv4 and IPv4-mapped IPv6 socket endpoints are normalized before
the setup-portal guard checks the exact `192.168.254.1` SoftAP destination,
dedicated client subnet, and current associated DHCP lease.

Version 2.4.11 remains immutable and quarantined to the Building 9 physical-HIL
ESP. Its target marker must not be edited or removed. Build and sign 2.4.12 from
this exact commit as a new immutable candidate targeted to the production
`ZONE-SWAT-01` MAC. Flash the same signed 2.4.12 application bytes on the
attached physical ESP and repeat the portal gate before starting the Swat OTA.

Promotion remains blocked until Swat completes OTA without rollback, returns
to `ONLINE`, `OTA_READY`, `LIVE_CAPTURE`, and certified terminal-source parity,
and every resolvable attendance row is Oracle-confirmed. Preserve missing or
conflicting identities in their existing fail-closed states. After audited
canary acceptance, promote the identical signed bytes and use the central ADD
runner to update one zone-scoped batch at a time, stopping on any regression.
