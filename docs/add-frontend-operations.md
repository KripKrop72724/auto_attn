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

Users are always scoped to one selected terminal. Directory results use cursor pagination; **Select eligible users in this view** applies only to loaded eligible rows, and the summary beside it states the exact bulk-action scope. Opening `/users` clears a previous terminal selection, while `/users/{connector}` restores explicit context.

The preferred enrollment path is a ten-minute administrator lease. Permanent elevation requires a password, an audited reason, and the exact phrase `ELEVATE {user_id} ON {device_id}`. CNIC remains write-only and masked in every response.

### Attendance

All visible and entered times are Pakistan Standard Time (`Asia/Karachi`). Browser `datetime-local` values are converted from PKT to UTC before requests, so **Today** produces identical bounds in every browser timezone. CNIC queries run only with exactly 13 digits. Searches debounce, abort superseded requests, retain prior rows during refresh, paginate, and distinguish failure from a genuine empty result.

### Reconciliation

Preflight belongs strictly to the currently selected connector and is cleared before every device switch. Complete scans remain resumable and immutable. Protected raw evidence automatically hides after 60 seconds, when the page is backgrounded, or when its drawer closes.

### Software updates and Set up a device

**Software updates** (`/updates`) opens with one status line ("18 of 20 devices are up to date · 1 updating · 1 update failed"), built with `softwareStatus()` in `src/words/software.ts`, the one rule the Devices list and the device panel also use. Spares are not counted; the line shows a skeleton until the device list arrives. Tabs: **Overview** (Life and Health panels with the devices that are not up to date, updates running now, newest version per machine brand), **Updates** (one card per update with a step strip and the pause reason in plain words; Pause, Continue, Stop update, Hide from list, Try again on this device and Clear history stay one click) and **Versions** (Withdraw version… sits in each row's ⋯ menu and keeps its note and password). **Update software** starts one update per place in one click (groups of 30). Running updates refresh every 15 s while the tab is visible. Signing keys, fingerprints, test-device plans and raw codes are under Technical details.

**Set up a device** (`/setup`) shows one "Switched off for now" card while the server's installer or setup switch is off, the State Life Device Installer card first when it is on, and the older browser setup only when the server says it can start. Past setups list by place with search and Mine · Everyone.

### Alerts

Alerts use the global cursor-paginated `GET /api/v1/alerts` queue. Filters are server-authoritative, severity ordering is explicit, safe device identity is embedded, and a request failure never renders as “No alerts.” Device names link to the exact Fleet inspector; source exceptions link to their immutable evidence view.

## Motion and accessibility

Corporate motion uses 120 ms control feedback, 180 ms content transitions, and a 240 ms maximum for dialogs, drawers, and sheets. Motion never delays data or critical actions. `prefers-reduced-motion` removes spatial movement, and forced-colors mode retains explicit boundaries. Keyboard focus, dialog semantics, 44 px touch targets, safe-area padding, screen-reader live regions, and status redundancy are required release checks.

## Release and rollback

Frontend and additive backend contracts ship as one exact-SHA replacement. Run the complete backend, frontend, Playwright, migration, accessibility, and bundle-budget gates in staging. Preserve the existing database backup and transactional rollback workflow. Legacy device-scoped alert routes remain compatible, while the new migration only adds firmware campaign idempotency state.

## Page addresses

The dashboard pages are `/home` (the default), `/devices` and `/devices/{connector}` (the device panel),
`/attendance`, `/problems`, `/people` and `/people/{connector}`, `/recover` (`?tab=recoveries | unreadable | held`),
`/updates` (`?tab=overview | updates | versions`), `/setup` and `/settings`. `/status` is the public page and is
separate. Old addresses keep working: `/fleet` opens Home, `/fleet/{connector}` the device, `/users` People,
`/alerts` Problems, `/reconciliation` Recover attendance (old tab names are translated, `job_id` and `device_id`
kept), `/firmware` Software updates and `/firmware?tab=prepare` Set up a device; very old `#users`-style links work
too. Every redirect keeps the query string and `?division=`; `routing.ts` `legacyRedirect()` holds the table and
`routing.test.ts` tests every row.

Each page is mounted by one module in `src/routes/` that reads the shared app data (`src/AppData.tsx`) and passes
the page the same props as before, so a page change never needs `App.tsx`. The browser keeps at most one storage
key, `add.prefs` (Division, technical details, menu note), written only when the person changes a choice.

## Releasing a dashboard change

Gates, all in the PR's CI:

1. `pnpm test`, `pnpm run build` and `pnpm run budget` (paste the headroom table into the PR).
2. Playwright on every project plus `phone-390-touch`; zero Content Security Policy violations (`vite preview`
   serves the production policy from `security-headers.conf`); storage and URL privacy checks.
3. axe with the WCAG 2.2 AA tags; the page sweep (`tests/e2e/guards.spec.ts`) reports tap targets, system words and
   motion as annotations.
4. Screenshots: the `add-ui-screenshots` artifact holds full-page PNGs of every page at 390 px (touch) and 1440 px
   (`screenshots/{page}-{project}.png`). Download it with `gh run download <run> -n add-ui-screenshots` and send
   the changed screens with the PR description (`.github/pull_request_template.md`).

Live check after every deploy (read-only, about 15 minutes, on the owner's signed-in browser; no action buttons):

1. Before merging, note `/status` (devices, online, offline), the newest check-in and the check-ins received and sent
   to the HR system in the last 15 minutes.
2. Confirm the "ADD production deployment" status is success for the merged commit.
3. At 1440×900 and 375×812, open every page the PR touched, screenshot it and read the console (no errors, no CSP
   warnings). First paint never shows "0 of 0"; the device list is requested at most every 5 seconds.
4. Check-ins keep arriving and reaching the HR system within 10 minutes; otherwise stop and roll back.
5. `/status` is unchanged in words and counts.

Rollback: redeploy the previous live commit with "Deploy ADD" (`deploy_sha`), then open a revert PR on main. An
open page that was loaded before a deploy shows "A new version of the dashboard is ready." with Reload, and a page
file missing after a deploy reloads the page once by itself before showing "This page couldn't open".
