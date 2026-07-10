import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, queryString, setCsrfToken } from './api'
import { Icon } from './Icon'
import type { Alert, AttendanceEvent, ConnectionEvent, Device, DeviceLog, DeviceUser, Overview } from './types'

type View = 'fleet' | 'attendance' | 'alerts'
type DeviceTab = 'summary' | 'users' | 'enrollment' | 'logs' | 'control'

const dateTime = (value?: string | null) => value
  ? new Intl.DateTimeFormat('en-PK', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'Asia/Karachi' }).format(new Date(value))
  : '—'

const relativeTime = (value?: string | null) => {
  if (!value) return 'Never seen'
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute')
  return formatter.format(Math.round(minutes / 60), 'hour')
}

const stateClass = (state: string) => state.toLowerCase().replaceAll('_', '-')
const idempotency = (prefix: string) => `${prefix}:${crypto.randomUUID()}`

function useToast() {
  const [toast, setToast] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 5000)
    return () => window.clearTimeout(timer)
  }, [toast])
  return { toast, success: (text: string) => setToast({ kind: 'success', text }), error: (text: string) => setToast({ kind: 'error', text }) }
}

function Login({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState('StateHealthAdmin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true); setError('')
    try { await onLogin(username, password) } catch (err) { setError(err instanceof Error ? err.message : 'Sign in failed') } finally { setBusy(false) }
  }
  return <main className="login-shell">
    <section className="login-story">
      <div className="brand-mark"><span>SL</span><i /></div>
      <div>
        <p className="eyebrow">STATE LIFE · ATTENDANCE OPERATIONS</p>
        <h1>Every device.<br /><em>One command surface.</em></h1>
        <p className="login-copy">Securely observe and operate ESP32–ZKT attendance pairs across Pakistan, with live state, reliable capture, and controlled enrollment.</p>
      </div>
      <div className="signal-art" aria-hidden="true"><i/><i/><i/><i/><span /></div>
      <p className="story-foot">Encrypted control channel · Durable command ledger · Full operator audit trail</p>
    </section>
    <section className="login-panel">
      <form className="login-card" onSubmit={submit}>
        <div className="mobile-brand"><div className="brand-mark small"><span>SL</span><i /></div><b>Attendance Device Dashboard</b></div>
        <p className="eyebrow">CONTROL ROOM ACCESS</p>
        <h2>Welcome back</h2>
        <p>Sign in to the national device operations console.</p>
        <label>Username<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} autoFocus /></label>
        {error && <div className="form-error"><Icon name="alert" />{error}</div>}
        <button className="primary wide" disabled={busy || !username || !password}>{busy ? 'Authenticating…' : 'Enter command center'}<Icon name="chevron" /></button>
        <small>Authorized State Life personnel only. All actions are recorded.</small>
      </form>
    </section>
  </main>
}

function StatusPill({ state, pulse = false }: { state: string; pulse?: boolean }) {
  return <span className={`status-pill ${stateClass(state)}`}>{pulse && <i />}{state.replaceAll('_', ' ')}</span>
}

function Kpi({ label, value, detail, tone, icon }: { label: string; value: number | string; detail: string; tone: string; icon: Parameters<typeof Icon>[0]['name'] }) {
  return <article className={`kpi ${tone}`}><div className="kpi-icon"><Icon name={icon} /></div><div><p>{label}</p><strong>{value}</strong><span>{detail}</span></div></article>
}

