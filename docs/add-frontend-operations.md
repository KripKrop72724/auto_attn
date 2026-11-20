# ADD frontend operations

The Attendance Device Dashboard is the single administrator interface for State Life's attendance devices. **Home** is the first page; **Devices**, **Attendance**, **Problems** and **People** are the everyday pages, and **Advanced tools** holds **Recover attendance**, **Software updates**, **Set up a device** and **Settings**. Old addresses (`/fleet`, `/users`, `/alerts`, `/reconciliation`, `/firmware`) redirect to the new pages with their filters kept. The same safety and audit boundaries apply on desktop and mobile.

## Navigation and live state

From 1024 px the menu is a sidebar with the five everyday pages, an "Advanced tools" group and, at the bottom, "All times are Pakistan time" and a link to the public status page. Between 761 and 1023 px it is a labelled rail (icon over the name). On phones (760 px and less) the bottom bar is **Home · Devices · Attendance · Problems · More**; **More** opens a sheet with People, the Advanced tools, Settings, who is signed in, Sign out and "Show technical details". "Set up a device" shows a grey "Switched off" tag while provisioning is switched off. The Problems item shows how many problems nobody has looked at yet.

The header is opaque. On the left is the **All · Life · Health** switch (on phones a full-width row at the top of the page); the choice is kept in the page address (`?division=life`) and, once changed, in the one browser key `add.prefs`. When Life or Health is chosen, "Showing Life only · Show all" sits above the page title. On the right are the Live indicator and the account menu (Settings, the public status page, "Show technical details", Sign out).

The Live indicator says in words whether the page is live:

- **Connecting…**: the browser is opening the live stream.
- **Live**: changes show up as they happen.
- **Reconnecting…**: the last data stays on screen while the browser reconnects.
- **Offline · showing last saved data** (phone: **Offline**): the stream has been down for 30 seconds or a background refresh failed after its retries. A slim banner under the header says since when and offers **Try now**. Background failures never pop up a message.

Attendance, problems, people, requests, recoveries, logs, software updates and device events refresh only the views they affect. Event bursts are coalesced to prevent duplicate requests. Motion is short (fades of 120 ms, sheets 200 ms, panels 250 ms maximum) and off under reduced motion.

## Workspace behavior

### Home

Four numbers (Devices, Working, Needs attention, Offline) counted from the same device rows the Devices list shows; then a Life panel and a Health panel that list only the devices that are not working, worst first, with the reason and since when; today's attendance per division (received, sent to the HR system, waiting, not matched to an employee) with the automatic-sending line; the newest problems nobody has looked at; spare devices and temporary admin access. The map is gone. A device whose machine is not responding counts as Offline, as on the public status page.

### Devices

Every device, the ones that need attention first, then by name with numbers in order. Filters: status, city, machine brand, spare devices, software version and one search box for name, city, code or serial. Every filter is in the page address, so a filtered view can be shared; opening a device and closing it brings the same filtered list back. Codes, MAC, IP and serial are in the device's "Technical details".

### Settings

"Show technical details" (opens every Technical details box), check-in phone messages (the settings dialog and its password step are unchanged), contact IT, and **Use the old screens for now** (`/classic/`).

### People (was Users)

`/people` lists every device at once, grouped Life then Health by place name, with people counts and "need fixing"
counts from `GET /api/v2/people/summary` (hidden, never shown as 0, when that request fails). Spare devices are hidden
until **Show spare devices**. `/people/{connector}` shows the people on one machine: one summary line, the tabs
People · Same CNIC · Old check-ins, a table on a computer and one card per person on a phone, and a person sheet with
fix steps. Filters live in the address (`q`, `show`, `problem`, `role`, `shift`, `changing`, `sort`, `tab`); a
13-digit CNIC search is sent as the `cnic` API parameter and never enters the address. Changing device keeps the
quick view and clears the search and ticks. `?user_id={Employee ID}` opens Edit for that person; `?q={Employee ID}`
opens the person sheet.

