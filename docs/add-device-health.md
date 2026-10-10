# ADD device health

ADD derives each connector's `lifecycle_state` and `last_error_code` from one
evaluation, `zk_add/device_health.py::evaluate_health()`. Its inputs are the
connector's OPEN alerts, each bound to the boot and firmware that raised it,
and a terminal-link status computed from `add_zkt_devices`. DEGRADED now means
a real device fault backed by current evidence. Stale bookkeeping, terminal
outages and ADD's own failures no longer pin a device there.

## Tiers

| Tier | Meaning |
| --- | --- |
| `ONLINE` | Connected, with no active reason. |
| `ONLINE_WITH_WARNINGS` | Connected; every active reason is a warning. |
| `DEGRADED` | Connected, with at least one DEGRADED reason. |
| `OFFLINE` | No accepted heartbeat for 45 s. Only the sweep sets it and only a heartbeat clears it. |

`QUARANTINED_DUPLICATE_SERIAL` overrides every tier while its alert is open,
including while the device is offline and across reconnects. `ONBOARDING` is
kept until the first heartbeat. `FLAPPING` is no longer a lifecycle: an unstable
ZKT link is a warning on a live ESP, and the terminal link shows `FLAPPING`.

`last_error_code` is the best *gating* reason: quarantine first, then
DEGRADED before WARNING, then the priority below, then the oldest. The HIL
schedule (`CONNECTOR_ERROR_REQUIRES_REVIEW`) and the factory trial
(`FACTORY_TERMINAL_NOT_READY`) hold any device with a device error, so an
unresolved device fault keeps those holds even when it is only a warning. Data
codes and `ESP_OFFLINE` never gate.

## Terminal link

The terminal's own status never makes the ESP DEGRADED, except Hikvision
storage failures (`HIK_STORAGE`).

| Link | ZKT states | Effect |
| --- | --- | --- |
| `CONNECTED` | `ONLINE` | none |
| `STABILIZING` | `RECOVERING` | warning `TERMINAL_STABILIZING_STALLED` after 10 min |
| `MAINTENANCE` | `SESSION_REFRESH`, `RESTARTING` | counts as disconnected after 10 min |
| `STARTING`, `RECONNECTING` | `BOOTING`; `SUSPECT`, `CONNECTING`, `DISCOVERING`, `RETRY_WAIT`, `OFFLINE` | `DISCONNECTED` after 5 min |
| `DISCONNECTED` | any of the above for too long | warning `TERMINAL_DISCONNECTED`; `TERMINAL_LINK_DOWN` notification after 15 min |
| `FLAPPING` | `FLAPPING` | gating warning `ZKT_CONNECTION_FLAPPING` |
| `ERROR` | Hikvision poll errors 1, 3-7 | gating warning `HIK_*` |
| `UNKNOWN` | the ESP itself is not connected | none |

## Reasons

Every reason in the device drawer shows its message, tier, currency, since,
last seen, evidence and the condition that clears it. Currency is one of:

- **Current**: the latest evidence on this boot asserts the condition.
- **Held**: same boot; the latest evidence neither asserted nor cleared it.
  Missing diagnostics never clear a fault.
- **From an earlier boot**: the evidence belongs to a boot that has ended.
- **Latched**: heartbeats do not re-check it (OTA results, residual warnings).

| Code | Tier | Gating | Clears when |
| --- | --- | --- | --- |
| `QUARANTINED_DUPLICATE_SERIAL` | lifecycle override | yes | no other connector claims the serial |
| `ESP_FATAL` | DEGRADED | yes | the LED reports a non-fatal state |
| `ZKT_SERIAL_MISMATCH` | DEGRADED | yes | the terminal reports its assigned serial |
| `ESP_DURABILITY_FAULT` | DEGRADED; earlier-boot WARNING on firmware that cannot report storage (2.5.2 and older) | yes | capable firmware verifies storage |
| `ESP_PRESERVATION_UNVERIFIED` | WARNING | yes | capable firmware verifies storage |
| `ESP_RESTART_LOOP` | DEGRADED | yes | the ESP stays up 30 min |
| `ESP_DELIVERY_WORKER_FAULT` | DEGRADED; earlier-boot WARNING | yes | every required worker runs with a fresh tick |
| `HEARTBEAT_STALE` | DEGRADED | no | ADD accepts the next heartbeat |
| `ESP_LOCAL_FAILURE` | DEGRADED; WARNING when latched with verified storage, or held after a 2.5.2 boot-time failure | yes | the LED reports healthy, or at reboot |
| `HIK_CAPTURE_UNHEALTHY` | DEGRADED for storage; otherwise WARNING (network only once disconnected) | yes | the terminal is online and its checks succeed |
| `DEVICE_MESSAGE_REJECTED` | DEGRADED for a current heartbeat or custody rejection; otherwise WARNING | yes | ADD accepts the next message of each rejected type |
| `ADD_MESSAGE_PROCESSING_FAILED` | DEGRADED after 120 s of failed heartbeats; WARNING after 3 failures over 5 min | no | ADD processes the next message of each type |
| `HIK_LIGHT_RECONCILE_BLOCKED` | WARNING | yes | the history audit scans or completes |
| `OTA_DEVICE_ROLLED_BACK`, `OTA_DEVICE_REPORTED_FAILURE` | WARNING | yes | a later deployment to the device succeeds |
| `ZKT_CONNECTION_FLAPPING` | WARNING | yes | three consecutive successful connections |
| `TERMINAL_DISCONNECTED`, `TERMINAL_STABILIZING_STALLED` | WARNING | no | the link recovers |
| `DELIVERY_AUTHORITY_UNKNOWN` | WARNING | no | the journal reports ADD authority |