function FleetView({ devices, overview, loading, onSelect, onAdd }: { devices: Device[]; overview: Overview; loading: boolean; onSelect: (device: Device) => void; onAdd: () => void }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('ALL')
  const shown = devices.filter(device => (filter === 'ALL' || device.state === filter) && `${device.display_name} ${device.zone_name} ${device.hardware_id}`.toLowerCase().includes(query.toLowerCase()))
  const online = overview.online || 0
  const pct = overview.total ? Math.round(online / overview.total * 100) : 0
  return <>
    <header className="page-header"><div><p className="eyebrow">NATIONAL FLEET</p><h1>Device command center</h1><p>Live operational state of every ESP32 and its assigned ZKT terminal.</p></div><button className="primary" onClick={onAdd}><Icon name="plus" />Register connector</button></header>
    <section className="kpi-grid">
      <Kpi label="Fleet availability" value={`${pct}%`} detail={`${online} of ${overview.total} connectors online`} tone="green" icon="pulse" />
      <Kpi label="Needs attention" value={(overview.degraded || 0) + (overview.offline || 0) + ((overview as Overview & { flapping?: number }).flapping || 0)} detail={`${overview.open_alerts} open alerts`} tone="amber" icon="alert" />
      <Kpi label="Active enrollment" value={overview.active_leases} detail="10-minute administrator leases" tone="blue" icon="shield" />
      <Kpi label="Total footprint" value={overview.total} detail="ESP32–ZKT pairs registered" tone="neutral" icon="server" />
    </section>
    <section className="panel fleet-panel">
      <div className="panel-head"><div><h2>Fleet status</h2><p>Connector and terminal state with anti-flap hysteresis.</p></div><div className="health-legend"><span><i className="online" />Online {overview.online || 0}</span><span><i className="degraded" />Degraded {(overview.degraded || 0) + ((overview as Overview & { flapping?: number }).flapping || 0)}</span><span><i className="offline" />Offline {overview.offline || 0}</span></div></div>
      <div className="fleet-bar"><i style={{ width: `${overview.total ? (overview.online || 0) / overview.total * 100 : 0}%` }} /><b style={{ width: `${overview.total ? ((overview.degraded || 0) + ((overview as Overview & { flapping?: number }).flapping || 0)) / overview.total * 100 : 0}%` }} /><span /></div>
      <div className="table-tools"><div className="search"><Icon name="search" /><input placeholder="Search zone, connector or hardware ID" value={query} onChange={event => setQuery(event.target.value)} /></div><select value={filter} onChange={event => setFilter(event.target.value)}><option value="ALL">All states</option><option>ONLINE</option><option>FLAPPING</option><option>DEGRADED</option><option>OFFLINE</option><option>ONBOARDING</option></select></div>
      <div className="table-wrap"><table><thead><tr><th>Device</th><th>Connector</th><th>ZKT terminal</th><th>Activity</th><th>Last contact</th><th>Status</th><th /></tr></thead><tbody>
        {loading && <tr><td colSpan={7}><div className="empty">Loading live fleet…</div></td></tr>}
        {!loading && shown.map(device => <tr key={device.connector_id} onClick={() => onSelect(device)} className="clickable"><td><div className="device-name"><span className={`device-dot ${stateClass(device.state)}`}><Icon name="server" /></span><div><b>{device.display_name}</b><small>{device.zone_id}</small></div></div></td><td><code>{device.hardware_id}</code><small>FW {device.firmware_version || 'unknown'}</small></td><td>{device.zkt ? <><b>{device.zkt.model || 'ZKT device'}</b><small>{device.zkt.ip_address || 'Awaiting IP'} · {device.zkt.serial || 'No serial'}</small></> : <span className="muted">Not observed</span>}</td><td>{device.current_activity || 'Idle'}</td><td title={dateTime(device.last_seen_at)}>{relativeTime(device.last_seen_at)}</td><td><StatusPill state={device.state} pulse={device.connected} /></td><td><Icon className="row-chevron" name="chevron" /></td></tr>)}
        {!loading && !shown.length && <tr><td colSpan={7}><div className="empty"><Icon name="server" /><b>No devices match this view</b><span>Register a connector or change the filters.</span></div></td></tr>}
      </tbody></table></div>
    </section>
  </>
}