Every change to a machine is followed in **Changes on this machine** (Waiting → Sent to machine → Confirmed, or
Failed with Try again); several can run at once. Safeguards are unchanged: the password on every add, edit,
temporary admin access and machine confirmation; the typed Employee ID or name to remove one person; a reason of 10+
letters, the server's exact phrase `DELETE {n} USERS FROM {device_id}` and the password to remove several (the
selection is re-checked before the dialog opens and again before sending); typed `ELEVATE {user_id} ON {device_id}`
for a permanent machine admin; typed `SAME EMPLOYEE` / `REVOKE RESOLUTION` for Same CNIC; the typed HR-details phrase;
typed `REVOKE ACCESS` to end temporary admin access; the password to stop a removal. Machine admins, people with a
change being sent and damaged entries (`looks_damaged`) can't be ticked or removed. The preferred way to add
fingerprints is temporary admin access (10 minutes, ends by itself). CNIC remains write-only and masked in every
response.

### Attendance

All visible and entered times are Pakistan Standard Time (`Asia/Karachi`). Browser `datetime-local` values are converted from PKT to UTC before requests, so **Today** produces identical bounds in every browser timezone. CNIC queries run only with exactly 13 digits. Searches debounce, abort superseded requests, retain prior rows during refresh, paginate, and distinguish failure from a genuine empty result.

### Recover attendance

Preflight belongs strictly to the currently selected connector and is cleared before every device switch. Complete scans remain resumable and immutable. Protected raw evidence automatically hides after 60 seconds, when the page is backgrounded, or when its drawer closes.

### Software updates

Campaign creation requires `POST /api/v1/firmware/campaigns/preflight`. The response lists exact eligible and excluded devices, exclusion reasons, offline count, expiration, and a signed five-minute scope token. Campaign creation sends that token plus a fresh idempotency key; changed or expired scope must be previewed again. Releases can be revoked from the protected release inventory with a reason and password.

### Problems (was Alerts)

`/problems` (old `/alerts` links redirect) reads `GET /api/v1/alerts`. Tabs **New** (`queue=NEEDS_ACTION`, the same number as the menu badge), **Seen** (`queue=SEEN`, open and marked as seen) and **Fixed** (`queue=RESOLVED`, `order=fixed_newest`, last 7 days by default, 50 per page with Show more). Tab and chip counts come from the response's `counts`; with an older backend the page falls back to `queue_totals`. Problems are grouped by device (friendly name and Life / Health tag), worst first; "Newest first" and "Oldest first" show a flat list.

Filters live in the link: `tab`, `serious` (urgent, important, low = all five backend levels), `type`, `device`, `when` / `from` / `to` (Pakistan days), `q`, `att` (affects attendance), `sort`, `old` (the old "seen" marks from before 10 Oct, `queue=LEGACY_SEEN`, hidden behind one note), plus the frame's `division`. Live changes (alert and device events) re-read the first page at most every 10 s and merge it by id, so pages from Show more stay and the list does not jump. A failed background refresh shows a thin "Couldn't update" bar, never a toast.

Each card shows plain words (`plain_title`, `plain_detail`, `next_step` from the backend, else `fe/words/problems.ts`), a router link to the page that fixes it, and the code, system message and allow-listed details only under Technical details. **Mark as seen** is one click (optionally with a note). **Mark as fixed** appears only when the row's `operator.resolvable` is true and keeps the reason (10 letters) and the password, one request key per dialog. **Check them** on a set-aside check-ins problem opens the set-aside sheet (`/api/v1/attendance-quarantine`; showing the original record and marking it as checked keep reason and password). **Clean up old problems** is in the ⋯ menu.

## Motion and accessibility

Motion uses 120 ms fades, 200 ms sheets and a 250 ms maximum for panels and dialogs. Motion never delays data or critical actions. `prefers-reduced-motion` removes spatial movement, and forced-colors mode retains explicit boundaries. Keyboard focus, dialog semantics, 44 px touch targets, safe-area padding, screen-reader live regions, and status redundancy are required release checks.

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
