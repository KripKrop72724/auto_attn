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
  FirmwareCampaign,
  FirmwareRelease,
  HistoricalIdentityCandidate,
  HistoricalIdentityReport,
  IdentityConflictGroup,
  IdentityConflictReport,
  IdentityIntegrity,
  Overview,
  UserCommandResponse,
  UserDeletionJob,
} from './types'

type View = 'fleet' | 'users' | 'attendance' | 'firmware' | 'alerts'
type DrawerTab = 'overview' | 'logs' | 'control'
type ToastState = { kind: 'notice' | 'error'; text: string } | null
type UserDialogState =
  | { mode: 'create' }
  | { mode: 'edit'; user: DeviceUser }
  | { mode: 'delete'; user: DeviceUser }
  | { mode: 'lease'; user: DeviceUser }
  | null
type IdentityResolutionDialogState = {
  mode: 'resolve' | 'revoke'
  group: IdentityConflictGroup
} | null
type HistoricalIdentityDialogState = {
  candidate: HistoricalIdentityCandidate
} | null

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
  if (user.identity_conflict_resolved) {
    return matches
      ? `${user.cnic_masked || 'Masked CNIC'} · Verified same employee across user ${matches}`
      : `${user.cnic_masked || 'Masked CNIC'} · Verified same-employee terminal alias`
  }
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

export const bulkDeletionConfirmation = (count: number, deviceId: string) =>
  `DELETE ${count} USERS FROM ${deviceId}`

const normalizedStatus = (state: unknown) =>
  typeof state === 'string' && state.trim() ? state.trim() : 'UNKNOWN'

