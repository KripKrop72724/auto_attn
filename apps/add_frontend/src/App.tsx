import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { api, ApiError, queryString, setCsrfToken } from './api'
import { Icon } from './Icon'
import type {
  Alert,
  AttendanceEvent,
  Command,
  ConnectionEvent,
  Device,
  DeviceLog,
  DeviceUser,
  IdentityIntegrity,
  Overview,
  UserCommandResponse,
} from './types'

type View = 'fleet' | 'users' | 'attendance' | 'alerts'
type DrawerTab = 'overview' | 'logs' | 'control'
type ToastState = { kind: 'notice' | 'error'; text: string } | null
type UserDialogState =
  | { mode: 'create' }
  | { mode: 'edit'; user: DeviceUser }
  | { mode: 'delete'; user: DeviceUser }
  | { mode: 'lease'; user: DeviceUser }
  | null

const terminalCommandStates = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'])
const drawerTabs: DrawerTab[] = ['overview', 'logs', 'control']

export const dateTime = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat('en-PK', {
        dateStyle: 'medium',
        timeStyle: 'medium',
        timeZone: 'Asia/Karachi',
      }).format(new Date(value))
    : '—'

const relativeTime = (value?: string | null) => {
  if (!value) return 'Never seen'
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 48) return formatter.format(hours, 'hour')
  return formatter.format(Math.round(hours / 24), 'day')
}

const idempotency = (prefix: string) => {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
  return `${prefix}:${id}`
}

const utf8Length = (value: string) => new TextEncoder().encode(value).length

const utf8Prefix = (value: string, maxBytes: number) => {
  let output = ''
  for (const character of value) {
    if (utf8Length(output + character) > maxBytes) break
    output += character
  }
  return output
}

export function identityConflictText(user: DeviceUser) {
  const matches = user.identity_conflict_members
    .map((member) => `${member.user_id} (UID ${member.uid})`)
    .join(', ')
  return matches
    ? `${user.cnic_masked || 'Masked CNIC'} · Exact CNIC also encoded on user ${matches} · correction required`
    : `${user.cnic_masked || 'Masked CNIC'} · Exact duplicate reported by terminal · correction required`
}

export function buildMachinePreview(
  displayName: string,
  cnic: string,
  shiftWorker: boolean,
  byteLimit = 24,
) {
  const canonical = displayName.trim().replace(/\s+/g, ' ')
  const suffix = `-${shiftWorker ? 'S-' : ''}${cnic}`
  return `${utf8Prefix(canonical, Math.max(0, byteLimit - utf8Length(suffix)))}${suffix}`
}

export function validateUserDraft(values: {
  displayName: string
  cnic: string
  password: string
  userIdOverride?: string
}) {
  if (!values.displayName.trim()) return 'Full name is required.'
  if (!/^\d{13}$/.test(values.cnic)) return 'CNIC must contain exactly 13 digits.'
  if (values.userIdOverride && !/^\d+$/.test(values.userIdOverride)) {
    return 'The optional employee/user ID must be numeric.'
  }
  if (!values.password) return 'Password confirmation is required.'
  return null
}

export const confirmationMatches = (value: string, user: DeviceUser) =>
  value === user.display_name || value === user.user_id

const statusPattern = (state: string) => {
  const normalized = state.toUpperCase()
  if (
    ['ONLINE', 'SUCCEEDED', 'ACKED', 'CERTIFIED', 'ACTIVE', 'OK', 'RESOLVED'].includes(
      normalized,
    )
  )
    return 'confirmed'
  if (
    ['OFFLINE', 'FAILED', 'CRITICAL', 'EXPIRED', 'QUARANTINED', 'BLOCKED_IDENTITY'].some(
      (item) => normalized.includes(item),
    )
  )
    return 'blocked'
  if (
    ['WAITING', 'RETRYING', 'DEGRADED', 'FLAPPING', 'PENDING', 'RUNNING'].some((item) =>
      normalized.includes(item),
    )
  )
    return 'waiting'
  return 'notice'
}

export function StatusBadge({ state, live = false }: { state: string; live?: boolean }) {
  const pattern = statusPattern(state)
  const icon =
    pattern === 'confirmed'
      ? 'check'
      : pattern === 'blocked'
        ? 'alert'
        : pattern === 'waiting'
          ? 'pause'
          : 'info'
  return (
    <span className={`status-badge pattern-${pattern}`} data-pattern={pattern}>
      <Icon name={icon} />
      <span>{state.replaceAll('_', ' ')}</span>
      {live && <i aria-hidden="true" />}
    </span>
  )
}

function useToast() {
  const [toast, setToast] = useState<ToastState>(null)
  useEffect(() => {
    if (!toast) return
    const timeout = window.setTimeout(() => setToast(null), 5000)
    return () => window.clearTimeout(timeout)
  }, [toast])
  const notice = useCallback((text: string) => setToast({ kind: 'notice', text }), [])
  const error = useCallback((text: string) => setToast({ kind: 'error', text }), [])
  return useMemo(() => ({ toast, notice, error }), [error, notice, toast])
}