`ESP_OFFLINE`, `TERMINAL_LINK_DOWN`, clock drift, `USER_SNAPSHOT_TRUNCATED` and
the attendance, Oracle and source data codes have no health effect. The drawer
lists them under "Other open items". `ESP_OFFLINE` is raised once a device has
been silent for 2 minutes, counted from its disconnect, so an ordinary reboot
does not alert. It is not raised again for a device disconnected more than 12
minutes earlier.

### Owner decisions (10 October 2026)

- Signed 2.6.15 latches its LED `LOCAL_FAILURE` until reboot, and its storage
  diagnostics then report DEGRADED. A latch from the lock-contention sources
  `add_connector.c:3764` and `:3787` is a warning when the same sample shows
  zero I/O counters and verified storage and workers. The alert stays OPEN,
  HIGH and gating, so the device keeps its holds. Read-error
  (`zone_lite.c:7389`) and storage-init (`zone_lite.c:3196`) latches, and any
  write failure, stay DEGRADED. Settings: `ADD_DEVICE_HEALTH_LATCHED_LED_TIER`
  (`WARNING`) and `ADD_DEVICE_HEALTH_LATCHED_LED_SOURCES`.
- The derived model is enforced on deploy. There was no shadow-only period.

## Evidence ADD records

- Diagnostics alerts record `boot_id`, `firmware_version`, `binding`
  (`OBSERVED`, `INFERRED_CURRENT`, `INFERRED_PREVIOUS`), `boot_first_seen_at`
  and a bounded `evidence` record naming the failing worker or storage
  operation. Rows from before this release are bound at the next heartbeat
  from its uptime, without moving `last_seen_at`.
- The 2.6.24-2.6.27 storage recovery images start no delivery workers by
  design. Their worker contract is waived only for the registered HIL_ONLY
  release digest on a reviewed Peshawar connector.
- Under UNKNOWN journal authority, stale worker ticks prove nothing, and
  explicit failures count only after startup.
- A journal owner that is still starting is neutral for up to 10 minutes.
  Hard storage evidence still raises at once.
- A heartbeat sent while the connector's state lock was busy carries a
  zeroed terminal snapshot and is ignored for terminal state. LED
  `STATE_LOCK_BUSY`, `UNAVAILABLE` and empty states are not evidence.
- Rejected messages are tracked per type, with field paths but never values.
  Failures inside ADD are `ADD_MESSAGE_PROCESSING_FAILED`, not the device's
  fault. Rejected envelopes never refresh `last_seen_at`.
- Every heartbeat's telemetry row stores `payload._add_health` with the mode,
  lifecycle, derived lifecycle, tier, reasons (`CODE:TIER:CURRENCY`) and
  terminal link.

Resolutions that follow positive evidence never move `last_seen_at`, so an old
HIGH or CRITICAL alert is never pulled into a later HIL evidence window. Those
are: verified storage, a matching serial, no duplicate claimant, a complete
snapshot, a later successful deployment, an accepted message of the same type,
and the end of the boot for a worker fault.

## Alerts and acknowledgement

Acknowledging an alert annotates it. The alert stays OPEN, keeps driving tier
and holds, and later evidence refreshes or resolves the same row. The alerts
page has queues: Needs action (default), Acknowledged, Resolved and All.

## Operator actions

Both actions need the admin password (step-up), a reason of at least 10
characters and an idempotency key. Both are audited with before and after.

