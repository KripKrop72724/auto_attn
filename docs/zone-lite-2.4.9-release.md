# Zone Lite 2.4.9 deterministic captive portal client authorization

Zone Lite 2.4.9 supersedes the rejected, never-promoted 2.4.8 canary. Physical
acceptance testing showed that querying ESP-IDF's associated-station and DHCP
lease tables on every HTTP request was not atomic. A station could be visible
before its lease was populated, causing legitimate captive clients to receive
intermittent or persistent `404 Not found` responses.

The portal now consumes the authoritative SoftAP lifecycle events instead:

- `WIFI_EVENT_AP_STACONNECTED` records the sole protected AP client and clears
  any stale lease.
- `IP_EVENT_AP_STAIPASSIGNED` atomically caches the DHCP-assigned client address.
- `WIFI_EVENT_AP_STADISCONNECTED` immediately revokes the matching lease.
- A first HTTP request that races the IP callback waits at most 1.5 seconds for
  the event, then checks the exact cached address again.
- Portal start, stop, and every client replacement clear authorization state.

The request must still originate in `192.168.254.0/24` and match the exact
current DHCP assignment. The protected one-client WPA2 setup AP, per-boot CSRF
token, dormant-by-default lifecycle, test-before-save flow, encrypted atomic
NVS storage, OTA exclusion, and rollback safeguards remain unchanged.

The authorization state machine has host-executed lifecycle coverage for event
ordering, stale leases, renewals, replacement clients, and disconnects. Release
2.4.9 only as exact-MAC `HIL_ONLY` first. It may move to the Swat canary and then
the existing gated nationwide rollout only after repeated physical page loads,
asset/API requests, concurrent captive probes, credential rollback, reboot,
and normal attendance operation all pass on the attached board.