function Dialog({
  titleId,
  title,
  description,
  onClose,
  children,
  className = '',
}: {
  titleId: string
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  className?: string
}) {
  const panel = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const node = panel.current
    const focusable = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ) || [],
      )
    focusable()[0]?.focus()
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
      }
      if (event.key === 'Tab') {
        const items = focusable()
        if (!items.length) return
        const first = items[0]
        const last = items[items.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', handle)
    return () => {
      document.removeEventListener('keydown', handle)
      previous?.focus()
    }
  }, [])
  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        ref={panel}
        className={`dialog-panel ${className}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? `${titleId}-description` : undefined}
      >
        <header className="dialog-header">
          <div>
            <p className="eyebrow">STATE LIFE · SECURE OPERATION</p>
            <h2 id={titleId}>{title}</h2>
            {description && <p id={`${titleId}-description`}>{description}</p>}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close dialog">
            <Icon name="x" />
          </button>
        </header>
        {children}
      </div>
    </div>
  )
}

function Login({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState('StateHealthAdmin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await onLogin(username, password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign in failed.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <main className="login-shell">
      <section className="login-brand" aria-label="State Life Attendance Device Dashboard">
        <img src="/state-life-logo.png" alt="State Life Insurance Corporation" />
        <p className="eyebrow inverse">ATTENDANCE DEVICE OPERATIONS</p>
        <h1>One trusted view of every attendance terminal.</h1>
        <p>
          Command, control, and surveillance for authorized Zone Lite devices and their ZKT
          terminals across Pakistan.
        </p>
        <div className="brand-proof">
          <span><Icon name="shield" /> Signed device identity</span>
          <span><Icon name="terminal" /> Durable command ledger</span>
          <span><Icon name="pulse" /> Live attendance oversight</span>
        </div>
      </section>
      <section className="login-form-side">
        <form className="login-card" onSubmit={submit}>
          <div className="compact-brand">
            <img src="/state-life-logo.png" alt="" />
            <span>State Life Insurance Corporation</span>
          </div>
          <p className="eyebrow">AUTHORIZED ACCESS</p>
          <h2>Attendance Device Dashboard</h2>
          <p className="supporting">Sign in to the national device operations console.</p>
          <label>
            Username
            <input
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {error && (
            <div className="message pattern-blocked" role="alert">
              <Icon name="alert" /> {error}
            </div>
          )}
          <button className="button primary full" disabled={busy || !username || !password}>
            {busy ? 'Authenticating…' : 'Enter dashboard'} <Icon name="chevron" />
          </button>
          <small>Authorized State Life personnel only. Every operation is audited.</small>
        </form>
      </section>
    </main>
  )
}

function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  )
}

function Metric({ label, value, detail, icon }: { label: string; value: string | number; detail: string; icon: Parameters<typeof Icon>[0]['name'] }) {
  return (
    <article className="metric-card">
      <span className="metric-icon"><Icon name={icon} /></span>
      <div><p>{label}</p><strong>{value}</strong><small>{detail}</small></div>
    </article>
  )
}

function FleetView({
  devices,
  overview,
  loading,
  onInspect,
  onManageUsers,
}: {
  devices: Device[]
  overview: Overview
  loading: boolean
  onInspect: (device: Device) => void
  onManageUsers: (device: Device) => void
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('ALL')
  const shown = devices.filter(
    (device) =>
      (filter === 'ALL' || device.state === filter) &&
      `${device.display_name} ${device.zone_name} ${device.hardware_id} ${device.zkt?.serial || ''}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  )
  const online = overview.online || 0
  const availability = overview.total ? Math.round((online / overview.total) * 100) : 0
  const attention =
    (overview.offline || 0) +
    (overview.degraded || 0) +
    (overview.flapping || 0) +
    (overview.quarantined_duplicate_serial || 0)
  const delivery = overview.ords_delivery
  return (
    <>
      <PageHeader
        eyebrow="NATIONAL FLEET"
        title="Attendance device command center"
        description="Live operational state of every authorized Zone Lite ESP and its assigned ZKT terminal."
      />
      <section className="metric-grid" aria-label="Fleet key indicators">
        <Metric label="Fleet availability" value={`${availability}%`} detail={`${online} of ${overview.total} connectors online`} icon="pulse" />
        <Metric label="Needs review" value={overview.open_alerts} detail={`${attention} devices degraded or offline`} icon="alert" />
        <Metric label="Enrollment access" value={overview.active_leases} detail="Temporary 10-minute leases" icon="shield" />
        <Metric label="ORDS delivery queue" value={delivery?.backlog ?? 0} detail={`${delivery?.retrying ?? 0} retrying · ${delivery?.blocked_identity ?? 0} identity blocked · ${delivery?.quarantined ?? 0} quarantined`} icon="clock" />
        <Metric label="National footprint" value={overview.total} detail="Authorized ESP–ZKT pairs" icon="server" />
      </section>
      <section className="panel">
        <header className="panel-header">
          <div><h2>Live fleet</h2><p>State labels and border patterns remain readable without color.</p></div>
          <div className="auto-onboard-note"><Icon name="shield" /> Secure auto-onboarding enabled</div>
        </header>
        <div className="toolbar">
          <label className="search-field">
            <span className="sr-only">Search fleet</span>
            <Icon name="search" />
            <input
              placeholder="Search zone, MAC, serial, or terminal"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label>
            <span className="sr-only">Filter by state</span>
            <select value={filter} onChange={(event) => setFilter(event.target.value)}>
              <option value="ALL">All states</option>
              <option value="ONLINE">Online</option>
              <option value="DEGRADED">Degraded</option>
              <option value="FLAPPING">Flapping</option>
              <option value="OFFLINE">Offline</option>
              <option value="ONBOARDING">Onboarding</option>
              <option value="QUARANTINED_DUPLICATE_SERIAL">Quarantined</option>
            </select>
          </label>
        </div>
        <div className="device-list" aria-busy={loading}>
          {loading && <div className="empty-state"><Icon name="refresh" /><h3>Loading live fleet…</h3></div>}
          {!loading && shown.map((device) => (
            <article className={`device-card pattern-${statusPattern(device.state)}`} key={device.connector_id}>
              <button className="device-card-main" onClick={() => onInspect(device)} aria-label={`Inspect ${device.display_name}`}>
                <span className="device-symbol"><Icon name="server" /></span>
                <span className="device-identity"><strong>{device.display_name}</strong><small>{device.zone_id} · {device.hardware_id}</small></span>
                <span className="device-terminal"><strong>{device.zkt?.model || 'Awaiting terminal identity'}</strong><small>{device.zkt?.ip_address || 'No IP'} · {device.zkt?.serial || 'No serial'}</small></span>
                <span className="device-activity"><strong>{device.current_activity || 'Idle'}</strong><small>{relativeTime(device.last_seen_at)}</small></span>
                <StatusBadge state={device.state} live={device.connected} />
                <Icon name="chevron" />
              </button>
              <div className="device-card-actions">
                <button className="text-button" onClick={() => onManageUsers(device)}><Icon name="users" /> Manage users</button>
                <span>FW {device.firmware_version || 'unknown'} · {device.zkt?.certification_state || 'uncertified'}</span>
              </div>
            </article>
          ))}
          {!loading && !shown.length && (
            <div className="empty-state">
              <Icon name="server" />
              <h3>{devices.length ? 'No devices match these filters.' : 'Waiting for an authorized Zone Lite device to connect automatically.'}</h3>
              <p>{devices.length ? 'Change the search or state filter.' : 'A securely flashed ESP will appear here after signed onboarding.'}</p>
            </div>
          )}
        </div>
      </section>
    </>
  )
}

