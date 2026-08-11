# Zone Lite 2.4.7 captive setup portal release

Zone Lite 2.4.7 preserves the 2.4.6 SoftAP beacon fix and corrects the full
captive-portal delivery path. Physical HIL showed that macOS captive-network
probes could occupy the firmware's two HTTP sessions before the browser loaded
the State Life page. The ESP associated clients and assigned DHCP correctly,
but then refused port 80 connections.

The portal now uses the ESP-IDF safe maximum of seven client sessions, closes
each stateless response, recognizes the standard Apple, Android, and Windows
captive-probe paths, and supports sixteen handlers. A user-requested network
scan also cancels any still-running disconnected station probe before scanning,
so the first list does not fail with `ESP_ERR_WIFI_STATE`.

The State Life interface presents the complete three-step flow, signal and
security information, hidden-network entry, password visibility control, and
clear test/save status. Joining the protected, one-client WPA2 setup AP remains
the authentication boundary. The redundant second entry of that same setup
password was removed; a random per-boot CSRF token and strict AP-interface and
subnet checks continue to protect configuration writes. Candidate credentials
are committed to encrypted NVS only after a successful station connection, and
failure restores the prior network.

Publish the first bytes as exact-MAC `HIL_ONLY`, prove the rendered page,
network discovery, rejection/rollback, successful save, reboot persistence,
and normal station operation on physical hardware, then use the existing
production-canary gate before marking the immutable release `AVAILABLE`.
