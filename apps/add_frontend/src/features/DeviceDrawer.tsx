import { useCallback, useEffect, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { api } from '../api'
import {
  CommandProgress, Dialog, StatusBadge, dateTime, drawerTabs, idempotency,
  relativeTime, statusPattern, useToast, type DrawerTab,
} from '../App'
import { Icon } from '../Icon'
import type { Command, ConnectionEvent, Device, DeviceLog } from '../types'

export function DeviceDrawer({
  seed,
  revision,
  onClose,
  onManageUsers,
  toast,
}: {
  seed: Device
  revision: number
  onClose: () => void
  onManageUsers: (device: Device) => void
  toast: ReturnType<typeof useToast>
}) {
  const [device, setDevice] = useState(seed)
  const [tab, setTab] = useState<DrawerTab>('overview')
  const [logs, setLogs] = useState<DeviceLog[]>([])
  const [connections, setConnections] = useState<ConnectionEvent[]>([])
  const [password, setPassword] = useState('')
  const [reason, setReason] = useState('Operator-requested terminal health refresh')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    const [detail, logResult, history] = await Promise.all([
      api<Device>(`/api/v1/devices/${seed.connector_id}`),
      api<{ rows: DeviceLog[] }>(`/api/v1/devices/${seed.connector_id}/logs?limit=250`),
      api<{ rows: ConnectionEvent[] }>(`/api/v1/devices/${seed.connector_id}/connectivity?limit=40`),
    ])
    setDevice(detail)
    setLogs(logResult.rows)
    setConnections(history.rows)
  }, [seed.connector_id])
  useEffect(() => { void load() }, [load, revision])
  const restart = async () => {
    if (!password) return toast.error('Confirm the administrator password before restart.')
    setBusy(true)
    try {
      await api(`/api/v1/devices/${device.connector_id}/restart`, { method: 'POST', body: JSON.stringify({ reason, password, idempotency_key: idempotency('restart') }) })
      toast.notice('Authenticated ZKT restart is queued.')
      setPassword('')
      await load()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Restart could not be queued.')
    } finally {
      setBusy(false)
    }
  }
  const refreshUsers = async () => {
    try {
      await api(`/api/v1/devices/${device.connector_id}/users/refresh`, { method: 'POST', body: '{}' })
      toast.notice('A complete terminal user reread was requested.')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Refresh could not be queued.')
    }
  }
  const cancelCommand = async (command: Command) => {
    try {
      await api<Command>(`/api/v2/commands/${command.command_id}/cancel`, { method: 'POST', body: '{}' })
      toast.notice('Command cancellation was recorded before execution.')
      await load()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Command could not be cancelled.')
    }
  }
  const handleTabKey = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % drawerTabs.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + drawerTabs.length) % drawerTabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = drawerTabs.length - 1
    else return
    event.preventDefault()
    setTab(drawerTabs[next])
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]
      ?.focus()
  }
  return (
    <Dialog titleId="device-drawer-title" title={device.display_name} description={`${device.zone_id} · ${device.hardware_id}`} onClose={onClose} className="device-drawer">
      <div className="drawer-status"><StatusBadge state={device.state} live={device.connected} /><span>{device.current_activity || 'Idle'} · Last contact {relativeTime(device.last_seen_at)}</span></div>
      <div className="tabs" role="tablist" aria-label="Device details">
        {drawerTabs.map((item, index) => (
          <button
            id={`device-tab-${item}`}
            key={item}
            role="tab"
            aria-controls="device-tabpanel"
            aria-selected={tab === item}
            tabIndex={tab === item ? 0 : -1}
            className={tab === item ? 'active' : ''}
            onClick={() => setTab(item)}
            onKeyDown={(event) => handleTabKey(event, index)}
          >
            {item === 'logs' ? 'Live logs' : item}
          </button>
        ))}
      </div>
      <div
        id="device-tabpanel"
        className="drawer-content"
        role="tabpanel"
        aria-labelledby={`device-tab-${tab}`}
      >
        {tab === 'overview' && <div className="overview-grid">
          <article className="detail-card"><p className="eyebrow">ESP CONNECTOR</p><h3>{device.connected ? 'Connected to ADD' : 'Not currently connected'}</h3><dl><div><dt>Firmware</dt><dd>{device.firmware_version || 'Unknown'}</dd></div><div><dt>Wi-Fi MAC</dt><dd>{device.hardware_id}</dd></div><div><dt>Onboarding generation</dt><dd>{device.onboarding_generation}</dd></div><div><dt>Last onboarding</dt><dd>{dateTime(device.last_onboarded_at)}</dd></div></dl></article>
          <article className="detail-card"><p className="eyebrow">ZKT TERMINAL</p><h3>{device.zkt?.model || 'Awaiting terminal'}</h3><dl><div><dt>Serial</dt><dd>{device.zkt?.serial || '—'}</dd></div><div><dt>Address</dt><dd>{device.zkt?.ip_address || '—'}</dd></div><div><dt>Certification</dt><dd><StatusBadge state={device.zkt?.certification_state || 'UNKNOWN'} /></dd></div><div><dt>Snapshot</dt><dd>{device.zkt?.snapshot_complete ? 'Complete' : 'Incomplete'}</dd></div></dl></article>
          <article className="detail-card"><p className="eyebrow">LIVE TERMINAL CLOCK</p><h3>{device.zkt?.device_time ? dateTime(device.zkt.device_time) : 'No live sample'}</h3><p>Sampled {relativeTime(device.zkt?.device_time_sampled_at)} · Drift {device.zkt?.drift_seconds == null ? 'unknown' : `${Math.round(device.zkt.drift_seconds)} seconds`}</p></article>
          <article className="detail-card">
            <p className="eyebrow">CAPTURE HEALTH</p>
            <h3>{device.zkt?.attendance_count ?? '—'} terminal punches</h3>
            <p>{device.zkt?.user_count ?? '—'} users · Last 15-minute reconciliation {relativeTime(device.zkt?.last_reconcile_at)}</p>
            <dl>
              <div>
                <dt>Historical truth</dt>
                <dd><StatusBadge state={String(device.zkt?.capabilities.history_backfill_state || 'NOT_STARTED')} /></dd>
              </div>
              <div>
                <dt>Terminal coverage starts</dt>
                <dd>{String(device.zkt?.capabilities.history_coverage_start_month || 'Not discovered')}</dd>
              </div>
              <div>
                <dt>Current cursor</dt>
                <dd>{String(device.zkt?.capabilities.history_cursor_month || '—')}</dd>
              </div>
              <div>
                <dt>Blocked windows</dt>
                <dd>{Number(device.zkt?.capabilities.history_failed_windows || 0)}</dd>
              </div>
            </dl>
          </article>
          {device.last_error_code && <article className="detail-card wide pattern-blocked"><p className="eyebrow">ACTIVE PROBLEM</p><h3>{device.last_error_code.replaceAll('_', ' ')}</h3><p>{device.zkt?.writes_disabled_reason || 'Review live logs and connectivity history.'}</p></article>}
          <article className="detail-card wide"><div className="detail-title"><div><p className="eyebrow">INTERMITTENT CONNECTIVITY HISTORY</p><h3>Bounded reconnect and anti-flap state</h3></div><StatusBadge state={device.zkt?.connection_state || 'UNKNOWN'} /></div><div className="connection-list">{connections.slice(0, 12).map((row) => <div key={row.id}><time>{dateTime(row.observed_at)}</time><StatusBadge state={row.from_state || 'START'} /><Icon name="chevron" /><StatusBadge state={row.to_state} /><span>{row.reason || 'State observation'} · failures {row.consecutive_failures} · flaps {row.flap_count_15m}</span></div>)}{!connections.length && <p>No connectivity transitions recorded yet.</p>}</div></article>
        </div>}
        {tab === 'logs' && <section className="terminal-view" aria-label="Live ESP serial monitor"><header><span><i /><i /><i /></span><strong>{device.hardware_id} · live operations log</strong><button className="text-button" onClick={() => void load()}><Icon name="refresh" /> Refresh</button></header><div>{logs.map((row) => <p key={row.id} className={`log-pattern-${statusPattern(row.level)}`}><time>{dateTime(row.device_time || row.received_at)}</time><strong>{row.level}</strong><em>{row.subsystem}</em><span>{row.code ? `[${row.code}] ` : ''}{row.message}</span></p>)}{!logs.length && <div className="terminal-empty">Waiting for live Zone Lite logs…</div>}</div></section>}
        {tab === 'control' && <div className="control-stack">
          <article className="control-card"><span><Icon name="users" /></span><div><h3>Selected-terminal users</h3><p>Create, edit, delete, or grant a 10-minute enrollment lease. Every write requires current certification and a full snapshot.</p></div><button className="button primary" onClick={() => onManageUsers(device)}>Open Users workspace</button></article>
          <article className="control-card"><span><Icon name="refresh" /></span><div><h3>Refresh terminal users</h3><p>Request two matching terminal reads. Current verified revision: {device.zkt?.identity_snapshot_revision || 'none'} · {device.zkt?.identity_snapshot_stable ? 'stable' : 'awaiting verification'}{device.zkt?.identity_snapshot_observed_at ? ` · ${relativeTime(device.zkt.identity_snapshot_observed_at)}` : ''}.</p></div><button className="button secondary" onClick={() => void refreshUsers()}>Request verified reread</button></article>
          <article className="control-card pattern-blocked"><span><Icon name="power" /></span><div><h3>Restart ZKT terminal</h3><p>Issues an authenticated protocol restart. Active enrollment leases block this operation.</p><label>Reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label><label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label></div><button className="button destructive" disabled={busy} onClick={() => void restart()}>{busy ? 'Queuing…' : 'Restart terminal'}</button></article>
          {device.active_command && <CommandProgress command={device.active_command} onCancel={cancelCommand} />}
        </div>}
      </div>
    </Dialog>
  )
}