export function CommandProgress({
  command,
  onCancel,
}: {
  command: Command
  onCancel: (command: Command) => Promise<void>
}) {
  const canCancel = !['RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'].includes(command.status)
  return (
    <section className={`command-progress pattern-${statusPattern(command.status)}`} aria-live="polite">
      <span className="command-symbol"><Icon name={command.status === 'SUCCEEDED' ? 'check' : command.status === 'FAILED' ? 'alert' : 'refresh'} /></span>
      <div>
        <p className="eyebrow">{command.type.replaceAll('_', ' ')}</p>
        <h3>{command.status.replaceAll('_', ' ')}</h3>
        <p>{command.error_message || command.error_code || `Command ${command.command_id.slice(0, 8)} is durably tracked.`}</p>
      </div>
      {canCancel && <button className="button secondary" onClick={() => void onCancel(command)}>Cancel before execution</button>}
    </section>
  )
}

function UserOperationDialog({
  state,
  device,
  onClose,
  onCommand,
  toast,
}: {
  state: Exclude<UserDialogState, null>
  device: Device
  onClose: () => void
  onCommand: (command: Command) => void
  toast: ReturnType<typeof useToast>
}) {
  const user = state.mode === 'create' ? null : state.user
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [cnic, setCnic] = useState('')
  const [shiftWorker, setShiftWorker] = useState(user?.shift_worker || false)
  const [privilege, setPrivilege] = useState<0 | 14>(user?.privilege || 0)
  const [userIdOverride, setUserIdOverride] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const conflictRequiresCnic = Boolean(
    state.mode === 'edit' && user?.identity_conflict_code,
  )
  const preview = cnic
    ? buildMachinePreview(displayName, cnic, shiftWorker)
    : conflictRequiresCnic
      ? 'Enter the corrected CNIC to generate a safe terminal preview.'
      : user?.machine_name_preview || 'CNIC is preserved and never returned to the browser.'

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (state.mode === 'create') {
      const validation = validateUserDraft({ displayName, cnic, password, userIdOverride })
      if (validation) return setError(validation)
    } else if (state.mode === 'edit' && !displayName.trim()) {
      return setError('Full name is required.')
    } else if (state.mode === 'edit' && conflictRequiresCnic && !cnic) {
      return setError('A replacement CNIC is required to resolve this identity conflict.')
    } else if (state.mode === 'edit' && cnic && !/^\d{13}$/.test(cnic)) {
      return setError('CNIC must contain exactly 13 digits.')
    } else if (!password) {
      return setError('Password confirmation is required.')
    } else if (state.mode === 'delete' && !confirmationMatches(confirmation, state.user)) {
      return setError('Type the exact full name or user ID to confirm deletion.')
    }
    setBusy(true)
    try {
      if (state.mode === 'create') {
        const response = await api<UserCommandResponse>(`/api/v2/devices/${device.connector_id}/users`, {
          method: 'POST',
          body: JSON.stringify({
            display_name: displayName.trim(),
            cnic,
            shift_worker: shiftWorker,
            user_id_override: userIdOverride || null,
            password,
            idempotency_key: idempotency('create-user'),
          }),
        })
        onCommand(response.command)
        toast.notice('User creation is queued and will be verified by a terminal reread.')
      } else if (state.mode === 'edit') {
        const response = await api<UserCommandResponse>(
          `/api/v2/devices/${device.connector_id}/users/${state.user.user_key}`,
          {
            method: 'PATCH',
            body: JSON.stringify({
              display_name: displayName.trim(),
              ...(cnic ? { cnic } : {}),
              shift_worker: shiftWorker,
              privilege,
              expected_version: state.user.row_version,
              password,
              idempotency_key: idempotency('update-user'),
            }),
          },
        )
        onCommand(response.command)
        toast.notice('User update is queued with optimistic version checks.')
      } else if (state.mode === 'delete') {
        const response = await api<UserCommandResponse>(
          `/api/v2/devices/${device.connector_id}/users/${state.user.user_key}`,
          {
            method: 'DELETE',
            body: JSON.stringify({
              expected_version: state.user.row_version,
              typed_confirmation: confirmation,
              password,
              idempotency_key: idempotency('delete-user'),
            }),
          },
        )
        onCommand(response.command)
        toast.notice('Deletion is queued. Attendance counts must remain unchanged.')
      } else {
        const response = await api<{ command: Command }>(`/api/v1/devices/${device.connector_id}/admin-leases`, {
          method: 'POST',
          body: JSON.stringify({
            uid: state.user.uid,
            password,
            idempotency_key: idempotency('enrollment-lease'),
          }),
        })
        onCommand(response.command)
        toast.notice('Temporary administrator access is queued for 10 minutes.')
      }
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The operation could not be queued.')
    } finally {
      setBusy(false)
    }
  }

  const copy = {
    create: ['Add user to selected terminal', 'Creates a regular user on this ZKT only.'],
    edit: ['Edit device user', 'Name, replacement CNIC, shift status, and permanent role are verified after write.'],
    delete: ['Delete user from terminal', 'The user is removed; punches and identity history remain permanently preserved.'],
    lease: ['Grant enrollment access', 'The selected regular user becomes administrator for 10 minutes, then reverts automatically.'],
  } as const
  const [title, description] = copy[state.mode]
  return (
    <Dialog titleId="user-operation-title" title={title} description={description} onClose={onClose}>
      <form className="dialog-body" onSubmit={submit}>
        {(state.mode === 'create' || state.mode === 'edit') && (
          <>
            <div className="form-grid">
              <label>Full canonical name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={255} /></label>
              <label>{state.mode === 'edit' ? conflictRequiresCnic ? 'Replacement CNIC (required to resolve conflict)' : 'Replacement CNIC (leave blank to preserve)' : 'CNIC'}<input inputMode="numeric" autoComplete="off" value={cnic} onChange={(event) => setCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" required={conflictRequiresCnic} /></label>
              {state.mode === 'create' && <label>Employee/user ID override (optional)<input inputMode="numeric" value={userIdOverride} onChange={(event) => setUserIdOverride(event.target.value.replace(/\D/g, '').slice(0, 24))} /></label>}
              {state.mode === 'edit' && <label>Permanent role<select value={privilege} onChange={(event) => setPrivilege(Number(event.target.value) as 0 | 14)}><option value={0}>Regular user</option><option value={14}>Permanent administrator</option></select></label>}
            </div>
            <label className="check-field"><input type="checkbox" checked={shiftWorker} onChange={(event) => setShiftWorker(event.target.checked)} /><span><strong>Shift worker</strong><small>Adds the -S- identity marker used for raw-punch handling.</small></span></label>
            <div className="preview-box"><span>Exact ZKT 24-byte name preview</span><code>{preview}</code><small>{cnic ? `${utf8Length(preview)} / 24 UTF-8 bytes` : conflictRequiresCnic ? 'Correction is required before this update can be queued.' : 'Stored CNIC remains write-only.'}</small></div>
          </>
        )}
        {state.mode === 'delete' && (
          <div className="destructive-copy pattern-blocked">
            <Icon name="trash" />
            <div><h3>{state.user.display_name}</h3><p>UID {state.user.uid} · User ID {state.user.user_id}</p><p>ADD and ZKT attendance records will not be deleted.</p></div>
          </div>
        )}
        {state.mode === 'lease' && (
          <div className="info-copy pattern-waiting"><Icon name="clock" /><div><h3>10-minute automatic lease</h3><p>{state.user.display_name} will be elevated only on {device.display_name}. The ESP watchdog revokes access even if ADD disconnects.</p></div></div>
        )}
        {state.mode === 'delete' && <label>Type “{state.user.display_name}” or “{state.user.user_id}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>}
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className={`button ${state.mode === 'delete' ? 'destructive' : 'primary'}`} disabled={busy}>{busy ? 'Queuing…' : state.mode === 'delete' ? 'Delete user safely' : state.mode === 'lease' ? 'Grant 10-minute access' : 'Confirm operation'}</button>
        </footer>
      </form>
    </Dialog>
  )
}