function AttendanceView({ devices, liveRevision }: { devices: Device[]; liveRevision: number }) {
  const [rows, setRows] = useState<AttendanceEvent[]>([])
  const [filters, setFilters] = useState({ device_id: '', q: '', cnic: '', punch: '', clock_quality: '', from_time: '', to_time: '' })
  const [loading, setLoading] = useState(false)
  const load = useCallback(async () => { setLoading(true); try { const result = await api<{ rows: AttendanceEvent[] }>('/api/v1/attendance' + queryString(filters)); setRows(result.rows) } finally { setLoading(false) } }, [filters])
  useEffect(() => { void load() }, [load, liveRevision])
  return <><header className="page-header"><div><p className="eyebrow">CAPTURE LEDGER</p><h1>Attendance events</h1><p>Filter live and reconciled punches across every certified terminal.</p></div><button className="secondary" onClick={() => void load()}><Icon name="refresh" />Refresh</button></header>
    <section className="panel"><div className="filter-grid"><label>Device<select value={filters.device_id} onChange={event => setFilters({ ...filters, device_id: event.target.value })}><option value="">All devices</option>{devices.map(device => <option value={device.connector_id} key={device.connector_id}>{device.display_name}</option>)}</select></label><label>Name / user ID<input value={filters.q} onChange={event => setFilters({ ...filters, q: event.target.value })} placeholder="Search employee" /></label><label>CNIC<input inputMode="numeric" maxLength={13} value={filters.cnic} onChange={event => setFilters({ ...filters, cnic: event.target.value.replace(/\D/g, '') })} placeholder="13 digits" /></label><label>Punch<select value={filters.punch} onChange={event => setFilters({ ...filters, punch: event.target.value })}><option value="">All punches</option><option value="0">Check in</option><option value="1">Check out</option></select></label><label>Clock quality<select value={filters.clock_quality} onChange={event => setFilters({ ...filters, clock_quality: event.target.value })}><option value="">Any quality</option><option>OK</option><option>DRIFTED</option><option>UNKNOWN</option></select></label><label>From<input type="datetime-local" value={filters.from_time} onChange={event => setFilters({ ...filters, from_time: event.target.value })} /></label><label>To<input type="datetime-local" value={filters.to_time} onChange={event => setFilters({ ...filters, to_time: event.target.value })} /></label></div>
      <div className="table-wrap"><table><thead><tr><th>Employee</th><th>Event time</th><th>Device</th><th>Capture</th><th>Clock</th><th>Oracle delivery</th></tr></thead><tbody>{loading && <tr><td colSpan={6}><div className="empty">Loading capture ledger…</div></td></tr>}{!loading && rows.map(row => <tr key={row.event_uid}><td><b>{row.display_name || 'Unknown employee'}</b><small>{row.cnic_masked || `User ${row.user_id}`}</small></td><td><b>{dateTime(row.device_event_time)}</b><small>Received {relativeTime(row.received_at)}</small></td><td>{row.device_serial || 'Unreported serial'}</td><td><StatusPill state={row.source} /><small>Punch {row.punch ?? '—'}</small></td><td><StatusPill state={row.clock_quality} /><small>{row.clock_drift_seconds == null ? 'No drift sample' : `${Math.round(row.clock_drift_seconds)}s drift`}</small></td><td><StatusPill state={row.ords_status} /></td></tr>)}{!loading && !rows.length && <tr><td colSpan={6}><div className="empty"><Icon name="clock" /><b>No attendance matches these filters</b></div></td></tr>}</tbody></table></div>
    </section></>
}

function AlertsView({ devices, toast }: { devices: Device[]; toast: ReturnType<typeof useToast> }) {
  const [rows, setRows] = useState<(Alert & { device: Device })[]>([])
  const load = useCallback(async () => {
    const results = await Promise.all(devices.map(async device => ({ device, alerts: (await api<{ rows: Alert[] }>(`/api/v1/devices/${device.connector_id}/alerts`)).rows })))
    setRows(results.flatMap(result => result.alerts.map(alert => ({ ...alert, device: result.device }))).sort((a, b) => +new Date(b.last_seen_at) - +new Date(a.last_seen_at)))
  }, [devices])
  useEffect(() => { void load() }, [load])
  const acknowledge = async (row: Alert) => { try { await api(`/api/v1/alerts/${row.id}/acknowledge`, { method: 'POST', body: '{}' }); toast.success('Alert acknowledged'); await load() } catch (err) { toast.error(err instanceof Error ? err.message : 'Unable to acknowledge alert') } }
  return <><header className="page-header"><div><p className="eyebrow">OPERATIONS QUEUE</p><h1>Alerts & exceptions</h1><p>Prioritized device conditions requiring operator review.</p></div><button className="secondary" onClick={() => void load()}><Icon name="refresh" />Refresh</button></header><section className="alert-list">{rows.map(row => <article className={`alert-card ${row.severity.toLowerCase()}`} key={`${row.device.connector_id}-${row.id}`}><div className="alert-symbol"><Icon name="alert" /></div><div className="alert-body"><div><StatusPill state={row.severity} /><span>{row.device.display_name} · {row.device.zone_id}</span></div><h3>{row.message}</h3><p>{row.code} · First observed {dateTime(row.first_seen_at)} · Last observed {relativeTime(row.last_seen_at)}</p></div><div>{row.state === 'OPEN' ? <button className="secondary small" onClick={() => void acknowledge(row)}><Icon name="check" />Acknowledge</button> : <StatusPill state={row.state} />}</div></article>)}{!rows.length && <div className="panel empty-state"><Icon name="shield" /><h2>No device alerts</h2><p>The fleet has no recorded exceptions.</p></div>}</section></>
}