- **Resolve with reason** (`POST /api/v1/alerts/{id}/resolve`). It is refused
  while the latest evidence still asserts the condition, while a diagnostics
  fault is held on the same boot, and for workflow-owned or unknown codes.
  Resolving `ESP_DURABILITY_FAULT` leaves `ESP_PRESERVATION_UNVERIFIED`, a
  gating warning that the factory trial and the HIL post-verdict hold also
  refuse, until capable firmware verifies storage. That warning cannot itself
  be resolved by hand.
- **Re-evaluate device error** (`POST /api/v1/devices/{id}/clear-error`). It
  sets the stored device error to the derived value. It is refused while an
  active alert still backs the code, or if the code changed since you looked.

## Stranded-alert cleanup

In ADD, open **Alerts → Stranded alert review**.

1. Choose **Peshawar 02 + 06** first, then **Preview**. The preview is
   read-only and signed for 15 minutes. Rows are grouped by device:
   - **Stranded diagnostics**: a fault from a boot that has ended. The raising
     firmware and boot are shown, inferred from telemetry for older rows.
     Worker rows are preselected. Durability rows are not, because resolving
     one leaves `ESP_PRESERVATION_UNVERIFIED`.
   - **Condition cleared**: fresh evidence disproves the condition. It needs a
     connected device; an offline device's rows are skipped.
   - **Superseded acknowledgement**: a legacy acknowledged row with a newer
     open row.
   - **Stale device error** (per device): the stored error no active alert
     backs.
   - **Needs review**: listed, never preselected. For example, an OTA failure
     with no later success.

   Devices under HIL observation are blocked. Current conditions, same-boot
   held diagnostics and data codes never appear.
2. Review each row, and the before and after lifecycle, device error and hold
   effect of each device. Keep a P02 durability row unless the recovery
   receipts prove preservation.
3. Enter a reason, the typed confirmation `RESOLVE n ALERTS ON m DEVICES` and
   the password, then **Apply**. Apply locks the devices and their alerts,
   recomputes the plan, and refuses with `SCOPE_CHANGED` if anything moved.
4. Verify:
   - the next heartbeat shows no stranded reason;
   - the device error is empty, or the deliberate residual;
   - the audit rows exist (`ALERT_RESOLVED_BY_CLEANUP`,
     `DEVICE_HEALTH_REDERIVED_BY_CLEANUP`, `DEVICE_HEALTH_CLEANUP_APPLIED`);
   - the HIL schedule no longer reports `CONNECTOR_ERROR_REQUIRES_REVIEW`.
5. Repeat for the other devices, in batches of 200 alerts or fewer.
   SLICTOWER-13FL's durability row is resolved only by explicit owner
   decision.

Nothing is deleted. A condition that is still real is raised again from fresh
evidence as a new, traceable row.

## Shadow diff

`GET /api/v1/device-health/shadow` (admin, read-only) lists every active
device whose stored lifecycle or device error differs from the derived pair.
Each row shows whether the HIL and factory holds would lift, appear or change.
In enforced mode it should list only devices whose stored state predates their
last heartbeat, such as offline devices.

## Switches and rollback

| Setting (`.env.add`) | Default | Repository variable for deploy |
| --- | --- | --- |
| `ADD_DEVICE_HEALTH_DERIVED_ENABLED` | `true` | `ADD_DEVICE_HEALTH_DERIVED_ENABLED` |
| `ADD_DEVICE_HEALTH_CLEANUP_APPLY_ENABLED` | `true` | `ADD_DEVICE_HEALTH_CLEANUP_APPLY_ENABLED` |

1. **Kill switch.** Set `ADD_DEVICE_HEALTH_DERIVED_ENABLED=false` and redeploy
   or restart. The legacy writers own lifecycle again from the next heartbeat
   (15 s). Binding, evidence, the new resolutions, operator actions and the
   cleanup keep working. After any restart, the sweep waits 90 s before
   marking a device offline.
2. **Image rollback.** The previous image ignores the new detail keys, resets
   lifecycle on the next heartbeat, and treats acknowledged rows as open.
   Alerts with the new codes stay open with no lifecycle effect; acknowledge
   them or roll forward.

## Measuring time in each tier

```sql
SELECT c.display_name,
       t.payload -> '_add_health' ->> 'derived_lifecycle' AS lifecycle,
       count(*) * 15 / 60 AS approx_minutes
FROM add_device_telemetry t
JOIN add_connectors c ON c.id = t.connector_id
WHERE t.created_at >= now() - interval '7 days'
GROUP BY 1, 2
ORDER BY 1, 2;
```

Each heartbeat is about 15 s. `payload -> '_add_health' -> 'reasons'` names
the reason behind every DEGRADED or warning minute.