function UsersView({
  devices,
  selectedDeviceId,
  onSelectDevice,
  revision,
  toast,
  refreshFleet,
}: {
  devices: Device[]
  selectedDeviceId: string
  onSelectDevice: (id: string) => void
  revision: number
  toast: ReturnType<typeof useToast>
  refreshFleet: () => Promise<void>
}) {
  const selected = devices.find((device) => device.connector_id === selectedDeviceId)
  const [rows, setRows] = useState<DeviceUser[]>([])
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [identity, setIdentity] = useState('ALL')
  const [role, setRole] = useState('ALL')
  const [dialog, setDialog] = useState<UserDialogState>(null)
  const [command, setCommand] = useState<Command | null>(null)
  const [integrity, setIntegrity] = useState<IdentityIntegrity | null>(null)

  const load = useCallback(async () => {
    if (!selected) {
      setRows([])
      setIntegrity(null)
      return
    }
    setLoading(true)
    try {
      const compact = query.replace(/\D/g, '')
      const cnicSearch = compact.length === 13 && compact === query.replace(/[\s-]/g, '')
      const result = await api<{
        rows: DeviceUser[]
        identity_integrity: IdentityIntegrity
      }>(
        `/api/v2/devices/${selected.connector_id}/users${queryString({
          q: cnicSearch ? undefined : query,
          cnic: cnicSearch ? compact : undefined,
          identity: identity === 'ALL' ? undefined : identity,
          privilege: role === 'ALL' ? undefined : role,
        })}`,
      )
      setRows(result.rows)
      setIntegrity(result.identity_integrity || null)
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to load users.')
    } finally {
      setLoading(false)
    }
  }, [identity, query, role, selected, toast.error])

  useEffect(() => {
    const timeout = window.setTimeout(() => void load(), 250)
    return () => window.clearTimeout(timeout)
  }, [load, revision])

  useEffect(() => {
    if (!command || terminalCommandStates.has(command.status)) return
    const timeout = window.setTimeout(async () => {
      try {
        const updated = await api<Command>(`/api/v2/commands/${command.command_id}`)
        setCommand(updated)
        if (terminalCommandStates.has(updated.status)) {
          await Promise.all([load(), refreshFleet()])
          if (updated.status === 'SUCCEEDED') toast.notice(`${updated.type.replaceAll('_', ' ')} completed and was verified.`)
          else toast.error(updated.error_message || `${updated.type.replaceAll('_', ' ')} ended as ${updated.status}.`)
        }
      } catch (reason) {
        toast.error(reason instanceof Error ? reason.message : 'Unable to refresh command state.')
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [command, load, refreshFleet, toast.error, toast.notice])

  const cancel = async (row: Command) => {
    try {
      setCommand(await api<Command>(`/api/v2/commands/${row.command_id}/cancel`, { method: 'POST', body: '{}' }))
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to cancel command.')
    }
  }

  const writable = Boolean(
    selected?.zkt?.certification_state === 'CERTIFIED' &&
      selected.zkt.snapshot_complete &&
      selected.zkt.capabilities.user_write,
  )

  return (
    <>
      <PageHeader
        eyebrow="SELECTED-TERMINAL USER CONTROL"
        title="Device users"
        description="Create, enrich, edit, elevate, and safely remove identities on one selected ZKT terminal."
        action={selected && <button className="button primary" disabled={!writable} onClick={() => setDialog({ mode: 'create' })}><Icon name="userPlus" /> Add user</button>}
      />
      <section className="panel selection-panel">
        <label>Selected terminal<select value={selectedDeviceId} onChange={(event) => onSelectDevice(event.target.value)}><option value="">Choose a terminal</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name} · {device.zkt?.serial || 'awaiting serial'}</option>)}</select></label>
        {selected && <div className="selected-device-summary"><StatusBadge state={selected.state} live={selected.connected} /><span>{selected.zkt?.model || 'ZKT terminal'} · {selected.zkt?.ip_address || 'No IP'}</span><span>Users {selected.zkt?.user_count ?? '—'} · Last sync {relativeTime(selected.last_seen_at)}</span></div>}
      </section>
      {!selected ? (
        <section className="panel empty-state"><Icon name="users" /><h2>Select a terminal to manage its users.</h2><p>Mutations are always scoped to one selected ZKT device.</p></section>
      ) : (
        <>
          {!writable && <div className="capability-banner pattern-waiting"><Icon name="shield" /><div><strong>User writes are unavailable.</strong><span>{selected.zkt?.writes_disabled_reason || 'The terminal is not yet write-certified or its complete snapshot is pending.'}</span></div><StatusBadge state={selected.zkt?.certification_state || 'READ_ONLY'} /></div>}
          {integrity && integrity.duplicate_users > 0 && (
            <div className="capability-banner pattern-blocked" role="status">
              <Icon name="alert" />
              <div>
                <strong>Exact CNIC conflicts are present in the terminal data.</strong>
                <span>
                  The latest {integrity.source === 'CURRENT_COMPLETE_ZKT_SNAPSHOT' ? 'complete ' : ''}
                  ZKT snapshot reports {integrity.duplicate_users} users across {integrity.duplicate_groups}{' '}
                  exact-CNIC groups. Matching terminal user IDs are shown below; unsafe reuse remains blocked.
                </span>
              </div>
              <StatusBadge state="CORRECTION REQUIRED" />
            </div>
          )}
          {command && <CommandProgress command={command} onCancel={cancel} />}
          <section className="panel">
            <div className="toolbar user-toolbar">
              <label className="search-field"><span className="sr-only">Search users</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, user ID, or exact CNIC" /></label>
              <label><span className="sr-only">Identity completeness</span><select value={identity} onChange={(event) => setIdentity(event.target.value)}><option value="ALL">All identities</option><option value="COMPLETE">CNIC complete</option><option value="MISSING">CNIC missing or conflicted</option><option value="CONFLICT">CNIC conflict</option></select></label>
              <label><span className="sr-only">Role</span><select value={role} onChange={(event) => setRole(event.target.value)}><option value="ALL">All roles</option><option value="0">Regular users</option><option value="14">Administrators</option></select></label>
              <button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh users</button>
            </div>
            <div className="user-table" aria-busy={loading}>
              <div className="user-table-head"><span>Identity</span><span>Terminal record</span><span>Role & shift</span><span>Last sync</span><span>Actions</span></div>
              {loading && <div className="empty-state compact"><Icon name="refresh" /><p>Reading the selected terminal user view…</p></div>}
              {!loading && rows.map((user) => (
                <article key={user.user_key} className={`user-row ${user.identity_conflict_code ? 'identity-conflict' : user.identity_complete ? '' : 'identity-missing'}`}>
                  <div className="user-person"><span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>{user.identity_conflict_code ? identityConflictText(user) : user.cnic_masked || 'CNIC missing · punches blocked until enriched'}</small></span></div>
                  <div><strong>User {user.user_id}</strong><small>UID {user.uid} · v{user.row_version}</small><code>{user.machine_name_preview || 'No machine preview'}</code></div>
                  <div><StatusBadge state={user.privilege === 14 ? 'ADMINISTRATOR' : 'REGULAR'} /><small>{user.shift_worker ? 'Shift worker' : 'Standard worker'}</small></div>
                  <div><strong>{relativeTime(user.observed_at)}</strong><small>{dateTime(user.observed_at)}</small></div>
                  <div className="row-actions">
                    <button className="icon-button" disabled={!writable} onClick={() => setDialog({ mode: 'edit', user })} aria-label={`Edit ${user.display_name}`}><Icon name="edit" /></button>
                    <button className="icon-button" disabled={!writable || user.privilege !== 0} onClick={() => setDialog({ mode: 'lease', user })} aria-label={`Grant enrollment access to ${user.display_name}`}><Icon name="shield" /></button>
                    <button className="icon-button" disabled={!writable || user.privilege === 14} onClick={() => setDialog({ mode: 'delete', user })} aria-label={`Delete ${user.display_name}`}><Icon name="trash" /></button>
                  </div>
                </article>
              ))}
              {!loading && !rows.length && <div className="empty-state"><Icon name="users" /><h3>No users match this selected-terminal view.</h3><p>Change the filters or add the first ADD-managed user.</p></div>}
            </div>
          </section>
        </>
      )}
      {dialog && selected && <UserOperationDialog state={dialog} device={selected} onClose={() => setDialog(null)} onCommand={setCommand} toast={toast} />}
    </>
  )
}

function AttendanceView({ devices, revision }: { devices: Device[]; revision: number }) {
  const [rows, setRows] = useState<AttendanceEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [filters, setFilters] = useState({ device_id: '', q: '', cnic: '', punch: '', clock_quality: '', from_time: '', to_time: '' })
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const response = await api<{ rows: AttendanceEvent[] }>(`/api/v1/attendance${queryString(filters)}`)
      setRows(response.rows)
    } finally {
      setLoading(false)
    }
  }, [filters])
  useEffect(() => { void load() }, [load, revision])
  return (
    <>
      <PageHeader eyebrow="IMMUTABLE CAPTURE LEDGER" title="Attendance events" description="Filter live and reconciled punches without changing terminal history." action={<button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh</button>} />
      <section className="panel">
        <div className="filter-grid">
          <label>Device<select value={filters.device_id} onChange={(event) => setFilters({ ...filters, device_id: event.target.value })}><option value="">All devices</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name}</option>)}</select></label>
          <label>Name / user ID<input value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} /></label>
          <label>Exact CNIC<input inputMode="numeric" value={filters.cnic} onChange={(event) => setFilters({ ...filters, cnic: event.target.value.replace(/\D/g, '').slice(0, 13) })} placeholder="13 digits" /></label>
          <label>Punch<select value={filters.punch} onChange={(event) => setFilters({ ...filters, punch: event.target.value })}><option value="">All punches</option><option value="0">Check in</option><option value="1">Check out</option></select></label>
          <label>Clock quality<select value={filters.clock_quality} onChange={(event) => setFilters({ ...filters, clock_quality: event.target.value })}><option value="">Any quality</option><option value="OK">OK</option><option value="DRIFTED">Drifted</option><option value="UNKNOWN">Unknown</option></select></label>
          <label>From<input type="datetime-local" value={filters.from_time} onChange={(event) => setFilters({ ...filters, from_time: event.target.value })} /></label>
          <label>To<input type="datetime-local" value={filters.to_time} onChange={(event) => setFilters({ ...filters, to_time: event.target.value })} /></label>
        </div>
        <div className="attendance-table">
          <div className="attendance-head"><span>Employee</span><span>Event time</span><span>Terminal</span><span>Capture</span><span>Delivery</span></div>
          {loading && <div className="empty-state compact">Loading attendance ledger…</div>}
          {!loading && rows.map((row) => <article key={row.event_uid}><div><strong>{row.display_name || 'Unknown identity'}</strong><small>{row.cnic_masked || `User ${row.user_id}`}</small></div><div><strong>{dateTime(row.device_event_time)}</strong><small>Received {relativeTime(row.received_at)}</small></div><div><strong>{row.device_serial || 'Unreported serial'}</strong><small>UID {row.uid || '—'} · User {row.user_id}</small></div><div><StatusBadge state={row.source} /><small>Punch {row.punch ?? '—'} · {row.clock_quality}</small></div><div><StatusBadge state={row.ords_status} /><small>{row.clock_drift_seconds == null ? 'No drift sample' : `${Math.round(row.clock_drift_seconds)}s clock drift`}</small></div></article>)}
          {!loading && !rows.length && <div className="empty-state"><Icon name="clock" /><h3>No attendance matches these filters.</h3></div>}
        </div>
      </section>
    </>
  )
}

