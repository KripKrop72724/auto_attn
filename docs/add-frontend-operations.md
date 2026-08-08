# ADD frontend operations

The Attendance Device Dashboard is the single administrator interface for the national ADD fleet. Fleet inventory is the home workspace; Users, Attendance, Reconciliation, Firmware, and Alerts preserve the same safety and audit boundaries on desktop and mobile.

## Navigation and live state

Desktop keeps the permanent State Life navigation rail. Mobile presents Fleet, Users, Attendance, and Alerts in the primary bottom navigation; Reconciliation and Firmware remain available from **More**. Device details open as a full-screen mobile sheet.

The header reports one of four realtime states:

- **Connecting**: the browser is establishing the authenticated SSE stream.
- **Live sync**: named events are being received and the last successful synchronization is current.
- **Reconnecting**: cached data remains visible while the browser reconnects.
- **Cached data**: the stream has been unavailable for more than 30 seconds. Affected summaries are refreshed every 30 seconds until a complete resynchronization succeeds.

Attendance, alerts, users, commands, reconciliation, identity, logs, firmware, backend errors, and device events invalidate only their affected views. Event bursts are coalesced to prevent duplicate requests.

## Workspace behavior

### Fleet

Fleet is the operational home and authoritative inventory. Status includes text, icon, and border pattern. Last-contact time and connectivity reasons remain visible in the device inspector. A pending command can be cancelled through the real command-cancellation endpoint before execution.

### Users

Users are always scoped to one selected terminal. Directory results use cursor pagination; **Select eligible on loaded rows** states the exact bulk-action scope. Opening `/users` clears a previous terminal selection, while `/users/{connector}` restores explicit context.

The preferred enrollment path is a ten-minute administrator lease. Permanent elevation requires a password, an audited reason, and the exact phrase `ELEVATE {user_id} ON {device_id}`. CNIC remains write-only and masked in every response.

### Attendance

All visible and entered times are Pakistan Standard Time (`Asia/Karachi`). Browser `datetime-local` values are converted from PKT to UTC before requests, so **Today** produces identical bounds in every browser timezone. CNIC queries run only with exactly 13 digits. Searches debounce, abort superseded requests, retain prior rows during refresh, paginate, and distinguish failure from a genuine empty result.

### Reconciliation

Preflight belongs strictly to the currently selected connector and is cleared before every device switch. Complete scans remain resumable and immutable. Protected raw evidence automatically hides after 60 seconds, when the page is backgrounded, or when its drawer closes.

### Firmware

Campaign creation requires `POST /api/v1/firmware/campaigns/preflight`. The response lists exact eligible and excluded devices, exclusion reasons, offline count, expiration, and a signed five-minute scope token. Campaign creation sends that token plus a fresh idempotency key; changed or expired scope must be previewed again. Releases can be revoked from the protected release inventory with a reason and password.

### Alerts

Alerts use the global cursor-paginated `GET /api/v1/alerts` queue. Filters are server-authoritative, severity ordering is explicit, safe device identity is embedded, and a request failure never renders as “No alerts.” Device names link to the exact Fleet inspector; source exceptions link to their immutable evidence view.

## Motion and accessibility

Corporate motion uses 120 ms control feedback, 180 ms content transitions, and a 240 ms maximum for dialogs, drawers, and sheets. Motion never delays data or critical actions. `prefers-reduced-motion` removes spatial movement, and forced-colors mode retains explicit boundaries. Keyboard focus, dialog semantics, 44 px touch targets, safe-area padding, screen-reader live regions, and status redundancy are required release checks.

## Release and rollback

Frontend and additive backend contracts ship as one exact-SHA replacement. Run the complete backend, frontend, Playwright, migration, accessibility, and bundle-budget gates in staging. Preserve the existing database backup and transactional rollback workflow. Legacy device-scoped alert routes remain compatible, while the new migration only adds firmware campaign idempotency state.
