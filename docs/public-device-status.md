# Public device status page and API

`https://attendancedevices.slichealth.com/status` shows management whether each attendance device
is **Online** or **Offline**, and since when. It needs no sign-in, and neither does its API, so
other systems such as APEX dashboards can use it.

## What Online and Offline mean

- **Online**: the device's attendance is reaching ADD. Devices the dashboard shows as degraded or
  online with warnings are Online here too.
- **Offline**: ADD cannot receive the device's attendance. Either the ESP is not connected, or the
  ESP is up but cannot read its terminal: unreachable for more than 5 minutes, failing its checks,
  or no terminal bound.

A change shows only after it holds for 2 minutes (to Offline) or 1 minute (back to Online). This
way an ESP reconnect, a planned terminal restart or an ADD restart does not make a device flicker or
reset its "since". The settings are `ADD_PUBLIC_STATUS_OFFLINE_GRACE_SECONDS` and
`ADD_PUBLIC_STATUS_ONLINE_GRACE_SECONDS`.

Devices whose name starts with `LF` are State Life **Life** zone devices. All others are
**Health**. Names are shown in plain words: `LF-ZONE-BLD9-03` is "Building 9 · Device 3",
`LF-ZONE-G&PBLD8-01` is "Group & Pension Building 8", `ZONE-SLICTOWER-13FL` is
"SLIC Tower · 13th Floor" and `ZONE-SWAT-01` is "Swat". A device number shows only where a place
has several devices. Spare connectors are not listed.

## API

Every endpoint is read-only, needs no credentials and allows any origin. Add `?zone=life` or
`?zone=health` to any of them to get one zone only.

| Endpoint | Use |
| --- | --- |
| `GET /api/public/v1/status` | JSON snapshot. Suits APEX REST data sources and polling. |
| `GET /events/public/v1/status` | Server-sent events. An `event: status` frame carries the full snapshot whenever anything changes, and again every 30 seconds. |
| `wss://…/api/public/v1/status/ws` | WebSocket. Each text message is the full snapshot, sent on every change and every 30 seconds. |

ADD refreshes the snapshot every 10 seconds. At most 200 live connections (event streams plus
WebSockets) are served at once; beyond that, the event stream answers 503 with `Retry-After` and
the WebSocket closes with code 1013.

```json
{
  "updated_at": "2026-10-24T11:30:00Z",
  "total": 20,
  "online": 15,
  "offline": 5,
  "zones": [
    {"zone": "life", "name": "Life", "total": 6, "online": 2, "offline": 4},
    {"zone": "health", "name": "Health", "total": 14, "online": 13, "offline": 1}
  ],
  "devices": [
    {
      "name": "Building 9 · Device 3",
      "location": "Building 9",
      "unit": "Device 3",
      "zone": "life",
      "code": "LF-ZONE-BLD9-03",
      "status": "offline",
      "since": "2026-10-21T10:17:52Z"
    }
  ]
}
```

- `status` is `online` or `offline`. `since` is when that status began, in UTC.
- `code` is the device's name in ADD. `name` is the plain-words name the page shows; `location` and
  `unit` are its two parts (`unit` is null when only the place is shown).
- Devices are sorted by zone (Life first) and then by name.

The snapshot holds names and statuses only: no addresses, serial numbers, firmware, users or
attendance.