function AlertsView({ devices, toast, revision }: { devices: Device[]; toast: ReturnType<typeof useToast>; revision: number }) {
  const [rows, setRows] = useState<(Alert & { device: Device })[]>([])
  const load = useCallback(async () => {
    const responses = await Promise.all(devices.map(async (device) => ({ device, rows: (await api<{ rows: Alert[] }>(`/api/v1/devices/${device.connector_id}/alerts`)).rows })))
    setRows(responses.flatMap((response) => response.rows.map((row) => ({ ...row, device: response.device }))).sort((a, b) => +new Date(b.last_seen_at) - +new Date(a.last_seen_at)))
  }, [devices])
  useEffect(() => { void load() }, [load, revision])
  const acknowledge = async (row: Alert) => {
    try {
      await api(`/api/v1/alerts/${row.id}/acknowledge`, { method: 'POST', body: '{}' })
      toast.notice('Alert acknowledged with an audit entry.')
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to acknowledge alert.')
    }
  }
  return (
    <>
      <PageHeader eyebrow="OPERATIONS QUEUE" title="Alerts and exceptions" description="Device conditions prioritized with labels, icons, and border patterns." action={<button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh</button>} />
      <section className="alert-list">
        {rows.map((row) => {
          const diagnostics = formatAlertDiagnostics(row.details)
          return <article className={`alert-card pattern-${statusPattern(row.severity)}`} key={`${row.device.connector_id}-${row.id}`}><span className="alert-icon"><Icon name="alert" /></span><div><div className="alert-meta"><StatusBadge state={row.severity} /><span>{row.device.display_name} · {row.device.zone_id}</span></div><h2>{row.message}</h2><p>{row.code} · First {dateTime(row.first_seen_at)} · Last {relativeTime(row.last_seen_at)}</p>{diagnostics && <p className="alert-diagnostics" aria-label="Safe alert diagnostics">{diagnostics}</p>}</div>{row.state === 'OPEN' ? <button className="button secondary" onClick={() => void acknowledge(row)}><Icon name="check" /> Acknowledge</button> : <StatusBadge state={row.state} />}</article>
        })}
        {!rows.length && <div className="panel empty-state"><Icon name="shield" /><h2>No recorded device exceptions.</h2><p>The queue will update from live ESP telemetry.</p></div>}
      </section>
    </>
  )
}