const statusPattern = (state: unknown) => {
  const normalized = normalizedStatus(state).toUpperCase()
  if (
    ['ONLINE', 'SUCCEEDED', 'CERTIFIED', 'ACTIVE', 'OK', 'RESOLVED', 'COMPLETE'].includes(normalized) ||
    normalized.includes('ACKED')
  )
    return 'confirmed'
  if (
    ['OFFLINE', 'FAILED', 'PARTIAL', 'CRITICAL', 'EXPIRED', 'QUARANTINED', 'BLOCKED_IDENTITY'].some(
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

export function StatusBadge({
  state,
  live = false,
}: {
  state?: string | null
  live?: boolean
}) {
  const status = normalizedStatus(state)
  const pattern = statusPattern(status)
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
      <span>{status.replaceAll('_', ' ')}</span>
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
        <Metric
          label="ORDS delivery queue"
          value={delivery?.backlog ?? 0}
          detail={`${delivery?.retrying ?? 0} retrying · ${delivery?.blocked_identity ?? 0} identity blocked · ${delivery?.quarantined ?? 0} quarantined · ${delivery?.firmware_unverified ?? delivery?.acknowledged_firmware ?? 0} firmware receipts awaiting Oracle proof · ${delivery?.membership_reverify ?? 0} periodic checks`}
          icon="clock"
        />
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
                <span>FW {device.firmware_version || 'unknown'} · {device.ota_capable ? (device.ota_state || 'OTA ready') : 'Manual firmware updates'} · {device.zkt?.certification_state || 'uncertified'}</span>
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
  const status = normalizedStatus(command.status)
  const commandType = normalizedStatus(command.type)
  const canCancel = !['RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'].includes(status)
  return (
    <section className={`command-progress pattern-${statusPattern(status)}`} aria-live="polite">
      <span className="command-symbol"><Icon name={status === 'SUCCEEDED' ? 'check' : status === 'FAILED' ? 'alert' : 'refresh'} /></span>
      <div>
        <p className="eyebrow">{commandType.replaceAll('_', ' ')}</p>
        <h3>{status.replaceAll('_', ' ')}</h3>
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
    state.mode === 'edit' && user?.identity_conflict_code && !user.identity_conflict_resolved,
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

function IdentityResolutionDialog({
  state,
  device,
  onClose,
  onComplete,
  toast,
}: {
  state: Exclude<IdentityResolutionDialogState, null>
  device: Device
  onClose: () => void
  onComplete: (report: IdentityConflictReport) => void
  toast: ReturnType<typeof useToast>
}) {
  const resolving = state.mode === 'resolve'
  const expectedConfirmation = resolving ? 'SAME EMPLOYEE' : 'REVOKE RESOLUTION'
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (reason.trim().length < 10) return setError('Record a reason of at least 10 characters.')
    if (confirmation !== expectedConfirmation) {
      return setError(`Type ${expectedConfirmation} exactly to continue.`)
    }
    if (!password) return setError('Password confirmation is required.')
    setBusy(true)
    try {
      const path = resolving
        ? `/api/v2/devices/${device.connector_id}/identity-conflicts/resolve`
        : `/api/v2/devices/${device.connector_id}/identity-conflicts/${state.group.resolution_id}/revoke`
      const body = resolving
        ? {
            group_token: state.group.group_token,
            members: state.group.members.map((member) => ({
              user_key: member.user_key,
              expected_version: member.row_version,
            })),
            reason: reason.trim(),
            typed_confirmation: expectedConfirmation,
            password,
            idempotency_key: idempotency('identity-resolution'),
          }
        : {
            reason: reason.trim(),
            typed_confirmation: expectedConfirmation,
            password,
          }
      const response = await api<{ report: IdentityConflictReport }>(path, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      onComplete(response.report)
      toast.notice(
        resolving
          ? 'Same-employee alias approved. No terminal user or attendance row was changed.'
          : 'Identity resolution revoked. New punches return to identity quarantine.',
      )
      onClose()
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'Resolution could not be saved.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="identity-resolution-title"
      title={resolving ? 'Verify same employee' : 'Revoke identity resolution'}
      description={
        resolving
          ? 'Approve only after the member records are confirmed to belong to one legal identity.'
          : 'Re-enable quarantine if the prior same-employee decision was incorrect.'
      }
      onClose={onClose}
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className={resolving ? 'info-copy pattern-waiting' : 'destructive-copy pattern-blocked'}>
          <Icon name={resolving ? 'shield' : 'alert'} />
          <div>
            <h3>{state.group.cnic_masked || 'Masked CNIC group'}</h3>
            <p>{state.group.members.map((member) => `${member.display_name} · User ${member.user_id}`).join(' | ')}</p>
            <p>No ZKT user, fingerprint template, UID, or attendance event is merged, deleted, or rewritten.</p>
          </div>
        </div>
        <label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className={`button ${resolving ? 'primary' : 'destructive'}`} disabled={busy}>{busy ? 'Saving…' : resolving ? 'Approve same employee' : 'Revoke resolution'}</button>
        </footer>
      </form>
    </Dialog>
  )
}

function HistoricalIdentityResolutionDialog({
  state,
  device,
  onClose,
  onComplete,
  toast,
}: {
  state: Exclude<HistoricalIdentityDialogState, null>
  device: Device
  onClose: () => void
  onComplete: () => Promise<void>
  toast: ReturnType<typeof useToast>
}) {
  const candidate = state.candidate
  const [cnic, setCnic] = useState('')
  const [employeeId, setEmployeeId] = useState('')
  const [serviceNumber, setServiceNumber] = useState(candidate.user_id)
  const [employeeName, setEmployeeName] = useState(candidate.display_name)
  const [zoneCode, setZoneCode] = useState(device.zone_id)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const expectedConfirmation = `${candidate.user_id} -> HR ${employeeId}`

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (!/^\d{13}$/.test(cnic)) return setError('Directory CNIC must contain exactly 13 digits.')
    if (!/^\d+$/.test(employeeId)) return setError('Directory employee ID must contain only digits.')
    if (!/^[A-Za-z0-9._-]+$/.test(serviceNumber)) {
      return setError('Directory service number contains unsupported characters.')
    }
    if (!employeeName.trim()) return setError('Directory employee name is required.')
    if (reason.trim().length < 10) return setError('Record an audit reason of at least 10 characters.')
    if (confirmation !== expectedConfirmation) {
      return setError(`Type ${expectedConfirmation} exactly to continue.`)
    }
    if (!password) return setError('Password confirmation is required.')
    if (
      candidate.source_kind === 'EVENT_GROUP' &&
      (!candidate.group_token || !candidate.uid)
    ) {
      return setError('The exact historical event cohort is incomplete. Refresh and retry.')
    }
    if (
      candidate.source_kind !== 'EVENT_GROUP' &&
      (!candidate.source_user_key || candidate.row_version == null)
    ) {
      return setError('The historical terminal user is incomplete. Refresh and retry.')
    }
    setBusy(true)
    try {
      const eventGroup = candidate.source_kind === 'EVENT_GROUP'
      const response = await api<{ repaired_events: number }>(
        `/api/v2/devices/${device.connector_id}/historical-identities/${
          eventGroup ? 'resolve-event-group' : 'resolve'
        }`,
        {
          method: 'POST',
          body: JSON.stringify({
            ...(eventGroup
              ? {
                  group_token: candidate.group_token,
                  source_user_id: candidate.user_id,
                  source_uid: candidate.uid,
                }
              : {
                  source_user_key: candidate.source_user_key,
                  expected_version: candidate.row_version,
                }),
            source_cnic: cnic,
            directory_employee_id: employeeId,
            directory_service_number: serviceNumber,
            directory_employee_name: employeeName.trim(),
            directory_zone_code: zoneCode.trim() || null,
            reason: reason.trim(),
            typed_confirmation: confirmation,
            password,
            idempotency_key: idempotency('historical-directory-identity'),
          }),
        },
      )
      await onComplete()
      toast.notice(
        `${response.repaired_events.toLocaleString()} preserved attendance event${response.repaired_events === 1 ? '' : 's'} requeued for Oracle confirmation.`,
      )
      onClose()
    } catch (reasonValue) {
      setError(
        reasonValue instanceof Error
          ? reasonValue.message
          : 'Historical identity evidence could not be saved.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="historical-identity-resolution-title"
      title="Enter verified HR identity evidence"
      description="Use authoritative HR directory evidence only. ADD will preserve every attendance event and requeue only an exact, unambiguous match."
      onClose={onClose}
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className="info-copy pattern-waiting">
          <Icon name="shield" />
          <div>
            <h3>{candidate.display_name}</h3>
            <p>User {candidate.user_id} · UID {candidate.uid} · version {candidate.row_version}</p>
            <p>{candidate.event_count.toLocaleString()} preserved events from {dateTime(candidate.first_event_at)} to {dateTime(candidate.last_event_at)}.</p>
          </div>
        </div>
        <div className="form-grid">
          <label>Authoritative CNIC<input inputMode="numeric" autoComplete="off" value={cnic} onChange={(event) => setCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" /></label>
          <label>HR employee ID<input inputMode="numeric" value={employeeId} onChange={(event) => setEmployeeId(event.target.value.replace(/\D/g, '').slice(0, 32))} /></label>
          <label>HR service number<input value={serviceNumber} onChange={(event) => setServiceNumber(event.target.value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64))} /></label>
          <label>HR employee name<input value={employeeName} onChange={(event) => setEmployeeName(event.target.value)} maxLength={255} /></label>
          <label>HR zone code<input value={zoneCode} onChange={(event) => setZoneCode(event.target.value)} maxLength={64} /></label>
        </div>
        <div className="message pattern-blocked" role="note">
          <Icon name="alert" />
          Do not infer or guess a CNIC. The terminal service number and employee name must match the authoritative HR record.
        </div>
        <label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={busy}>{busy ? 'Verifying and requeuing…' : 'Save verified HR evidence'}</button>
        </footer>
      </form>
    </Dialog>
  )
}

function BulkDeletionDialog({
  users,
  device,
  onClose,
  onCreated,
}: {
  users: DeviceUser[]
  device: Device
  onClose: () => void
  onCreated: (job: UserDeletionJob) => void
}) {
  const expectedConfirmation = bulkDeletionConfirmation(users.length, device.device_id)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (reason.trim().length < 10) return setError('Record a reason of at least 10 characters.')
    if (confirmation !== expectedConfirmation) {
      return setError(`Type ${expectedConfirmation} exactly to continue.`)
    }
    if (!password) return setError('Password confirmation is required.')
    setBusy(true)
    try {
      const response = await api<{ job: UserDeletionJob }>(
        `/api/v2/devices/${device.connector_id}/user-deletion-jobs`,
        {
          method: 'POST',
          body: JSON.stringify({
            targets: users.map((user) => ({
              user_key: user.user_key,
              expected_version: user.row_version,
            })),
            reason: reason.trim(),
            typed_confirmation: confirmation,
            password,
            idempotency_key: idempotency('bulk-delete-users'),
          }),
        },
      )
      onCreated(response.job)
      onClose()
    } catch (reasonValue) {
      setError(
        reasonValue instanceof Error
          ? reasonValue.message
          : 'The bulk deletion job could not be created.',
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId="bulk-user-deletion-title"
      title={`Delete ${users.length} terminal users`}
      description="ADD will process one user at a time and stop advancing if terminal verification is unsafe."
      onClose={onClose}
      className="bulk-deletion-dialog"
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className="destructive-copy pattern-blocked">
          <Icon name="trash" />
          <div>
            <h3>{device.display_name}</h3>
            <p>{users.map((user) => `${user.display_name} (${user.user_id})`).join(' · ')}</p>
            <p>User records are removed from the ZKT. Attendance and ADD identity history remain preserved.</p>
          </div>
        </div>
        <label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button destructive" disabled={busy}>{busy ? 'Creating durable job…' : `Delete ${users.length} users safely`}</button>
        </footer>
      </form>
    </Dialog>
  )
}

function BulkDeletionProgress({
  job,
  onCancel,
}: {
  job: UserDeletionJob
  onCancel: (password: string) => Promise<void>
}) {
  const active = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(job.status)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  return (
    <section className={`command-progress bulk-deletion-progress pattern-${statusPattern(job.status)}`} aria-live="polite">
      <span className="command-symbol"><Icon name={job.status === 'SUCCEEDED' ? 'check' : ['PARTIAL', 'FAILED', 'EXPIRED'].includes(job.status) ? 'alert' : 'refresh'} /></span>
      <div>
        <p className="eyebrow">DURABLE BULK USER DELETION</p>
        <h3>{job.status.replaceAll('_', ' ')}</h3>
        <p>
          {job.counts.succeeded} verified deleted · {job.counts.pending} pending ·{' '}
          {job.counts.failed} failed · {job.counts.canceled} canceled · {job.counts.expired} expired
        </p>
        {job.items.find((item) => item.error_message)?.error_message && (
          <small>{job.items.find((item) => item.error_message)?.error_message}</small>
        )}
      </div>
      {active && job.status !== 'CANCEL_REQUESTED' && (
        <div className="bulk-cancel">
          {!cancelOpen ? (
            <button className="button secondary" onClick={() => setCancelOpen(true)}>Cancel untouched users</button>
          ) : (
            <>
              <input aria-label="Administrator password to cancel" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Administrator password" />
              <button
                className="button destructive"
                disabled={busy || !password}
                onClick={async () => {
                  setBusy(true)
                  try {
                    await onCancel(password)
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                {busy ? 'Canceling…' : 'Confirm cancel'}
              </button>
            </>
          )}
        </div>
      )}
    </section>
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
  const [conflictReport, setConflictReport] = useState<IdentityConflictReport | null>(null)
  const [resolutionDialog, setResolutionDialog] = useState<IdentityResolutionDialogState>(null)
  const [historicalReport, setHistoricalReport] = useState<HistoricalIdentityReport | null>(null)
  const [historicalDialog, setHistoricalDialog] = useState<HistoricalIdentityDialogState>(null)
  const [selectedUserKeys, setSelectedUserKeys] = useState<Set<string>>(new Set())
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false)
  const [deletionJob, setDeletionJob] = useState<UserDeletionJob | null>(null)

  const load = useCallback(async () => {
    if (!selected) {
      setRows([])
      setIntegrity(null)
      setConflictReport(null)
      setHistoricalReport(null)
      return
    }
    setLoading(true)
    try {
      const compact = query.replace(/\D/g, '')
      const cnicSearch = compact.length === 13 && compact === query.replace(/[\s-]/g, '')
      const [result, conflicts, history, latestJob] = await Promise.all([
        api<{
          rows: DeviceUser[]
          identity_integrity: IdentityIntegrity
        }>(
          `/api/v2/devices/${selected.connector_id}/users${queryString({
            q: cnicSearch ? undefined : query,
            cnic: cnicSearch ? compact : undefined,
            identity: identity === 'ALL' ? undefined : identity,
            privilege: role === 'ALL' ? undefined : role,
          })}`,
        ),
        api<IdentityConflictReport>(
          `/api/v2/devices/${selected.connector_id}/identity-conflicts`,
        ),
        api<HistoricalIdentityReport>(
          `/api/v2/devices/${selected.connector_id}/historical-identities`,
        ),
        api<{ job: UserDeletionJob | null }>(
          `/api/v2/devices/${selected.connector_id}/user-deletion-jobs/latest`,
        ),
      ])
      setRows(result.rows)
      setSelectedUserKeys((current) => {
        const available = new Set(result.rows.map((user) => user.user_key))
        return new Set([...current].filter((key) => available.has(key)))
      })
      setIntegrity(result.identity_integrity || null)
      setConflictReport(conflicts)
      setHistoricalReport(history?.totals ? history : null)
      setDeletionJob(latestJob.job)
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
    setSelectedUserKeys(new Set())
    setBulkDialogOpen(false)
    setHistoricalDialog(null)
  }, [selectedDeviceId])

  useEffect(() => {
    if (!deletionJob || !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(deletionJob.status)) {
      return
    }
    const timeout = window.setTimeout(async () => {
      try {
        const response = await api<{ job: UserDeletionJob }>(
          `/api/v2/user-deletion-jobs/${deletionJob.job_id}`,
        )
        const finished = !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(response.job.status)
        setDeletionJob(response.job)
        if (finished) {
          setSelectedUserKeys(new Set())
          await Promise.all([load(), refreshFleet()])
          if (response.job.status === 'SUCCEEDED') {
            toast.notice('Every selected user was deleted and verified; attendance was preserved.')
          } else {
            toast.error(`Bulk deletion ended as ${response.job.status}. Review the per-user result.`)
          }
        }
      } catch (reason) {
        toast.error(reason instanceof Error ? reason.message : 'Unable to refresh deletion progress.')
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [deletionJob, load, refreshFleet, toast.error, toast.notice])

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
  const deleteWritable = Boolean(writable && selected?.zkt?.capabilities.delete_user)
  const eligibleRows = rows.filter(
    (user) =>
      user.privilege !== 14 &&
      !user.read_only &&
      !user.current_command_state,
  )
  const selectedUsers = eligibleRows.filter((user) => selectedUserKeys.has(user.user_key))
  const activeDeletionJob = Boolean(
    deletionJob && ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(deletionJob.status),
  )
  const toggleUser = (userKey: string, checked: boolean) => {
    setSelectedUserKeys((current) => {
      const next = new Set(current)
      if (checked) next.add(userKey)
      else next.delete(userKey)
      return next
    })
  }
  const cancelDeletionJob = async (password: string) => {
    if (!deletionJob) return
    try {
      const response = await api<{ job: UserDeletionJob }>(
        `/api/v2/user-deletion-jobs/${deletionJob.job_id}/cancel`,
        { method: 'POST', body: JSON.stringify({ password }) },
      )
      setDeletionJob(response.job)
      toast.notice('Cancellation recorded. Any running user will finish verification; untouched users will be skipped.')
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to cancel deletion job.')
      throw reason
    }
  }

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
          {integrity && integrity.unresolved_duplicate_users > 0 && (
            <div className="capability-banner pattern-blocked" role="status">
              <Icon name="alert" />
              <div>
                <strong>Exact CNIC conflicts are present in the terminal data.</strong>
                <span>
                  The latest {integrity.source === 'CURRENT_COMPLETE_ZKT_SNAPSHOT' ? 'complete ' : ''}
                  ZKT snapshot reports {integrity.unresolved_duplicate_users} unresolved users across{' '}
                  {integrity.unresolved_duplicate_groups} exact-CNIC groups. Matching terminal user IDs are
                  quarantined until corrected or explicitly verified as the same employee.
                </span>
              </div>
              <StatusBadge state="CORRECTION REQUIRED" />
            </div>
          )}
          {command && <CommandProgress command={command} onCancel={cancel} />}
          {deletionJob && <BulkDeletionProgress job={deletionJob} onCancel={cancelDeletionJob} />}
          {historicalReport && historicalReport.totals.unresolved_events > 0 && (
            <section className="panel conflict-workbench" aria-label="Historical identity backlog">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">PRESERVED ATTENDANCE · IDENTITY REQUIRED</p>
                  <h2>Historical identity backlog</h2>
                  <p>
                    {historicalReport.totals.unresolved_events.toLocaleString()} events remain fail-closed:
                    {' '}{historicalReport.totals.blocked_identity.toLocaleString()} missing verified identity and
                    {' '}{historicalReport.totals.quarantined_identity_reuse.toLocaleString()} quarantined for identity reuse.
                    {' '}{historicalReport.totals.unassigned_events.toLocaleString()} are not linked to one deleted terminal user;
                    {' '}{(historicalReport.totals.actionable_event_groups ?? 0).toLocaleString()} exact cohorts can accept guarded HR evidence.
                  </p>
                </div>
                <StatusBadge state="HR EVIDENCE REQUIRED" />
              </div>
              <div className="conflict-groups">
                {[...historicalReport.rows, ...(historicalReport.unassigned_groups ?? [])].map((candidate) => (
                  <article key={candidate.source_user_key || candidate.group_token} className="conflict-group pattern-blocked">
                    <div className="conflict-group-heading">
                      <div>
                        <strong>{candidate.display_name}</strong>
                        <small>
                          User {candidate.user_id} · UID {candidate.uid || 'missing'}
                          {candidate.row_version == null ? ' · exact event cohort' : ` · version ${candidate.row_version}`}
                        </small>
                      </div>
                      <StatusBadge state={candidate.resolution_path.replaceAll('_', ' ')} />
                    </div>
                    <div className="conflict-members">
                      <div>
                        <span>
                          <strong>{candidate.event_count.toLocaleString()} preserved events</strong>
                          <small>{candidate.blocked_count.toLocaleString()} blocked · {candidate.quarantined_count.toLocaleString()} quarantined</small>
                        </span>
                        <span>
                          <strong>{dateTime(candidate.first_event_at)}</strong>
                          <small>through {dateTime(candidate.last_event_at)}</small>
                        </span>
                      </div>
                    </div>
                    <div className="conflict-group-actions">
                      <small>
                        {candidate.operator_actionable
                          ? 'Requires exact authoritative HR CNIC, employee ID, service number, name, and audit approval.'
                          : candidate.source_kind === 'EVENT_GROUP'
                            ? 'This cohort remains fail-closed because it lacks a unique UID, has conflicting terminal names, or is linked to another identity.'
                            : 'This row remains fail-closed until its identity conflict or reuse review is resolved.'}
                      </small>
                      <button
                        className="button secondary"
                        disabled={!candidate.operator_actionable}
                        onClick={() => setHistoricalDialog({ candidate })}
                      >
                        <Icon name="shield" />
                        Enter verified HR evidence
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          )}
          {conflictReport && conflictReport.raw_duplicate_groups > 0 && (
            <section className="panel conflict-workbench" aria-label="Identity conflict review">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">REVERSIBLE IDENTITY REVIEW</p>
                  <h2>Exact-CNIC terminal groups</h2>
                  <p>
                    {conflictReport.resolved_groups} verified · {conflictReport.unresolved_groups} awaiting review.
                    ADD has {conflictReport.evidence_scope.add_attendance_count.toLocaleString()} of the terminal’s{' '}
                    {conflictReport.evidence_scope.terminal_attendance_count.toLocaleString()} punches
                    {conflictReport.evidence_scope.attendance_coverage_percent == null
                      ? ''
                      : ` (${conflictReport.evidence_scope.attendance_coverage_percent}% evidence coverage)`}.
                  </p>
                </div>
                <StatusBadge state={conflictReport.unresolved_groups ? 'REVIEW REQUIRED' : 'RESOLVED'} />
              </div>
              <div className="conflict-groups">
                {conflictReport.groups.map((group) => (
                  <article key={group.group_token} className={`conflict-group pattern-${group.status === 'UNRESOLVED' ? 'blocked' : 'confirmed'}`}>
                    <div className="conflict-group-heading">
                      <div><strong>{group.cnic_masked || 'Masked CNIC'}</strong><small>{group.classification.replaceAll('_', ' ')}</small></div>
                      <StatusBadge state={group.status} />
                    </div>
                    <div className="conflict-members">
                      {group.members.map((member) => (
                        <div key={member.user_key}>
                          <span><strong>{member.display_name}</strong><small>User {member.user_id} · UID {member.uid}</small></span>
                          <span><strong>{member.punch_evidence.captured_count.toLocaleString()} captured</strong><small>{member.punch_evidence.last_captured_at ? `Last ${dateTime(member.punch_evidence.last_captured_at)}` : 'No punches in ADD evidence window'}</small></span>
                        </div>
                      ))}
                    </div>
                    <div className="conflict-group-actions">
                      <small>{group.status === 'UNRESOLVED' ? group.recommended_action.replaceAll('_', ' ') : group.resolution_reason}</small>
                      <button
                        className={`button ${group.status === 'UNRESOLVED' ? 'secondary' : 'text-button'}`}
                        onClick={() => setResolutionDialog({ mode: group.status === 'UNRESOLVED' ? 'resolve' : 'revoke', group })}
                      >
                        <Icon name={group.status === 'UNRESOLVED' ? 'shield' : 'alert'} />
                        {group.status === 'UNRESOLVED' ? 'Review same-employee alias' : 'Revoke resolution'}
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          )}
          <section className="panel">
            <div className="toolbar user-toolbar">
              <label className="search-field"><span className="sr-only">Search users</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, user ID, or exact CNIC" /></label>
              <label><span className="sr-only">Identity completeness</span><select value={identity} onChange={(event) => setIdentity(event.target.value)}><option value="ALL">All identities</option><option value="COMPLETE">CNIC complete</option><option value="MISSING">CNIC missing or unresolved</option><option value="CONFLICT">CNIC conflict</option><option value="RESOLVED_ALIAS">Verified aliases</option></select></label>
              <label><span className="sr-only">Role</span><select value={role} onChange={(event) => setRole(event.target.value)}><option value="ALL">All roles</option><option value="0">Regular users</option><option value="14">Administrators</option></select></label>
              <button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh users</button>
              <label className="check-field bulk-select-all">
                <input
                  type="checkbox"
                  checked={eligibleRows.length > 0 && selectedUsers.length === eligibleRows.length}
                  disabled={!deleteWritable || activeDeletionJob || !eligibleRows.length}
                  onChange={(event) =>
                    setSelectedUserKeys(
                      event.target.checked
                        ? new Set(eligibleRows.map((user) => user.user_key))
                        : new Set(),
                    )
                  }
                />
                <span><strong>Select eligible</strong><small>{selectedUsers.length} selected</small></span>
              </label>
              <button
                className="button destructive"
                disabled={!deleteWritable || activeDeletionJob || !selectedUsers.length}
                onClick={() => setBulkDialogOpen(true)}
              >
                <Icon name="trash" /> Delete selected ({selectedUsers.length})
              </button>
            </div>
            <div className="user-table" aria-busy={loading}>
              <div className="user-table-head"><span>Identity</span><span>Terminal record</span><span>Role & shift</span><span>Last sync</span><span>Actions</span></div>
              {loading && <div className="empty-state compact"><Icon name="refresh" /><p>Reading the selected terminal user view…</p></div>}
              {!loading && rows.map((user) => (
                <article key={user.user_key} className={`user-row ${user.identity_conflict_resolved ? 'identity-resolved' : user.identity_conflict_code ? 'identity-conflict' : user.identity_complete ? '' : 'identity-missing'}`}>
                  <div className="user-person">
                    <input
                      className="user-select"
                      type="checkbox"
                      aria-label={`Select ${user.display_name} for bulk deletion`}
                      checked={selectedUserKeys.has(user.user_key)}
                      disabled={!deleteWritable || activeDeletionJob || user.privilege === 14 || user.read_only || Boolean(user.current_command_state)}
                      onChange={(event) => toggleUser(user.user_key, event.target.checked)}
                    />
                    <span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>{user.identity_conflict_code ? identityConflictText(user) : user.cnic_masked || 'CNIC missing · punches blocked until enriched'}</small></span>
                  </div>
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
      {bulkDialogOpen && selected && selectedUsers.length > 0 && (
        <BulkDeletionDialog
          users={selectedUsers}
          device={selected}
          onClose={() => setBulkDialogOpen(false)}
          onCreated={(job) => {
            setDeletionJob(job)
            toast.notice('Durable bulk deletion job created. ADD will verify one user at a time.')
          }}
        />
      )}
      {resolutionDialog && selected && <IdentityResolutionDialog state={resolutionDialog} device={selected} onClose={() => setResolutionDialog(null)} onComplete={(report) => { setConflictReport(report); void load() }} toast={toast} />}
      {historicalDialog && selected && (
        <HistoricalIdentityResolutionDialog
          state={historicalDialog}
          device={selected}
          onClose={() => setHistoricalDialog(null)}
          onComplete={async () => {
            await Promise.all([load(), refreshFleet()])
          }}
          toast={toast}
        />
      )}
    </>
  )
}

type FirmwareListResponse<T> = {
  rows: T[]
  enabled: boolean
  hil_enabled: boolean
}

function FirmwareCampaignControls({
  campaign,
  onChanged,
  toast,
}: {
  campaign: FirmwareCampaign
  onChanged: () => Promise<void>
  toast: ReturnType<typeof useToast>
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const actionAllowed = reason.trim().length >= 10 && Boolean(password) && !busy

  const control = async (action: 'pause' | 'resume' | 'cancel') => {
    setBusy(true)
    try {
      await api(`/api/v1/firmware/campaigns/${campaign.campaign_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), password }),
      })
      setPassword('')
      setReason('')
      toast.notice(`Firmware campaign ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'} with an audit entry.`)
      await onChanged()
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error ? error.message : 'Campaign control failed.')
    } finally {
      setBusy(false)
    }
  }

  if (!['ACTIVE', 'PAUSED'].includes(campaign.status)) return null
  return (
    <div className="firmware-controls">
      <label>
        Control reason
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={200}
          placeholder="10–200 characters"
        />
      </label>
      <label>
        Administrator password
        <input
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>
      <div className="firmware-control-actions">
        {campaign.status === 'ACTIVE' && (
          <button className="button secondary" disabled={!actionAllowed} onClick={() => void control('pause')}>
            <Icon name="pause" /> Pause
          </button>
        )}
        {campaign.status === 'PAUSED' && (
          <button className="button primary" disabled={!actionAllowed} onClick={() => void control('resume')}>
            <Icon name="refresh" /> Resume
          </button>
        )}
        <button className="button destructive" disabled={!actionAllowed} onClick={() => void control('cancel')}>
          <Icon name="x" /> Cancel campaign
        </button>
      </div>
    </div>
  )
}

function FirmwareView({
  devices,
  revision,
  toast,
}: {
  devices: Device[]
  revision: number
  toast: ReturnType<typeof useToast>
}) {
  const [releases, setReleases] = useState<FirmwareRelease[]>([])
  const [campaigns, setCampaigns] = useState<FirmwareCampaign[]>([])
  const [enabled, setEnabled] = useState(false)
  const [hilEnabled, setHilEnabled] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [releaseId, setReleaseId] = useState('')
  const [zoneId, setZoneId] = useState('')
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')

  const zones = useMemo(
    () => [...new Set(devices.map((device) => device.zone_id).filter(Boolean))].sort(),
    [devices],
  )
  const selectedRelease = releases.find((release) => release.release_id === releaseId) || null
  const hilTargetDevice = selectedRelease?.hil_target_mac
    ? devices.find(
        (device) =>
          device.hardware_id.toLowerCase() === selectedRelease.hil_target_mac?.toLowerCase(),
      ) || null
    : null
  const isHilRelease = selectedRelease?.state === 'HIL_ONLY'
  const selectedZoneMatchesHil = !isHilRelease || Boolean(
    hilTargetDevice && zoneId === hilTargetDevice.zone_id,
  )
  const releaseChannelEnabled = isHilRelease ? hilEnabled : enabled
  const startAllowed = Boolean(
    selectedRelease &&
      selectedRelease.state !== 'REVOKED' &&
      releaseChannelEnabled &&
      selectedZoneMatchesHil &&
      zoneId &&
      reason.trim().length >= 10 &&
      confirmation === selectedRelease.version &&
      password &&
      !busy,
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [releaseResponse, campaignResponse] = await Promise.all([
        api<FirmwareListResponse<FirmwareRelease>>('/api/v1/firmware/releases'),
        api<FirmwareListResponse<FirmwareCampaign>>('/api/v1/firmware/campaigns'),
      ])
      setReleases(releaseResponse.rows)
      setCampaigns(campaignResponse.rows)
      setEnabled(releaseResponse.enabled)
      setHilEnabled(releaseResponse.hil_enabled)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Unable to load firmware operations.')
    } finally {
      setLoading(false)
    }
  }, [toast.error])

  useEffect(() => { void load() }, [load, revision])

  const selectRelease = (nextReleaseId: string) => {
    setReleaseId(nextReleaseId)
    setConfirmation('')
    const nextRelease = releases.find((release) => release.release_id === nextReleaseId)
    if (nextRelease?.state === 'HIL_ONLY' && nextRelease.hil_target_mac) {
      const target = devices.find(
        (device) => device.hardware_id.toLowerCase() === nextRelease.hil_target_mac?.toLowerCase(),
      )
      setZoneId(target?.zone_id || '')
    } else {
      setZoneId('')
    }
  }

  const start = async (event: FormEvent) => {
    event.preventDefault()
    if (!selectedRelease || !startAllowed) return
    setBusy(true)
    try {
      const result = await api<{ campaign_id: string; status: string; eligible: number; legacy_skipped: number }>(
        '/api/v1/firmware/campaigns',
        {
          method: 'POST',
          body: JSON.stringify({
            release_id: selectedRelease.release_id,
            zone_id: zoneId,
            reason: reason.trim(),
            typed_confirmation: confirmation,
            password,
          }),
        },
      )
      setPassword('')
      setConfirmation('')
      setReason('')
      toast.notice(`Firmware campaign created for ${result.eligible} eligible device${result.eligible === 1 ? '' : 's'}; ${result.legacy_skipped} skipped.`)
      await load()
    } catch (error) {
      setPassword('')
      setConfirmation('')
      toast.error(error instanceof Error ? error.message : 'Firmware campaign could not be created.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="SIGNED OTA CONTROL PLANE"
        title="Firmware releases and campaigns"
        description="Inspect signed artifacts, enforce HIL quarantine, and operate audited zone-scoped rollouts."
        action={<button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh</button>}
      />
      <div className="firmware-mode-grid">
        <article className={`firmware-mode pattern-${enabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="shield" />
          <span><strong>National OTA</strong><small>{enabled ? 'Enabled for AVAILABLE releases' : 'Disabled'}</small></span>
        </article>
        <article className={`firmware-mode pattern-${hilEnabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="terminal" />
          <span><strong>Quarantined HIL OTA</strong><small>{hilEnabled ? 'Enabled for exact target MAC only' : 'Disabled'}</small></span>
        </article>
      </div>

      <section className="panel firmware-start-panel">
        <div className="panel-header">
          <div><h2>Start a constrained campaign</h2><p>Every start requires an exact release, zone, reason, typed version, and administrator step-up.</p></div>
          <StatusBadge state={isHilRelease ? 'HIL_ONLY' : selectedRelease?.state || 'NO RELEASE SELECTED'} />
        </div>
        <form className="firmware-start-form" onSubmit={(event) => void start(event)}>
          <label>
            Signed release
            <select value={releaseId} onChange={(event) => selectRelease(event.target.value)}>
              <option value="">Select a release</option>
              {releases.map((release) => (
                <option key={release.release_id} value={release.release_id} disabled={release.state === 'REVOKED'}>
                  {release.version} · {release.state}
                </option>
              ))}
            </select>
          </label>
          <label>
            Zone
            <select
              value={zoneId}
              disabled={isHilRelease}
              onChange={(event) => setZoneId(event.target.value)}
            >
              <option value="">Select a zone</option>
              {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
            </select>
          </label>
          <label className="firmware-reason">
            Audited reason
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="At least 10 characters"
            />
          </label>
          <label>
            Type release version to confirm
            <input
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder={selectedRelease?.version || 'Select a release first'}
            />
          </label>
          <label>
            Administrator password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {isHilRelease && (
            <div className={`firmware-quarantine pattern-${hilTargetDevice ? 'confirmed' : 'blocked'}`}>
              <Icon name={hilTargetDevice ? 'shield' : 'alert'} />
              <div>
                <strong>HIL quarantine is immutable for this release</strong>
                <p>
                  Only <code>{selectedRelease?.hil_target_mac || 'missing target MAC'}</code>
                  {hilTargetDevice ? ` in ${hilTargetDevice.zone_id}` : ' matches no registered device'} is eligible.
                </p>
              </div>
            </div>
          )}
          <button className="button primary firmware-start-button" type="submit" disabled={!startAllowed}>
            <Icon name="power" /> {busy ? 'Starting audited campaign…' : 'Start firmware campaign'}
          </button>
        </form>
      </section>

      <section className="panel firmware-section">
        <div className="panel-header"><div><h2>Signed release inventory</h2><p>Cryptographic identity and publication state from the server-side release store.</p></div></div>
        <div className="firmware-release-list" aria-busy={loading}>
          {releases.map((release) => (
            <article key={release.release_id} className="firmware-release">
              <div className="firmware-release-title">
                <div><p className="eyebrow">ZONE LITE {release.version}</p><h3>{release.release_id}</h3></div>
                <StatusBadge state={release.state} />
              </div>
              <dl>
                <div><dt>Git commit</dt><dd><code>{release.git_sha}</code></dd></div>
                <div><dt>Image SHA-256</dt><dd><code>{release.image_sha256}</code></dd></div>
                <div><dt>Application SHA-256</dt><dd><code>{release.application_sha256 || '—'}</code></dd></div>
                <div><dt>Signing key</dt><dd><code>{release.signing_key_id}</code></dd></div>
                <div><dt>Partition layout</dt><dd>{release.partition_layout}</dd></div>
                <div><dt>Image size</dt><dd>{new Intl.NumberFormat('en-PK').format(release.image_size)} bytes</dd></div>
                <div><dt>Published</dt><dd>{dateTime(release.published_at)}</dd></div>
                <div><dt>HIL target</dt><dd><code>{release.hil_target_mac || 'Not quarantined'}</code></dd></div>
              </dl>
            </article>
          ))}
          {!loading && !releases.length && <div className="empty-state"><Icon name="terminal" /><h3>No signed firmware releases are published.</h3></div>}
        </div>
      </section>

      <section className="panel firmware-section">
        <div className="panel-header"><div><h2>Campaign history and controls</h2><p>Deployment counts are durable server state; pause, resume, and cancel require step-up authentication.</p></div></div>
        <div className="firmware-campaign-list" aria-busy={loading}>
          {campaigns.map((campaign) => (
            <article key={campaign.campaign_id} className="firmware-campaign">
              <div className="firmware-campaign-summary">
                <div><p className="eyebrow">{campaign.zone_id}</p><h3>{campaign.version || 'Unknown release'}</h3><code>{campaign.campaign_id}</code></div>
                <StatusBadge state={campaign.status} />
                <div><strong>{campaign.eligible}</strong><small>Eligible · {campaign.legacy_skipped} skipped</small></div>
                <div><strong>{Object.entries(campaign.counts).map(([state, count]) => `${state.replaceAll('_', ' ')} ${count}`).join(' · ') || 'No offers yet'}</strong><small>Created {dateTime(campaign.created_at)}</small></div>
              </div>
              {campaign.pause_reason && <p className="firmware-pause-reason"><Icon name="alert" /> {campaign.pause_reason}</p>}
              {campaign.deployments?.map((deployment) => (
                <details className="firmware-deployment-evidence" key={deployment.deployment_id}>
                  <summary>
                    {deployment.connector_id || 'Unknown connector'} · {deployment.status.replaceAll('_', ' ')}
                    {deployment.error_code ? ` · ${deployment.error_code}` : ''}
                  </summary>
                  <dl>
                    <div><dt>Deployment</dt><dd><code>{deployment.deployment_id}</code></dd></div>
                    <div><dt>Version</dt><dd>{deployment.previous_version || 'Unknown'} → {deployment.target_version}</dd></div>
                    <div><dt>Bytes</dt><dd>{new Intl.NumberFormat('en-PK').format(deployment.bytes_written)}</dd></div>
                    <div><dt>Attempts</dt><dd>{deployment.attempt_count}</dd></div>
                  </dl>
                  {deployment.events.length > 0 && (
                    <ol className="firmware-deployment-events">
                      {deployment.events.map((event, index) => (
                        <li key={`${event.created_at}-${index}`}>
                          <time>{dateTime(event.created_at)}</time>
                          <strong>{event.state.replaceAll('_', ' ')}</strong>
                          {event.details.error_code ? <code>{String(event.details.error_code)}</code> : null}
                        </li>
                      ))}
                    </ol>
                  )}
                </details>
              ))}
              <FirmwareCampaignControls campaign={campaign} onChanged={load} toast={toast} />
            </article>
          ))}
          {!loading && !campaigns.length && <div className="empty-state"><Icon name="shield" /><h3>No firmware campaigns have been started.</h3></div>}
        </div>
      </section>
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
          {!loading && rows.map((row) => <article key={row.event_uid}><div><strong>{row.display_name || 'Unknown identity'}</strong><small>{row.cnic_masked || `User ${row.user_id}`}</small></div><div><strong>{dateTime(row.device_event_time)}</strong><small>Received {relativeTime(row.received_at)}</small></div><div><strong>{row.device_serial || 'Unreported serial'}</strong><small>UID {row.uid || '—'} · User {row.user_id}</small></div><div><StatusBadge state={row.source} /><small>Punch {row.punch ?? '—'} · {row.clock_quality}</small></div><div><StatusBadge state={row.ords_status} /><small>{row.oracle_confirmed_at ? `Oracle confirmed ${relativeTime(row.oracle_confirmed_at)} via ${(row.oracle_confirmation_path || 'unknown path').replaceAll('_', ' ').toLowerCase()}` : row.clock_drift_seconds == null ? 'No Oracle confirmation yet' : `${Math.round(row.clock_drift_seconds)}s clock drift`}</small></div></article>)}
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
    const handleSessionExpired = () => {
      setCsrfToken('')
      setAuthState('anonymous')
      setDevices([])
      setDrawer(null)
    }
    window.addEventListener('add:session-expired', handleSessionExpired)
    return () => window.removeEventListener('add:session-expired', handleSessionExpired)
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
    { id: 'firmware' as const, label: 'Firmware', icon: 'terminal' as const },
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
        {view === 'firmware' && <FirmwareView devices={devices} revision={revision} toast={toast} />}
        {view === 'alerts' && <AlertsView devices={devices} toast={toast} revision={revision} />}
      </main>
      {drawer && <DeviceDrawer seed={drawer} revision={revision} onClose={() => setDrawer(null)} onManageUsers={manageUsers} toast={toast} />}
      {toast.toast && <div className={`toast pattern-${toast.toast.kind === 'error' ? 'blocked' : 'confirmed'}`} role="status" aria-live="polite"><Icon name={toast.toast.kind === 'error' ? 'alert' : 'check'} />{toast.toast.text}</div>}
    </div>
  )
}
