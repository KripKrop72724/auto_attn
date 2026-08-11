# Zone Lite 2.4.10 captive portal event-order hardening

Zone Lite 2.4.10 supersedes the reviewed-but-unpublished 2.4.9 candidate. It
retains event-driven client authorization while closing both possible ordering
edges between the Wi-Fi association callbacks and the DHCP assignment callback.

Each physical association has an explicit generation, and an IP lease is usable
only when tagged with that same generation. When DHCP is delivered before the
queued connect callback, the handler confirms the exact MAC against the Wi-Fi
driver's current SoftAP association list, begins the generation, and caches the
lease; the matching connect callback then pairs with it. Every disconnect is a
generation boundary even if the driver already shows a fast same-MAC reconnect,
and any unpaired same-MAC connect also begins a new generation. Thus neither a
missing/delayed disconnect nor a delayed DHCP/connect callback can carry the
previous connection's address forward. A replacement client likewise clears
the prior lease immediately.

The association lookup occurs only once for a DHCP event. No associated-station
or DHCP table is queried by an HTTP request, so the intermittent 2.4.8 request
race is not reintroduced. Requests still require the dedicated setup subnet and
the exact current DHCP address, with a bounded 1.5-second wait when the first
captive request reaches the HTTP task just ahead of event delivery.

Release 2.4.10 only as an exact-MAC `HIL_ONLY` candidate first. Promotion remains
blocked until the attached board passes repeated page loads, assets and API
requests, concurrent captive probes, failed-credential rollback, reboot, and
normal attendance-operation validation.
