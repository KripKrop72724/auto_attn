import { useCallback, useEffect, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { api } from '../api'
import {
  CommandProgress, Dialog, StatusBadge, dateTime, drawerTabs, idempotency,
  relativeTime, statusPattern, useToast, type DrawerTab,
} from '../App'
import { Icon } from '../Icon'
import type {
  Command, CommKeyReveal, CommKeyState, ConnectionEvent, Device, DeviceLog,
} from '../types'

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
  const [commKeyState, setCommKeyState] = useState<CommKeyState | null>(null)
  const [commKeyMode, setCommKeyMode] = useState<'ESP_ONLY' | 'ESP_AND_TERMINAL'>('ESP_ONLY')
  const [newCommKey, setNewCommKey] = useState('')
  const [commKeySerial, setCommKeySerial] = useState(seed.zkt?.confirmed_serial || seed.zkt?.expected_serial || seed.zkt?.serial || '')
  const [commKeyReason, setCommKeyReason] = useState('Recover remote connector communication access')
  const [commKeyConfirmation, setCommKeyConfirmation] = useState('')
  const [commKeyPassword, setCommKeyPassword] = useState('')
  const [replacementReason, setReplacementReason] = useState('Replace the connector binding with the authenticated physical terminal')
  const [replacementConfirmation, setReplacementConfirmation] = useState('')
  const [replacementPassword, setReplacementPassword] = useState('')
  const [revealReason, setRevealReason] = useState('Authorized operational recovery inspection')
  const [revealConfirmation, setRevealConfirmation] = useState('')
  const [revealPassword, setRevealPassword] = useState('')
  const [revealedKey, setRevealedKey] = useState<CommKeyReveal | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    const [detail, logResult, history, keyState] = await Promise.all([
      api<Device>(`/api/v1/devices/${seed.connector_id}`),
      api<{ rows: DeviceLog[] }>(`/api/v1/devices/${seed.connector_id}/logs?limit=250`),
      api<{ rows: ConnectionEvent[] }>(`/api/v1/devices/${seed.connector_id}/connectivity?limit=40`),
      api<CommKeyState>(`/api/v1/devices/${seed.connector_id}/comm-key`),
    ])
    setDevice(detail)
    setLogs(logResult.rows)
    setConnections(history.rows)
    setCommKeyState(keyState)
    setCommKeySerial((current) => current || detail.zkt?.confirmed_serial || detail.zkt?.expected_serial || detail.zkt?.serial || '')
  }, [seed.connector_id])
  useEffect(() => { void load() }, [load, revision])
  useEffect(() => {
    if (!revealedKey) return
    const hide = () => setRevealedKey(null)
    const hideWhenBackgrounded = () => { if (document.visibilityState !== 'visible') hide() }
    const timer = window.setTimeout(hide, 15_000)
    window.addEventListener('blur', hide)
    document.addEventListener('visibilitychange', hideWhenBackgrounded)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('blur', hide)
      document.removeEventListener('visibilitychange', hideWhenBackgrounded)
    }
  }, [revealedKey])
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
  const changeCommKey = async () => {
    if (!commKeyState) return
    if (!/^[1-9][0-9]{0,9}$/.test(newCommKey) || Number(newCommKey) > 4_294_967_295) {
      return toast.error('Enter a non-zero decimal COMM Key without leading zeros.')
    }
    if (!commKeyPassword) return toast.error('Confirm the administrator password for this key change.')
    setBusy(true)
    try {
      await api(`/api/v1/devices/${device.connector_id}/comm-key/changes`, {
        method: 'POST',
        body: JSON.stringify({
          new_key: newCommKey,
          mode: commKeyMode,
          expected_revision: commKeyState.applied_revision,
          expected_terminal_serial: commKeySerial,
          reason: commKeyReason,
          typed_confirmation: commKeyConfirmation,
          password: commKeyPassword,
          idempotency_key: idempotency('comm-key'),
        }),
      })
      toast.notice(commKeyState.capabilities.esp_only ? 'COMM Key recovery was securely queued.' : 'COMM Key recovery was staged for COMM Key-capable firmware.')
      setNewCommKey('')
      setCommKeyPassword('')
      setCommKeyConfirmation('')
      await load()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'COMM Key change could not be queued.')
    } finally {
      setBusy(false)
    }
  }
  const revealCommKey = async () => {
    if (!revealPassword) return toast.error('Confirm the administrator password before reveal.')
    setBusy(true)
    try {
      const revealed = await api<CommKeyReveal>(`/api/v1/devices/${device.connector_id}/comm-key/reveal`, {
        method: 'POST',
        body: JSON.stringify({ password: revealPassword, reason: revealReason, typed_confirmation: revealConfirmation }),
      })
      setRevealedKey(revealed)
      setRevealPassword('')
      setRevealConfirmation('')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'COMM Key could not be revealed.')
    } finally {
      setBusy(false)
    }
  }
  const cancelCommKeyOperation = async () => {
    if (!commKeyState?.active_operation) return
    try {
      await api(`/api/v1/comm-key-operations/${commKeyState.active_operation.operation_id}/cancel`, { method: 'POST', body: '{}' })
      toast.notice('COMM Key operation was cancelled before mutation.')
      await load()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'COMM Key operation could not be cancelled.')
    }
  }
  const replaceTerminalBinding = async () => {
    const currentSerial = device.zkt?.confirmed_serial || device.zkt?.expected_serial
    const observedSerial = device.zkt?.serial
    if (!currentSerial || !observedSerial || currentSerial === observedSerial) return
    if (!replacementPassword) return toast.error('Confirm the administrator password for this terminal replacement.')
    setBusy(true)
    try {
      await api(`/api/v1/devices/${device.connector_id}/terminal-binding/replace`, {
        method: 'POST',
        body: JSON.stringify({
          current_serial: currentSerial,
          observed_serial: observedSerial,
          reason: replacementReason,
          typed_confirmation: replacementConfirmation,
          password: replacementPassword,
          idempotency_key: idempotency('terminal-replacement'),
        }),
      })
      toast.notice('Terminal replacement was authorized and sent for device acknowledgement.')
      setReplacementPassword('')
      setReplacementConfirmation('')
      await load()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Terminal replacement could not be authorized.')
    } finally {
      setBusy(false)
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
  const currentBindingSerial = device.zkt?.confirmed_serial || device.zkt?.expected_serial || ''
  const observedTerminalSerial = device.zkt?.serial || ''
  const terminalReplacementNeeded = Boolean(
    currentBindingSerial && observedTerminalSerial && currentBindingSerial !== observedTerminalSerial,
  )
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
          {terminalReplacementNeeded && <article className="control-card pattern-blocked"><span><Icon name="shield" /></span><div>
            <h3>Replace terminal binding</h3>
            <p>The authenticated physical terminal reports serial <strong>{observedTerminalSerial}</strong>, while this connector remains bound to <strong>{currentBindingSerial}</strong>. User and attendance writes remain blocked until the ESP persists the replacement serial.</p>
            <label>Operational reason<input value={replacementReason} onChange={(event) => setReplacementReason(event.target.value)} /></label>
            <label>Type <strong>REPLACE {device.connector_id} {currentBindingSerial} {observedTerminalSerial}</strong><input value={replacementConfirmation} onChange={(event) => setReplacementConfirmation(event.target.value)} /></label>
            <label>Confirm administrator password<input type="password" autoComplete="current-password" value={replacementPassword} onChange={(event) => setReplacementPassword(event.target.value)} /></label>
          </div><button className="button destructive" disabled={busy} onClick={() => void replaceTerminalBinding()}>{busy ? 'Authorizing…' : 'Replace binding'}</button></article>}
          <article className="control-card comm-key-card"><span><Icon name="shield" /></span><div>
            <h3>COMM Key recovery</h3>
            <p>
              State <strong>{commKeyState?.management_state || 'LOADING'}</strong> · applied revision {commKeyState?.applied_revision ?? 0} · desired revision {commKeyState?.desired_revision ?? 0}
              {commKeyState?.last_verified_at ? ` · verified ${relativeTime(commKeyState.last_verified_at)}` : ''}.
              {commKeyState?.capabilities.recovery_staging ? ' This operation will remain staged until COMM Key-capable Zone Lite firmware connects.' : ''}
              {commKeyState && !commKeyState.enabled ? ' Management is currently disabled by the production feature gate.' : ''}
            </p>
            {commKeyState?.last_error_code && <p className="pattern-blocked">Last failure: {commKeyState.last_error_code.replaceAll('_', ' ')}</p>}
            {commKeyState?.active_operation ? <div className="comm-key-operation">
              <StatusBadge state={commKeyState.active_operation.status} />
              <span>Revision {commKeyState.active_operation.requested_revision} · {commKeyState.active_operation.mode} · expires {dateTime(commKeyState.active_operation.expires_at)}</span>
              <button className="button secondary" disabled={busy || ['RUNNING', 'ACKNOWLEDGED'].includes(commKeyState.active_operation.status)} onClick={() => void cancelCommKeyOperation()}>Cancel safely</button>
            </div> : <div className="comm-key-controls">
              <label>Recovery mode<select value={commKeyMode} onChange={(event) => setCommKeyMode(event.target.value as 'ESP_ONLY' | 'ESP_AND_TERMINAL')}>
                <option value="ESP_ONLY">ESP connector only</option>
                <option value="ESP_AND_TERMINAL" disabled={!commKeyState?.capabilities.esp_and_terminal}>ESP and certified ZKT terminal</option>
              </select></label>
              {!commKeyState?.capabilities.esp_and_terminal && <p>Remote terminal rotation unavailable: {(commKeyState?.capabilities.esp_and_terminal_block_reason || 'CAPABILITY_NOT_AVAILABLE').replaceAll('_', ' ').toLowerCase()}.</p>}
              <label>New COMM Key<input type="password" inputMode="numeric" autoComplete="new-password" value={newCommKey} onChange={(event) => setNewCommKey(event.target.value)} /></label>
              <label>Expected ZKT serial<input value={commKeySerial} onChange={(event) => setCommKeySerial(event.target.value.trim())} /></label>
              {!device.zkt?.serial && <p>The entered serial will be recorded as a provisional, read-only recovery expectation. Firmware must authenticate to the ZKT and prove this exact serial before applying the key.</p>}
              <label>Operational reason<input value={commKeyReason} onChange={(event) => setCommKeyReason(event.target.value)} /></label>
              <label>Type <strong>CHANGE {device.connector_id} {commKeySerial || '&lt;serial&gt;'}</strong><input value={commKeyConfirmation} onChange={(event) => setCommKeyConfirmation(event.target.value)} /></label>
              <label>Confirm administrator password<input type="password" autoComplete="current-password" value={commKeyPassword} onChange={(event) => setCommKeyPassword(event.target.value)} /></label>
              <button className="button destructive" disabled={busy || !commKeyState?.enabled} onClick={() => void changeCommKey()}>{commKeyState?.capabilities.recovery_staging ? 'Stage secure recovery' : 'Queue secure recovery'}</button>
            </div>}
            {commKeyState?.managed && commKeyState.reveal_enabled && <div className="comm-key-reveal">
              <h4>Break-glass reveal</h4>
              <p>Requires fresh authentication and is audited. The value hides after 15 seconds, on window blur, or when this drawer closes.</p>
              {revealedKey ? <output aria-live="assertive">COMM Key: <strong>{revealedKey.comm_key}</strong></output> : <>
                <label>Reveal reason<input value={revealReason} onChange={(event) => setRevealReason(event.target.value)} /></label>
                <label>Type <strong>REVEAL {device.connector_id}</strong><input value={revealConfirmation} onChange={(event) => setRevealConfirmation(event.target.value)} /></label>
                <label>Confirm administrator password<input type="password" autoComplete="current-password" value={revealPassword} onChange={(event) => setRevealPassword(event.target.value)} /></label>
                <button className="button secondary" disabled={busy} onClick={() => void revealCommKey()}>Reveal for 15 seconds</button>
              </>}
            </div>}
          </div></article>
          <article className="control-card pattern-blocked"><span><Icon name="power" /></span><div><h3>Restart ZKT terminal</h3><p>Issues an authenticated protocol restart. Active enrollment leases block this operation.</p><label>Reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label><label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label></div><button className="button destructive" disabled={busy} onClick={() => void restart()}>{busy ? 'Queuing…' : 'Restart terminal'}</button></article>
          {device.active_command && <CommandProgress command={device.active_command} onCancel={cancelCommand} />}
        </div>}
      </div>
    </Dialog>
  )
}