export function formatAlertDiagnostics(details: Record<string, unknown>): string {
  const facts: string[] = []
  const category = details.failure_category
  if (typeof category === 'string' && /^[A-Z0-9_]{1,80}$/.test(category)) {
    facts.push(`Category ${category}`)
  }
  const httpStatus = details.http_status
  if (typeof httpStatus === 'number' && Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599) {
    facts.push(`HTTP ${httpStatus}`)
  }
  const attemptCount = details.attempt_count
  if (typeof attemptCount === 'number' && Number.isInteger(attemptCount) && attemptCount >= 0) {
    facts.push(`Attempt ${attemptCount}`)
  }
  const affectedUsers = details.affected_users
  if (typeof affectedUsers === 'number' && Number.isInteger(affectedUsers) && affectedUsers >= 0) {
    facts.push(`${affectedUsers} affected users`)
  }
  return facts.join(' · ')
}

function DeviceDrawer({
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
          <article className="detail-card"><p className="eyebrow">CAPTURE HEALTH</p><h3>{device.zkt?.attendance_count ?? '—'} terminal punches</h3><p>{device.zkt?.user_count ?? '—'} users · Last 15-minute reconciliation {relativeTime(device.zkt?.last_reconcile_at)}</p></article>
          {device.last_error_code && <article className="detail-card wide pattern-blocked"><p className="eyebrow">ACTIVE PROBLEM</p><h3>{device.last_error_code.replaceAll('_', ' ')}</h3><p>{device.zkt?.writes_disabled_reason || 'Review live logs and connectivity history.'}</p></article>}
          <article className="detail-card wide"><div className="detail-title"><div><p className="eyebrow">INTERMITTENT CONNECTIVITY HISTORY</p><h3>Bounded reconnect and anti-flap state</h3></div><StatusBadge state={device.zkt?.connection_state || 'UNKNOWN'} /></div><div className="connection-list">{connections.slice(0, 12).map((row) => <div key={row.id}><time>{dateTime(row.observed_at)}</time><StatusBadge state={row.from_state || 'START'} /><Icon name="chevron" /><StatusBadge state={row.to_state} /><span>{row.reason || 'State observation'} · failures {row.consecutive_failures} · flaps {row.flap_count_15m}</span></div>)}{!connections.length && <p>No connectivity transitions recorded yet.</p>}</div></article>
        </div>}
        {tab === 'logs' && <section className="terminal-view" aria-label="Live ESP serial monitor"><header><span><i /><i /><i /></span><strong>{device.hardware_id} · live operations log</strong><button className="text-button" onClick={() => void load()}><Icon name="refresh" /> Refresh</button></header><div>{logs.map((row) => <p key={row.id} className={`log-pattern-${statusPattern(row.level)}`}><time>{dateTime(row.device_time || row.received_at)}</time><strong>{row.level}</strong><em>{row.subsystem}</em><span>{row.code ? `[${row.code}] ` : ''}{row.message}</span></p>)}{!logs.length && <div className="terminal-empty">Waiting for live Zone Lite logs…</div>}</div></section>}
        {tab === 'control' && <div className="control-stack">
          <article className="control-card"><span><Icon name="users" /></span><div><h3>Selected-terminal users</h3><p>Create, edit, delete, or grant a 10-minute enrollment lease. Every write requires current certification and a full snapshot.</p></div><button className="button primary" onClick={() => onManageUsers(device)}>Open Users workspace</button></article>
          <article className="control-card"><span><Icon name="refresh" /></span><div><h3>Refresh terminal users</h3><p>Request a complete serialized user table reread without opening a competing ZKT connection.</p></div><button className="button secondary" onClick={() => void refreshUsers()}>Request reread</button></article>
          <article className="control-card pattern-blocked"><span><Icon name="power" /></span><div><h3>Restart ZKT terminal</h3><p>Issues an authenticated protocol restart. Active enrollment leases block this operation.</p><label>Reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label><label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label></div><button className="button destructive" disabled={busy} onClick={() => void restart()}>{busy ? 'Queuing…' : 'Restart terminal'}</button></article>
          {device.active_command && <CommandProgress command={device.active_command} onCancel={async () => undefined} />}
        </div>}
      </div>
    </Dialog>
  )
}