function DeviceDrawer({ seed, liveRevision, onClose, toast, onRefresh }: { seed: Device; liveRevision: number; onClose: () => void; toast: ReturnType<typeof useToast>; onRefresh: () => Promise<void> }) {
  const [device, setDevice] = useState(seed)
  const [tab, setTab] = useState<DeviceTab>('summary')
  const [users, setUsers] = useState<DeviceUser[]>([])
  const [logs, setLogs] = useState<DeviceLog[]>([])
  const [connections, setConnections] = useState<ConnectionEvent[]>([])
  const [userQuery, setUserQuery] = useState('')
  const [selectedUser, setSelectedUser] = useState<DeviceUser | null>(null)
  const [busy, setBusy] = useState(false)
  const [password, setPassword] = useState('')
  const [restartReason, setRestartReason] = useState('Operator-requested health refresh')
  const [editName, setEditName] = useState('')
  const [editPrivilege, setEditPrivilege] = useState(0)
  const logsEnd = useRef<HTMLDivElement>(null)
  const seenLiveRevision = useRef(liveRevision)
  const loadDevice = useCallback(async () => {
    const [nextDevice, history] = await Promise.all([
      api<Device>(`/api/v1/devices/${seed.connector_id}`),
      api<{ rows: ConnectionEvent[] }>(`/api/v1/devices/${seed.connector_id}/connectivity?limit=8`),
    ])
    setDevice(nextDevice); setConnections(history.rows)
  }, [seed.connector_id])
  const loadUsers = useCallback(async () => {
    const result = await api<{ rows: DeviceUser[] }>('/api/v1/users' + queryString({ device_id: seed.connector_id, q: userQuery, limit: 500 }))
    setUsers(result.rows)
  }, [seed.connector_id, userQuery])
  const loadLogs = useCallback(async () => {
    const result = await api<{ rows: DeviceLog[] }>(`/api/v1/devices/${seed.connector_id}/logs?limit=500`)
    setLogs(result.rows.reverse())
  }, [seed.connector_id])
  useEffect(() => { void loadDevice() }, [loadDevice])
  useEffect(() => { if (tab === 'users' || tab === 'enrollment') void loadUsers() }, [tab, loadUsers])
  useEffect(() => {
    if (tab !== 'logs') return
    void loadLogs()
    const timer = window.setInterval(() => void loadLogs(), 5000)
    return () => window.clearInterval(timer)
  }, [tab, loadLogs])
  useEffect(() => {
    if (seenLiveRevision.current === liveRevision) return
    seenLiveRevision.current = liveRevision
    void loadDevice()
    if (tab === 'users' || tab === 'enrollment') void loadUsers()
    if (tab === 'logs') void loadLogs()
  }, [liveRevision, loadDevice, loadLogs, loadUsers, tab])
  useEffect(() => { logsEnd.current?.scrollIntoView({ behavior: 'smooth' }) }, [logs])
  const run = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true)
    try { await action(); toast.success(success); await loadDevice(); await onRefresh() } catch (err) { toast.error(err instanceof Error ? err.message : 'Command failed') } finally { setBusy(false) }
  }
  const refreshUsers = () => run(() => api(`/api/v1/devices/${device.connector_id}/users/refresh`, { method: 'POST', body: '{}' }), 'User refresh queued')
  const openEdit = (user: DeviceUser) => { setSelectedUser(user); setEditName(user.display_name); setEditPrivilege(user.privilege) }
  const saveUser = async () => {
    if (!selectedUser) return
    await run(() => api(`/api/v1/devices/${device.connector_id}/users/${encodeURIComponent(selectedUser.uid)}`, { method: 'PATCH', body: JSON.stringify({ display_name: editName, privilege: editPrivilege, expected_version: selectedUser.row_version, idempotency_key: idempotency('user') }) }), 'User update queued')
    setSelectedUser(null); await loadUsers()
  }
  const grantLease = async (user: DeviceUser) => {
    if (!password) { toast.error('Enter your dashboard password to authorize enrollment'); return }
    await run(() => api(`/api/v1/devices/${device.connector_id}/admin-leases`, { method: 'POST', body: JSON.stringify({ uid: user.uid, password, idempotency_key: idempotency('lease') }) }), `${user.display_name} is being elevated for 10 minutes`)
    setPassword('')
  }
  const revokeLease = () => device.active_lease && run(() => api(`/api/v1/admin-leases/${device.active_lease!.lease_id}/revoke`, { method: 'POST', body: '{}' }), 'Immediate privilege revocation queued')
  const restart = async () => {
    if (!password) { toast.error('Enter your dashboard password to authorize restart'); return }
    await run(() => api(`/api/v1/devices/${device.connector_id}/restart`, { method: 'POST', body: JSON.stringify({ reason: restartReason, password, idempotency_key: idempotency('restart') }) }), 'Safe ZKT restart queued')
    setPassword('')
  }
  const certify = () => run(() => api(`/api/v1/devices/${device.connector_id}/certify`, { method: 'POST', body: JSON.stringify({ read_users: true, read_attendance: true, user_write: true, admin_lease: true, protocol_restart: true, telnet_recovery: false, name_bytes: 24 }) }), 'Device capability profile certified')

  return <div className="drawer-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}><aside className="device-drawer">
    <header className="drawer-head"><div className={`device-dot large ${stateClass(device.state)}`}><Icon name="server" /></div><div><div className="drawer-title"><h2>{device.display_name}</h2><StatusPill state={device.state} pulse={device.connected} /></div><p>{device.zone_id} · Connector {device.hardware_id}</p></div><button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="x" /></button></header>
    <nav className="tabs">{(['summary', 'users', 'enrollment', 'logs', 'control'] as DeviceTab[]).map(value => <button className={tab === value ? 'active' : ''} onClick={() => setTab(value)} key={value}>{value}</button>)}</nav>
    <div className="drawer-content">
      {tab === 'summary' && <div className="summary-grid">
        <section className="detail-card wide"><div className="detail-head"><div><p className="eyebrow">LIVE PAIR HEALTH</p><h3>Connector → terminal</h3></div><button className="icon-button" onClick={() => void loadDevice()}><Icon name="refresh" /></button></div><div className="link-health"><div className={device.connected ? 'good' : 'bad'}><Icon name="wifi" /><b>ESP32 connector</b><span>{device.connected ? 'Live control channel' : 'Disconnected'}</span></div><i /><div className={device.zkt?.online ? 'good' : 'bad'}><Icon name="server" /><b>ZKT terminal</b><span>{device.zkt?.connection_state || 'UNOBSERVED'}{device.zkt?.backoff_until ? ` · retry ${relativeTime(device.zkt.backoff_until)}` : ''}</span></div></div>{device.last_error_code && <div className="inline-alert"><Icon name="alert" /><div><b>{device.last_error_code}</b><span>{device.current_activity || 'Device requires attention'}</span></div></div>}</section>
        <section className="detail-card"><p>Terminal time</p><strong>{device.zkt?.device_time ? new Intl.DateTimeFormat('en-PK', { timeStyle: 'medium', timeZone: 'Asia/Karachi' }).format(new Date(device.zkt.device_time)) : '—'}</strong><span>Sampled {relativeTime(device.zkt?.device_time_sampled_at)}</span></section>
        <section className="detail-card"><p>Clock drift</p><strong>{device.zkt?.drift_seconds == null ? '—' : `${Math.round(device.zkt.drift_seconds)}s`}</strong><span>{Math.abs(device.zkt?.drift_seconds || 0) < 120 ? 'Within capture guardrail' : 'Requires correction'}</span></section>
        <section className="detail-card"><p>Registered users</p><strong>{device.zkt?.user_count ?? '—'}</strong><span>{device.zkt?.attendance_count ?? '—'} records on terminal</span></section>
        <section className="detail-card"><p>Connectivity stability</p><strong>{device.zkt?.flap_count_15m ?? 0}</strong><span>transitions in the last 15 minutes · {device.zkt?.probe_latency_ms ?? '—'} ms probe</span></section>
        <section className="detail-card"><p>Next scheduled restart</p><strong className="small-strong">{dateTime(device.zkt?.next_restart_at)}</strong><span>02:00 · 12:00 · 22:00 PKT</span></section>
        <section className="detail-card wide"><p className="eyebrow">IDENTITY & CAPABILITIES</p><dl><div><dt>ZKT serial</dt><dd>{device.zkt?.serial || 'Awaiting authenticated discovery'}</dd></div><div><dt>IP address</dt><dd>{device.zkt?.ip_address || 'Discovery pending'}</dd></div><div><dt>Model / platform</dt><dd>{device.zkt?.model || 'Unknown'} / {device.zkt?.platform || 'Unknown'}</dd></div><div><dt>Certification</dt><dd><StatusPill state={device.zkt?.certification_state || 'UNOBSERVED'} /></dd></div><div><dt>User record</dt><dd>{String(device.zkt?.capabilities?.observed_user_record_bytes || 'Unknown')} bytes</dd></div><div><dt>Firmware</dt><dd>{device.firmware_version || 'Unreported'}</dd></div><div><dt>Last reconcile</dt><dd>{dateTime(device.zkt?.last_reconcile_at)}</dd></div></dl></section>
        <section className="detail-card wide"><p className="eyebrow">RECENT CONNECTIVITY TRANSITIONS</p><div className="transition-list">{connections.map(item => <div key={item.id}><time>{dateTime(item.observed_at)}</time><StatusPill state={item.from_state || 'START'} /><Icon name="chevron" /><StatusPill state={item.to_state} /><span>{item.reason || 'Connector state observation'}</span></div>)}{!connections.length && <p className="muted">No state transitions have been reported yet.</p>}</div></section>
      </div>}
      {tab === 'users' && <section><div className="section-head"><div><h3>Terminal users</h3><p>Names and permanent roles stored on this ZKT terminal.</p></div><button className="secondary" disabled={busy} onClick={() => void refreshUsers()}><Icon name="refresh" />Read from ZKT</button></div><div className="search full"><Icon name="search" /><input placeholder="Search name, UID or user ID" value={userQuery} onChange={event => setUserQuery(event.target.value)} /></div><div className="user-list">{users.map(user => <article key={user.uid}><div className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</div><div><b>{user.display_name}</b><span>{user.cnic_masked || 'CNIC unavailable'} · User {user.user_id}</span></div><StatusPill state={user.privilege === 14 ? 'ADMIN' : 'USER'} /><button className="icon-button" onClick={() => openEdit(user)} title="Edit user"><Icon name="edit" /></button></article>)}{!users.length && <div className="empty"><Icon name="users" /><b>No users have been synchronized</b><span>Run “Read from ZKT” after the terminal is online.</span></div>}</div></section>}
      {tab === 'enrollment' && <section><div className="section-head"><div><h3>Temporary enrollment administrator</h3><p>Elevate one existing regular user for exactly 10 minutes.</p></div></div>{device.active_lease && <div className={`lease-banner ${stateClass(device.active_lease.state)}`}><Icon name="shield" /><div><b>Lease {device.active_lease.state.toLowerCase()}</b><span>{device.active_lease.expires_at ? `Automatic revoke ${relativeTime(device.active_lease.expires_at)}` : 'Waiting for terminal verification'}</span></div><button className="danger small" disabled={busy} onClick={() => void revokeLease()}>Revoke now</button></div>}<label className="confirm-password">Dashboard password<input type="password" value={password} onChange={event => setPassword(event.target.value)} placeholder="Required for each elevation" /></label><div className="user-list enrollment">{users.filter(user => user.privilege === 0).map(user => <article key={user.uid}><div className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</div><div><b>{user.display_name}</b><span>{user.cnic_masked || `User ${user.user_id}`}</span></div><button className="primary small" disabled={busy || Boolean(device.active_lease)} onClick={() => void grantLease(user)}><Icon name="shield" />Elevate 10 min</button></article>)}{!users.length && <div className="empty"><Icon name="users" /><b>No eligible users available</b></div>}</div><div className="safety-note"><Icon name="alert" /><p><b>Fail-safe behavior</b><span>The ESP32 records the lease locally and revokes it even if the dashboard connection drops. If the terminal is powered off, revocation is enforced immediately when it returns and the fleet raises a critical overdue alert.</span></p></div></section>}
      {tab === 'logs' && <section className="log-section"><div className="section-head"><div><h3>Live connector log</h3><p>Redacted, device-originated diagnostics. Refreshes every 5 seconds.</p></div><button className="secondary" onClick={() => void loadLogs()}><Icon name="refresh" />Refresh</button></div><div className="terminal"><div className="terminal-top"><i /><i /><i /><span>{device.hardware_id} / serial monitor</span></div><div className="terminal-lines">{logs.map(row => <div key={row.id} className={row.level.toLowerCase()}><time>{new Date(row.received_at).toLocaleTimeString('en-PK', { hour12: false })}</time><b>{row.level.padEnd(8)}</b><em>{row.subsystem}</em><span>{row.message}</span></div>)}{!logs.length && <div className="terminal-empty">Waiting for connector logs…</div>}<div ref={logsEnd} /></div></div></section>}
      {tab === 'control' && <section><div className="section-head"><div><h3>Protected terminal controls</h3><p>High-impact commands require an explicit operator confirmation.</p></div></div><article className="control-card"><div className="control-icon"><Icon name="shield" /></div><div><h4>Capability certification</h4><p>Writes remain locked until the connector observes the certified 72-byte user record. Legacy 28-byte / 8-character-name terminals stay read-only because they cannot preserve CNIC identity.</p><StatusPill state={device.zkt?.certification_state || 'UNOBSERVED'} /></div><button className="secondary" disabled={busy || Number(device.zkt?.capabilities?.observed_user_record_bytes) !== 72} onClick={() => void certify()}>Certify profile</button></article><article className="control-card danger-zone"><div className="control-icon"><Icon name="power" /></div><div><h4>Restart ZKT terminal</h4><p>Uses the authenticated ZKT protocol, closes the session cleanly, waits 90 seconds, and verifies the same serial returns.</p><label>Reason<input value={restartReason} onChange={event => setRestartReason(event.target.value)} /></label><label>Dashboard password<input type="password" value={password} onChange={event => setPassword(event.target.value)} /></label></div><button className="danger" disabled={busy || Boolean(device.active_lease) || !device.zkt?.capabilities?.protocol_restart} onClick={() => void restart()}><Icon name="power" />Restart safely</button></article></section>}
    </div>
    {selectedUser && <div className="modal-layer"><form className="mini-modal" onSubmit={event => { event.preventDefault(); void saveUser() }}><div className="modal-head"><div><p className="eyebrow">EDIT TERMINAL USER</p><h3>{selectedUser.display_name}</h3></div><button type="button" className="icon-button" onClick={() => setSelectedUser(null)}><Icon name="x" /></button></div><label>Display name<input value={editName} onChange={event => setEditName(event.target.value)} /></label><label>Permanent role<select value={editPrivilege} onChange={event => setEditPrivilege(Number(event.target.value))}><option value={0}>Regular user</option><option value={14}>Administrator</option></select></label><p className="field-note">The CNIC suffix is preserved automatically and never exposed to the browser.</p><div className="modal-actions"><button type="button" className="secondary" onClick={() => setSelectedUser(null)}>Cancel</button><button className="primary" disabled={busy}>Queue update</button></div></form></div>}
  </aside></div>
}

