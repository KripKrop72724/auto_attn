# Employee check-in phone notifications

After Oracle confirms a punch, ADD asks the HRM service to push an "attendance
punched" message to the employee's phone:

```
POST <host>/ords/slic_hrm/ESS_APIS/attendance_checkin_notify
X-Api-Key: <secret ADD_CHECKIN_NOTIFY_API_KEY>
Content-Type: application/json
{"cnic": 6110113371079}
```

The CNIC is the exact CNIC ADD delivered to Oracle for that punch, sent as a
JSON number (`ADD_CHECKIN_NOTIFY_CNIC_JSON_TYPE=string` sends it as a string).
Notifications never delay or change Oracle delivery: a failed notification
leaves the punch and its Oracle status untouched.

## Set the secret and deploy

1. GitHub → repository settings → Environments → `add-production` → add the
   secret `ADD_CHECKIN_NOTIFY_API_KEY` with the key from the HRM team. Never
   paste the key into an issue, chat, commit or log.
2. Run the normal ADD deployment. The workflow passes the secret to
   `deploy.ps1` exactly like `ADD_ORDS_PASSWORD`; it is stored in the protected
   `.env.add` and redacted from failure diagnostics.
3. Without the secret the deployment still succeeds and the feature shows
   **Not configured** (nothing is sent; punches get "Not sent · not configured").
   A key must not contain a single quote (the deployment refuses such values).

ADD runs inside the organisation network, so the primary URL is the
`ADD_ORDS_BASE_URL` host (`https://local.slichealth.com/...`). The fallback
`ADD_CHECKIN_NOTIFY_FALLBACK_URL` (default
`https://eclaim2.slichealth.com/ords/slic_hrm/ESS_APIS/attendance_checkin_notify`)
uses the same key and is tried **only** when the attempt on the other host
provably never reached a server: DNS failure, connection refused, connect
timeout or TLS handshake failure. After a read timeout, a reset after sending,
or any HTTP response, ADD never repeats the request on the other host. The host
that last delivered stays preferred; the primary is re-probed every 10 minutes.

## Modes and rollout

The live mode is stored in the database and switched at runtime from
**Attendance → All events → Phone notifications → Settings** (administrator
password and an audited reason required; no redeploy):

| Mode  | Effect |
|-------|--------|
| OFF   | Send nothing. The emergency stop. |
| PILOT | Send only for the listed zone IDs and/or employee CNICs. |
| ALL   | Send for every zone. |

The owner's decision (22 Oct 2026) is ALL zones at once, so the first run seeds
**ALL** when nothing else is configured. The repository variable
`ADD_CHECKIN_NOTIFY_MODE` (OFF, PILOT or ALL) changes that first seed only; once
the feature has run, the Settings dialog is the only switch.

Optional smoke test with your own CNIC before or after going ALL:

1. Settings → Mode **Pilot**, add your own 13-digit CNIC (stored hashed, shown
   masked), reason "Pilot with my own CNIC", password → Save.
2. Punch on any terminal. Within seconds of Oracle's confirmation the pill on
   that punch turns **Notified hh:mm** and your phone receives the message.
3. Settings → Mode **All zones**, reason "Owner approved all zones" → Save.

Punch policy: **Every punch** (default) or **First punch of the day** (PKT).

## Anti-spam rules

* Only punches confirmed by Oracle after this feature started can notify.
  Earlier punches have no notification row and show "Not sent · before feature".
  The attendance table is never backfilled.
* A punch whose Oracle confirmation arrives more than 12 hours after the punch
  time (`ADD_CHECKIN_NOTIFY_STALE_WINDOW_HOURS`) is "Not sent · old punch":
  backlog releases, history replays and reconciliations never notify.
  Invalid or future punch times are "Not sent · invalid time".
* Repeat punches by the same CNIC within 2 minutes
  (`ADD_CHECKIN_NOTIFY_COALESCE_SECONDS`) produce one message; the others show
  "Not sent · grouped".

## Reading the pill

| Pill | Meaning |
|------|---------|
| Notified hh:mm | The HRM service accepted the request at that time (PKT). |
| Pending | Queued; sends within seconds. |
| Retrying | A network error, timeout, HTTP 429 or 5xx; retried with backoff for up to 6 hours. |
| Failed | The HRM service refused it (another HTTP 4xx or `success: false`), or 6 hours of retries ran out. Click it for the reason; an administrator can **Retry notification**. |
| Not sent · reason | Deliberately not sent: before feature, old punch, invalid time, no CNIC, CNIC uncertain (the terminal delivered the punch itself with a CNIC that differs from ADD's saved one), grouped, not first of day, outside pilot, disabled, not configured, or awaiting Oracle. |

Filter by these states with **Filters → Phone notification**. The strip above
the list shows the mode, health and the last 24 hours' counts.

## Troubleshooting

* **Not configured**: the secret is missing from `add-production`; add it and
  redeploy.
* **Paused after failures**: 5 consecutive network/5xx/401/403 failures pause
  sending (60 s, doubling to 15 min). One probe resumes it. HTTP 401/403 means
  the key is wrong or revoked: ask HRM, update the secret, redeploy. Waiting
  notifications resume automatically; nothing is lost while paused.
* **Using public fallback host** in the strip: ADD cannot reach
  `local.slichealth.com`; check internal DNS and routing.
* A `CHECKIN_NOTIFY_DEGRADED` alert opens on a device when its notifications
  wait more than 10 minutes or 3 fail within an hour. It never affects device
  health and clears by itself when notifications flow again.
* API: `GET /api/v1/checkin-notifications` (mode, counts, last success, oldest
  waiting age, consecutive failures); `GET /api/v1/overview` includes the same
  health under `checkin_notifications`.

## Delivery guarantees

Each punch gets at most one notification row, written in the same database
transaction that records Oracle's confirmation, so a confirmation can never be
missed or doubled. The worker leases rows (`FOR UPDATE SKIP LOCKED`), bounds
every request to 10 s, caps concurrency and rate, and survives restarts and
multiple processes. Delivery is at least once: if ADD stops after the HRM
service received a request but before ADD recorded the answer, or a request
times out after the service received it, that punch is sent again on retry.
This rare window is the only way an employee can receive a duplicate message.
Logs never contain the key or a full CNIC (only `*****-****123-4`).