export default function App() {
  const [authState, setAuthState] = useState<'loading' | 'anonymous' | 'authenticated'>('loading')
  const [username, setUsername] = useState('')
  const [view, setView] = useState<View>('fleet')
  const [devices, setDevices] = useState<Device[]>([])
  const [overview, setOverview] = useState<Overview>({ total: 0, open_alerts: 0, active_leases: 0 })
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const [selectedDeviceId, setSelectedDeviceId] = useState('')
  const [drawer, setDrawer] = useState<Device | null>(null)
  const toast = useToast()

  const refreshFleet = useCallback(async () => {
    setLoading(true)
    try {
      const [counts, fleet] = await Promise.all([
        api<Overview>('/api/v1/overview'),
        api<{ rows: Device[] }>('/api/v1/devices'),
      ])
      setOverview(counts)
      setDevices(fleet.rows)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) setAuthState('anonymous')
      else toast.error(reason instanceof Error ? reason.message : 'Unable to refresh fleet.')
    } finally {
      setLoading(false)
    }
  }, [toast.error])

  useEffect(() => {
    api<{ username: string; csrf_token: string }>('/api/v1/auth/session')
      .then((session) => {
        setCsrfToken(session.csrf_token)
        setUsername(session.username)
        setAuthState('authenticated')
      })
      .catch(() => setAuthState('anonymous'))
  }, [])

  useEffect(() => {
    if (authState !== 'authenticated') return
    void refreshFleet()
    if (typeof EventSource === 'undefined') return
    const stream = new EventSource('/events/v1/stream', { withCredentials: true })
    stream.onmessage = () => setRevision((value) => value + 1)
    stream.addEventListener('device', () => { setRevision((value) => value + 1); void refreshFleet() })
    return () => stream.close()
  }, [authState, refreshFleet])

  useEffect(() => {
    if (authState === 'authenticated') void refreshFleet()
  }, [revision, authState, refreshFleet])

  const login = async (loginUsername: string, password: string) => {
    const response = await api<{ username: string; csrf_token: string }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: loginUsername, password }),
    })
    setCsrfToken(response.csrf_token)
    setUsername(response.username)
    setAuthState('authenticated')
  }

  const logout = async () => {
    try { await api('/api/v1/auth/logout', { method: 'POST', body: '{}' }) } finally {
      setCsrfToken('')
      setAuthState('anonymous')
      setDevices([])
    }
  }

  const manageUsers = (device: Device) => {
    setSelectedDeviceId(device.connector_id)
    setDrawer(null)
    setView('users')
  }

  const nav = useMemo(() => [
    { id: 'fleet' as const, label: 'Fleet', icon: 'grid' as const },
    { id: 'users' as const, label: 'Users', icon: 'users' as const },
    { id: 'attendance' as const, label: 'Attendance', icon: 'clock' as const },
    { id: 'alerts' as const, label: 'Alerts', icon: 'alert' as const },
  ], [])

  if (authState === 'loading') return <main className="boot-screen"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><p>Opening Attendance Device Dashboard…</p></main>
  if (authState === 'anonymous') return <Login onLogin={login} />

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="app-brand" href="#fleet" onClick={() => setView('fleet')}><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><span><strong>Attendance Device Dashboard</strong><small>National command center</small></span></a>
        <nav aria-label="Primary navigation">{nav.map((item) => <button key={item.id} className={view === item.id ? 'active' : ''} aria-current={view === item.id ? 'page' : undefined} onClick={() => setView(item.id)}><Icon name={item.icon} />{item.label}{item.id === 'alerts' && overview.open_alerts > 0 && <span className="nav-count" aria-label={`${overview.open_alerts} open alerts`}>{overview.open_alerts}</span>}</button>)}</nav>
        <div className="operator-area"><span className="live-sync"><i /> Live sync</span><span><strong>{username}</strong><small>State Life operator</small></span><button className="icon-button" onClick={() => void logout()} aria-label="Sign out"><Icon name="logout" /></button></div>
      </header>
      <main className="page-content">
        {view === 'fleet' && <FleetView devices={devices} overview={overview} loading={loading} onInspect={setDrawer} onManageUsers={manageUsers} />}
        {view === 'users' && <UsersView devices={devices} selectedDeviceId={selectedDeviceId} onSelectDevice={setSelectedDeviceId} revision={revision} toast={toast} refreshFleet={refreshFleet} />}
        {view === 'attendance' && <AttendanceView devices={devices} revision={revision} />}
        {view === 'alerts' && <AlertsView devices={devices} toast={toast} revision={revision} />}
      </main>
      {drawer && <DeviceDrawer seed={drawer} revision={revision} onClose={() => setDrawer(null)} onManageUsers={manageUsers} toast={toast} />}
      {toast.toast && <div className={`toast pattern-${toast.toast.kind === 'error' ? 'blocked' : 'confirmed'}`} role="status" aria-live="polite"><Icon name={toast.toast.kind === 'error' ? 'alert' : 'check'} />{toast.toast.text}</div>}
    </div>
  )
}