function RegisterConnector({ onClose, onCreated, toast }: { onClose: () => void; onCreated: () => Promise<void>; toast: ReturnType<typeof useToast> }) {
  const [form, setForm] = useState({ display_name: '', hardware_id: '', zone_id: '', zone_name: '', device_id: '', expected_serial: '' })
  const [result, setResult] = useState<{ connector_id: string; activation_code: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true)
    try {
      const response = await api<{ connector: Device; activation_code: string }>('/api/v1/connectors', { method: 'POST', body: JSON.stringify({ ...form, expected_serial: form.expected_serial || null }) })
      setResult({ connector_id: response.connector.connector_id, activation_code: response.activation_code }); await onCreated(); toast.success('Connector registered')
    } catch (err) { toast.error(err instanceof Error ? err.message : 'Registration failed') } finally { setBusy(false) }
  }
  return <div className="modal-layer fixed"><form className="register-modal" onSubmit={submit}><div className="modal-head"><div><p className="eyebrow">SECURE ONBOARDING</p><h2>Register ESP32 connector</h2><p>Create a single-use activation bundle for a new device pair.</p></div><button type="button" className="icon-button" onClick={onClose}><Icon name="x" /></button></div>{result ? <div className="activation-result"><Icon name="check" /><h3>Registration ready</h3><p>Copy these values into the connector’s protected provisioning process. The activation code is shown once.</p><label>Connector ID<code>{result.connector_id}</code></label><label>One-time activation code<code>{result.activation_code}</code></label><button type="button" className="primary wide" onClick={onClose}>Done</button></div> : <><div className="form-grid"><label>Display name<input required value={form.display_name} onChange={event => setForm({ ...form, display_name: event.target.value })} placeholder="SLICTOWER · 3rd Floor" /></label><label>ESP hardware ID<input required value={form.hardware_id} onChange={event => setForm({ ...form, hardware_id: event.target.value })} placeholder="ESP32 MAC address" /></label><label>Zone ID<input required value={form.zone_id} onChange={event => setForm({ ...form, zone_id: event.target.value })} /></label><label>Zone name<input required value={form.zone_name} onChange={event => setForm({ ...form, zone_name: event.target.value })} /></label><label>Device ID<input required value={form.device_id} onChange={event => setForm({ ...form, device_id: event.target.value })} /></label><label>Expected ZKT serial<input value={form.expected_serial} onChange={event => setForm({ ...form, expected_serial: event.target.value })} placeholder="Recommended" /></label></div><div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{busy ? 'Registering…' : 'Register connector'}</button></div></>}</form></div>
}

export default function App() {
  const [session, setSession] = useState<{ username: string; csrf_token: string } | null | undefined>(undefined)
  const [view, setView] = useState<View>('fleet')
  const [devices, setDevices] = useState<Device[]>([])
  const [overview, setOverview] = useState<Overview>({ total: 0, open_alerts: 0, active_leases: 0 })
  const [loading, setLoading] = useState(true)
  const [liveRevision, setLiveRevision] = useState(0)
  const [selected, setSelected] = useState<Device | null>(null)
  const [registering, setRegistering] = useState(false)
  const toast = useToast()
  const loadFleet = useCallback(async () => {
    try {
      const [deviceResult, overviewResult] = await Promise.all([api<{ rows: Device[] }>('/api/v1/devices'), api<Overview>('/api/v1/overview')])
      setDevices(deviceResult.rows); setOverview(overviewResult)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setSession(null)
      else toast.error(err instanceof Error ? err.message : 'Unable to load fleet')
    } finally { setLoading(false) }
  }, []) // toast callbacks intentionally excluded; they are recreated with render state.
  useEffect(() => { api<{ username: string; csrf_token: string }>('/api/v1/auth/session').then(value => { setCsrfToken(value.csrf_token); setSession(value) }).catch(() => setSession(null)) }, [])
  useEffect(() => { if (session) void loadFleet() }, [session, loadFleet])
  useEffect(() => {
    if (!session) return
    const events = new EventSource('/events/v1/stream', { withCredentials: true })
    const refresh = () => { setLiveRevision(value => value + 1); void loadFleet() }
    for (const event of [
      'device', 'heartbeat', 'alert', 'backend_error',
      'command', 'command_update',
      'attendance', 'attendance_batch',
      'users', 'user_snapshot', 'log',
    ]) events.addEventListener(event, refresh)
    events.onerror = () => { /* Browser reconnects with Last-Event-ID automatically. */ }
    const fallback = window.setInterval(refresh, 30000)
    return () => { events.close(); window.clearInterval(fallback) }
  }, [session, loadFleet])
  const login = async (username: string, password: string) => { const value = await api<{ username: string; csrf_token: string }>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }); setCsrfToken(value.csrf_token); setSession(value) }
  const logout = async () => { try { await api('/api/v1/auth/logout', { method: 'POST', body: '{}' }) } finally { setCsrfToken(''); setSession(null) } }
  const nav = useMemo(() => [{ value: 'fleet' as const, label: 'Fleet', icon: 'grid' as const }, { value: 'attendance' as const, label: 'Attendance', icon: 'clock' as const }, { value: 'alerts' as const, label: 'Alerts', icon: 'alert' as const }], [])
  if (session === undefined) return <div className="boot"><div className="brand-mark"><span>SL</span><i /></div><p>Opening command center…</p></div>
  if (!session) return <Login onLogin={login} />
  return <div className="app-shell"><aside className="sidebar"><div className="sidebar-brand"><div className="brand-mark small"><span>SL</span><i /></div><div><b>Attendance</b><span>Device Dashboard</span></div></div><nav>{nav.map(item => <button key={item.value} className={view === item.value ? 'active' : ''} onClick={() => setView(item.value)}><Icon name={item.icon} />{item.label}{item.value === 'alerts' && overview.open_alerts > 0 && <i className="nav-count">{overview.open_alerts}</i>}</button>)}</nav><div className="sidebar-foot"><div className="system-state"><i /><div><b>ADD backend live</b><span>Secure control channel</span></div></div><button onClick={() => void logout()}><Icon name="logout" />Sign out</button></div></aside><main className="workspace"><div className="topbar"><div className="mobile-title"><div className="brand-mark tiny"><span>SL</span><i /></div><b>ADD Command Center</b></div><div className="freshness"><i /><span>Live fleet sync</span><time>{new Intl.DateTimeFormat('en-PK', { timeStyle: 'short', timeZone: 'Asia/Karachi' }).format(new Date())} PKT</time></div><div className="operator"><span>SA</span><div><b>{session.username}</b><small>National administrator</small></div></div></div><div className="page-content">{view === 'fleet' && <FleetView devices={devices} overview={overview} loading={loading} onSelect={setSelected} onAdd={() => setRegistering(true)} />}{view === 'attendance' && <AttendanceView devices={devices} liveRevision={liveRevision} />}{view === 'alerts' && <AlertsView devices={devices} toast={toast} />}</div></main>{selected && <DeviceDrawer seed={selected} liveRevision={liveRevision} onClose={() => setSelected(null)} toast={toast} onRefresh={loadFleet} />}{registering && <RegisterConnector onClose={() => setRegistering(false)} onCreated={loadFleet} toast={toast} />}{toast.toast && <div className={`toast ${toast.toast.kind}`}>{toast.toast.kind === 'success' ? <Icon name="check" /> : <Icon name="alert" />}{toast.toast.text}</div>}</div>
}
