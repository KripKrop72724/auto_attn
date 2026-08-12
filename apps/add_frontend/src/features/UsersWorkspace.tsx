import {
  useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent,
} from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import './UsersWorkspace.css'
import { api, queryString } from '../api'
import {
  CommandProgress, Dialog, Metric, PageHeader, StatusBadge, dateTime,
  identityConflictText, relativeTime, statusPattern, terminalCommandStates, useToast,
  type HistoricalIdentityDialogState, type IdentityResolutionDialogState, type UserDialogState,
} from '../App'
import { Icon } from '../Icon'
import type {
  Command, Device, DeviceUser, HistoricalIdentityReport, IdentityConflictReport,
  IdentityIntegrity, UserDeletionJob,
} from '../types'
import {
  BulkDeletionDialog, BulkDeletionProgress, HistoricalIdentityResolutionDialog,
  IdentityResolutionDialog, UserOperationDialog,
} from './Users'

type UsersSection = 'directory' | 'identity' | 'history'
type UsersDirectoryResponse = {
  rows: DeviceUser[]
  next_cursor: number | null
  device?: Device
  identity_integrity?: IdentityIntegrity
}
type DiagnosticKey = 'device' | 'identity' | 'history' | 'deletion'

const userSections: Array<{ id: UsersSection; label: string; icon: 'users' | 'alert' | 'shield' }> = [
  { id: 'directory', label: 'Directory', icon: 'users' },
  { id: 'identity', label: 'Identity Review', icon: 'alert' },
  { id: 'history', label: 'Historical Backlog', icon: 'shield' },
]

const requestError = (reason: unknown, fallback: string) =>
  reason instanceof Error ? reason.message : fallback

function TerminalPicker({
  devices,
  selectedDeviceId,
  open,
  onOpenChange,
  onSelect,
}: {
  devices: Device[]
  selectedDeviceId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (id: string) => void
}) {
  const [query, setQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const resultsRef = useRef<HTMLDivElement>(null)
  const selected = devices.find((device) => device.connector_id === selectedDeviceId)
  const shown = devices.filter((device) =>
    `${device.display_name} ${device.zone_name} ${device.zone_id} ${device.zkt?.serial || ''}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  )

  useEffect(() => {
    if (open) window.setTimeout(() => searchRef.current?.focus(), 0)
    else setQuery('')
  }, [open])

  const closePicker = () => {
    onOpenChange(false)
    window.setTimeout(() => triggerRef.current?.focus(), 0)
  }

  const moveOptionFocus = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const options = Array.from(resultsRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') || [])
    let target = index
    if (event.key === 'ArrowDown') target = Math.min(options.length - 1, index + 1)
    else if (event.key === 'ArrowUp') target = Math.max(0, index - 1)
    else if (event.key === 'Home') target = 0
    else if (event.key === 'End') target = options.length - 1
    else if (event.key === 'Escape') { event.preventDefault(); closePicker(); return }
    else return
    event.preventDefault()
    options[target]?.focus()
  }

  return (
    <section className={`terminal-picker panel ${open ? 'is-open' : ''}`} aria-label="Selected terminal">
      <div className="terminal-picker-current">
        <span className="terminal-picker-icon"><Icon name="server" /></span>
        <div>
          <small>Selected terminal</small>
          <strong>{selected?.display_name || 'Choose a terminal'}</strong>
          <span>{selected ? `${selected.zone_name} · ${selected.zkt?.serial || 'serial pending'}` : 'Search the authorized national fleet'}</span>
        </div>
        {selected && <StatusBadge state={selected.state} live={selected.connected} />}
        <button ref={triggerRef} className="button secondary" type="button" aria-expanded={open} onClick={() => onOpenChange(!open)}><Icon name="search" /> {selected ? 'Change terminal' : 'Select terminal'}</button>
      </div>
      {open && (
        <div className="terminal-picker-popover">
          <label className="search-field"><span className="sr-only">Search terminals</span><Icon name="search" /><input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'ArrowDown') { event.preventDefault(); resultsRef.current?.querySelector<HTMLButtonElement>('[role="option"]')?.focus() } else if (event.key === 'Escape') closePicker() }} placeholder="Search terminal, zone, or serial" /></label>
          <div ref={resultsRef} className="terminal-picker-results" role="listbox" aria-label="Authorized terminals">
            {shown.map((device, index) => (
              <button
                key={device.connector_id}
                type="button"
                role="option"
                aria-selected={device.connector_id === selectedDeviceId}
                onKeyDown={(event) => moveOptionFocus(event, index)}
                onClick={() => { onSelect(device.connector_id); closePicker() }}
              >
                <span className="terminal-result-symbol"><Icon name="server" /></span>
                <span><strong>{device.display_name}</strong><small>{device.zone_name} · {device.zkt?.serial || 'serial pending'}</small></span>
                <span><StatusBadge state={device.state} /><small>{device.zkt?.user_count ?? '—'} users · {relativeTime(device.last_seen_at)}</small></span>
                <Icon name="chevron" />
              </button>
            ))}
            {!shown.length && <div className="empty-state compact"><Icon name="search" /><p>No authorized terminals match this search.</p></div>}
          </div>
        </div>
      )}
    </section>
  )
}

function UserActionMenu({
  user,
  canEdit,
  canLease,
  canDelete,
  editReason,
  leaseReason,
  deleteReason,
  onEdit,
  onLease,
  onDelete,
}: {
  user: DeviceUser
  canEdit: boolean
  canLease: boolean
  canDelete: boolean
  editReason: string
  leaseReason: string
  deleteReason: string
  onEdit: () => void
  onLease: () => void
  onDelete: () => void
}) {
  const menuRef = useRef<HTMLDetailsElement>(null)
  const summaryRef = useRef<HTMLElement>(null)
  const runAction = (action: () => void) => {
    if (menuRef.current) menuRef.current.open = false
    summaryRef.current?.focus()
    action()
  }
  return (
    <div className="user-row-actions">
      <button className="button secondary user-edit-action" type="button" aria-label={`Edit ${user.display_name}`} disabled={!canEdit} title={!canEdit ? editReason : undefined} onClick={onEdit}><Icon name="edit" /> Edit</button>
      <details ref={menuRef} className="user-action-menu" onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); if (menuRef.current) menuRef.current.open = false; summaryRef.current?.focus() } }}>
        <summary ref={summaryRef} aria-label={`More actions for ${user.display_name}`}><Icon name="menu" /> More</summary>
        <div role="group" aria-label={`Actions for ${user.display_name}`}>
          <button type="button" aria-label={`Enrollment access for ${user.display_name}`} disabled={!canLease} onClick={() => runAction(onLease)}><Icon name="shield" /><span><strong>Enrollment access</strong><small>{canLease ? 'Grant a 10-minute administrator lease' : leaseReason}</small></span></button>
          <button type="button" className="danger-action" aria-label={`Delete ${user.display_name}`} disabled={!canDelete} onClick={() => runAction(onDelete)}><Icon name="trash" /><span><strong>Delete user</strong><small>{canDelete ? 'Preserves attendance and identity history' : deleteReason}</small></span></button>
        </div>
      </details>
    </div>
  )
}

function LeaseRevokeDialog({
  device,
  onClose,
  onCommand,
  toast,
}: {
  device: Device
  onClose: () => void
  onCommand: (command: Command) => void
  toast: ReturnType<typeof useToast>
}) {
  const lease = device.active_lease
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!lease) return null
  const revoke = async () => {
    setBusy(true)
    setError('')
    try {
      const response = await api<{ command: Command }>(`/api/v1/admin-leases/${lease.lease_id}/revoke`, { method: 'POST', body: '{}' })
      onCommand(response.command)
      toast.notice('Enrollment access revocation is queued and will be verified on the terminal.')
      onClose()
    } catch (reason) {
      setError(requestError(reason, 'Enrollment access could not be revoked.'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog titleId="revoke-enrollment-access-title" title="Revoke enrollment access" description="End the temporary administrator lease before its automatic expiry." onClose={onClose}>
      <div className="dialog-body">
        <div className="destructive-copy pattern-blocked"><Icon name="shield" /><div><h3>{device.display_name}</h3><p>Lease state {lease.state.replaceAll('_', ' ')} · expires {relativeTime(lease.expires_at)}</p><p>The terminal will verify that the user returned to regular access.</p></div></div>
        <label>Type “REVOKE ACCESS”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions"><button className="button secondary" type="button" onClick={onClose}>Cancel</button><button className="button destructive" type="button" disabled={busy || confirmation !== 'REVOKE ACCESS'} onClick={() => void revoke()}>{busy ? 'Queuing revocation…' : 'Revoke access'}</button></footer>
      </div>
    </Dialog>
  )
}

export function UsersView({
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
  const selectedFromFleet = devices.find((device) => device.connector_id === selectedDeviceId)
  const [deviceDetail, setDeviceDetail] = useState<Device | null>(null)
  const selected = deviceDetail?.connector_id === selectedDeviceId ? deviceDetail : selectedFromFleet
  const [section, setSection] = useState<UsersSection>('directory')
  const [pickerOpen, setPickerOpen] = useState(!selectedDeviceId)
  const [rows, setRows] = useState<DeviceUser[]>([])
  const [integrity, setIntegrity] = useState<IdentityIntegrity | null>(null)
  const [conflictReport, setConflictReport] = useState<IdentityConflictReport | null>(null)
  const [historicalReport, setHistoricalReport] = useState<HistoricalIdentityReport | null>(null)
  const [deletionJob, setDeletionJob] = useState<UserDeletionJob | null>(null)
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [cnicQuery, setCnicQuery] = useState('')
  const [identity, setIdentity] = useState('ALL')
  const [role, setRole] = useState('ALL')
  const [loadingDirectory, setLoadingDirectory] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadingDiagnostics, setLoadingDiagnostics] = useState(false)
  const [directoryError, setDirectoryError] = useState('')
  const [diagnosticErrors, setDiagnosticErrors] = useState<Partial<Record<DiagnosticKey, string>>>({})
  const [dialog, setDialog] = useState<UserDialogState>(null)
  const [resolutionDialog, setResolutionDialog] = useState<IdentityResolutionDialogState>(null)
  const [historicalDialog, setHistoricalDialog] = useState<HistoricalIdentityDialogState>(null)
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false)
  const [revokeLeaseOpen, setRevokeLeaseOpen] = useState(false)
  const [command, setCommand] = useState<Command | null>(null)
  const [selectedUserKeys, setSelectedUserKeys] = useState<Set<string>>(new Set())
  const [, setLeaseClock] = useState(0)
  const directoryRequest = useRef<AbortController | null>(null)
  const diagnosticsRequest = useRef<AbortController | null>(null)
  const revisionRef = useRef(revision)
  const tabsRef = useRef<HTMLDivElement>(null)
  const userTableRef = useRef<HTMLDivElement>(null)
  const userVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => userTableRef.current,
    estimateSize: () => 92,
    overscan: 10,
    enabled: rows.length > 200,
  })

  const cnicError = cnicQuery && !/^\d{13}$/.test(cnicQuery) ? 'Exact CNIC must contain 13 digits.' : ''

  const loadDirectory = useCallback(async (cursor?: number, append = false) => {
    if (!selectedDeviceId || cnicError) {
      if (!selectedDeviceId) { setRows([]); setIntegrity(null); setNextCursor(null) }
      if (cnicError) setDirectoryError(cnicError)
      return
    }
    directoryRequest.current?.abort()
    const controller = new AbortController()
    directoryRequest.current = controller
    if (append) setLoadingMore(true)
    else setLoadingDirectory(true)
    setDirectoryError('')
    try {
      const response = await api<UsersDirectoryResponse>(`/api/v2/devices/${selectedDeviceId}/users${queryString({
        q: query,
        cnic: cnicQuery,
        identity: identity === 'ALL' ? undefined : identity,
        privilege: role === 'ALL' ? undefined : role,
        cursor,
        limit: 200,
      })}`, { signal: controller.signal })
      if (controller.signal.aborted || directoryRequest.current !== controller) return
      if (append) setRows((current) => [...current, ...response.rows.filter((row) => !current.some((item) => item.user_key === row.user_key))])
      else {
        setRows(response.rows)
        setSelectedUserKeys((current) => {
          const available = new Set(response.rows.map((row) => row.user_key))
          return new Set([...current].filter((key) => available.has(key)))
        })
      }
      setNextCursor(response.next_cursor ?? null)
      if (response.identity_integrity) setIntegrity(response.identity_integrity)
      if (response.device) setDeviceDetail((current) => current?.active_lease ? current : response.device || null)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setDirectoryError(requestError(reason, 'The terminal directory could not be loaded.'))
    } finally {
      if (directoryRequest.current === controller) {
        setLoadingDirectory(false)
        setLoadingMore(false)
      }
    }
  }, [cnicError, cnicQuery, identity, query, role, selectedDeviceId])

  const loadDiagnostics = useCallback(async () => {
    if (!selectedDeviceId) return
    diagnosticsRequest.current?.abort()
    const controller = new AbortController()
    diagnosticsRequest.current = controller
    setLoadingDiagnostics(true)
    setDiagnosticErrors({})
    const setFailure = (key: DiagnosticKey, reason: unknown, fallback: string) => {
      if (controller.signal.aborted) return
      setDiagnosticErrors((current) => ({ ...current, [key]: requestError(reason, fallback) }))
    }
    const tasks = [
      api<Device>(`/api/v1/devices/${selectedDeviceId}`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setDeviceDetail(value) }).catch((reason) => setFailure('device', reason, 'Terminal status is temporarily unavailable.')),
      api<IdentityConflictReport>(`/api/v2/devices/${selectedDeviceId}/identity-conflicts`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setConflictReport(value) }).catch((reason) => setFailure('identity', reason, 'Identity review could not be loaded.')),
      api<HistoricalIdentityReport>(`/api/v2/devices/${selectedDeviceId}/historical-identities`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setHistoricalReport(value?.totals ? value : null) }).catch((reason) => setFailure('history', reason, 'Historical identity backlog could not be loaded.')),
      api<{ job: UserDeletionJob | null }>(`/api/v2/devices/${selectedDeviceId}/user-deletion-jobs/latest`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setDeletionJob(value.job) }).catch((reason) => setFailure('deletion', reason, 'Deletion-job status could not be loaded.')),
    ]
    await Promise.allSettled(tasks)
    if (diagnosticsRequest.current === controller) setLoadingDiagnostics(false)
  }, [selectedDeviceId])

  const refreshWorkspace = useCallback(async () => {
    await Promise.all([loadDirectory(), loadDiagnostics()])
  }, [loadDiagnostics, loadDirectory])

  useEffect(() => {
    directoryRequest.current?.abort()
    diagnosticsRequest.current?.abort()
    setSection('directory')
    setPickerOpen(!selectedDeviceId)
    setRows([])
    setIntegrity(null)
    setConflictReport(null)
    setHistoricalReport(null)
    setDeletionJob(null)
    setDeviceDetail(null)
    setNextCursor(null)
    setQuery('')
    setCnicQuery('')
    setIdentity('ALL')
    setRole('ALL')
    setDirectoryError('')
    setDiagnosticErrors({})
    setSelectedUserKeys(new Set())
    setDialog(null)
    setResolutionDialog(null)
    setHistoricalDialog(null)
    setBulkDialogOpen(false)
    setRevokeLeaseOpen(false)
    setCommand(null)
  }, [selectedDeviceId])

  useEffect(() => {
    const timeout = window.setTimeout(() => void loadDirectory(), 250)
    return () => window.clearTimeout(timeout)
  }, [loadDirectory])

  useEffect(() => {
    void loadDiagnostics()
    return () => diagnosticsRequest.current?.abort()
  }, [loadDiagnostics])

  useEffect(() => {
    if (revision === revisionRef.current) return
    revisionRef.current = revision
    void refreshWorkspace()
  }, [refreshWorkspace, revision])

  useEffect(() => () => directoryRequest.current?.abort(), [])

  useEffect(() => {
    if (!selected?.active_lease) return
    const interval = window.setInterval(() => setLeaseClock((value) => value + 1), 1000)
    return () => window.clearInterval(interval)
  }, [selected?.active_lease])

  useEffect(() => {
    if (!deletionJob || !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(deletionJob.status)) return
    const timeout = window.setTimeout(async () => {
      try {
        const response = await api<{ job: UserDeletionJob }>(`/api/v2/user-deletion-jobs/${deletionJob.job_id}`)
        const finished = !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(response.job.status)
        setDeletionJob(response.job)
        if (finished) {
          setSelectedUserKeys(new Set())
          await Promise.all([refreshWorkspace(), refreshFleet()])
          if (response.job.status === 'SUCCEEDED') toast.notice('Every selected user was deleted and verified; attendance was preserved.')
          else toast.error(`Bulk deletion ended as ${response.job.status}. Review the per-user result.`)
        }
      } catch (reason) {
        toast.error(requestError(reason, 'Unable to refresh deletion progress.'))
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [deletionJob, refreshFleet, refreshWorkspace, toast])

  const trackedCommand = command || selected?.active_command || null
  useEffect(() => {
    if (!trackedCommand || terminalCommandStates.has(trackedCommand.status)) return
    const timeout = window.setTimeout(async () => {
      try {
        const updated = await api<Command>(`/api/v2/commands/${trackedCommand.command_id}`)
        setCommand(updated)
        if (terminalCommandStates.has(updated.status)) {
          await Promise.all([refreshWorkspace(), refreshFleet()])
          if (updated.status === 'SUCCEEDED') toast.notice(`${updated.type.replaceAll('_', ' ')} completed and was verified.`)
          else toast.error(updated.error_message || `${updated.type.replaceAll('_', ' ')} ended as ${updated.status}.`)
        }
      } catch (reason) {
        toast.error(requestError(reason, 'Unable to refresh command state.'))
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [refreshFleet, refreshWorkspace, toast, trackedCommand])

  const cancelCommand = async (row: Command) => {
    try {
      setCommand(await api<Command>(`/api/v2/commands/${row.command_id}/cancel`, { method: 'POST', body: '{}' }))
      await refreshWorkspace()
    } catch (reason) {
      toast.error(requestError(reason, 'Unable to cancel command.'))
    }
  }

  const syncFromTerminal = async () => {
    if (!selectedDeviceId) return
    try {
      const response = await api<Command>(`/api/v1/devices/${selectedDeviceId}/users/refresh`, { method: 'POST', body: '{}' })
      setCommand(response)
      toast.notice('Terminal user synchronization is queued and durably tracked.')
    } catch (reason) {
      toast.error(requestError(reason, 'Terminal user synchronization could not be queued.'))
    }
  }

  const cancelDeletionJob = async (password: string) => {
    if (!deletionJob) return
    try {
      const response = await api<{ job: UserDeletionJob }>(`/api/v2/user-deletion-jobs/${deletionJob.job_id}/cancel`, { method: 'POST', body: JSON.stringify({ password }) })
      setDeletionJob(response.job)
      toast.notice('Cancellation recorded. A running user will finish verification; untouched users will be skipped.')
    } catch (reason) {
      toast.error(requestError(reason, 'Unable to cancel deletion job.'))
      throw reason
    }
  }

  const baseWritable = Boolean(selected?.zkt?.certification_state === 'CERTIFIED' && selected.zkt.snapshot_complete)
  const capabilities = selected?.zkt?.capabilities || {}
  const canCreate = baseWritable && capabilities.create_user === true
  const canEditProfile = baseWritable && capabilities.user_write === true
  const canGrantLease = baseWritable && capabilities.admin_lease === true && !selected?.active_lease
  const canDeleteProfile = baseWritable && capabilities.delete_user === true
  const activeDeletionJob = Boolean(deletionJob && ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(deletionJob.status))
  const actionReason = selected?.zkt?.writes_disabled_reason || 'This terminal capability is not certified or its complete snapshot is pending.'
  const createReason = !baseWritable ? actionReason : capabilities.create_user !== true ? 'This terminal does not advertise the certified create-user capability.' : ''
  const editCapabilityReason = !baseWritable ? actionReason : capabilities.user_write !== true ? 'This terminal does not advertise the certified user-write capability.' : ''
  const leaseCapabilityReason = !baseWritable ? actionReason : capabilities.admin_lease !== true ? 'This terminal does not advertise certified temporary enrollment access.' : ''
  const deleteCapabilityReason = !baseWritable ? actionReason : capabilities.delete_user !== true ? 'This terminal does not advertise the certified delete-user capability.' : ''
  const eligibleRows = rows.filter((user) => user.privilege !== 14 && !user.read_only && !user.current_command_state)
  const selectedUsers = eligibleRows.filter((user) => selectedUserKeys.has(user.user_key))
  const identityComplete = integrity?.with_cnic ?? rows.filter((row) => row.identity_complete).length
  const identityTotal = integrity?.total_users ?? selected?.zkt?.user_count ?? rows.length
  const completeness = identityTotal ? Math.round(identityComplete * 100 / identityTotal) : 0
  const identitiesNeedingAttention = (integrity?.missing_cnic ?? 0) + (integrity?.unresolved_duplicate_users ?? 0)
  const historyCount = historicalReport?.totals.unresolved_events ?? 0
  const identityCount = conflictReport?.unresolved_groups ?? 0
  const activeFilters = [
    query && { key: 'query', label: `Search: ${query}` },
    cnicQuery && { key: 'cnic', label: `Exact CNIC: ${cnicQuery}` },
    identity !== 'ALL' && { key: 'identity', label: `Identity: ${identity.replaceAll('_', ' ').toLowerCase()}` },
    role !== 'ALL' && { key: 'role', label: role === '14' ? 'Administrators' : 'Regular users' },
  ].filter(Boolean) as Array<{ key: string; label: string }>

  const clearFilter = (key: string) => {
    if (key === 'query') setQuery('')
    if (key === 'cnic') setCnicQuery('')
    if (key === 'identity') setIdentity('ALL')
    if (key === 'role') setRole('ALL')
  }
  const clearFilters = () => { setQuery(''); setCnicQuery(''); setIdentity('ALL'); setRole('ALL') }
  const toggleUser = (userKey: string, checked: boolean) => setSelectedUserKeys((current) => {
    const next = new Set(current)
    if (checked) next.add(userKey); else next.delete(userKey)
    return next
  })

  const handleTabKey = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const tabs = Array.from(tabsRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]') || [])
    const current = tabs.indexOf(event.currentTarget)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    tabs[next]?.focus()
    tabs[next]?.click()
  }

  const renderUserRow = (user: DeviceUser, style?: CSSProperties, virtualIndex?: number) => {
    const rowBusy = Boolean(user.current_command_state)
    const editReason = rowBusy ? `Operation ${user.current_command_state} is already active.` : user.read_only ? 'This record is read-only.' : editCapabilityReason
    const leaseReason = selected?.active_lease ? 'Another enrollment lease is already active on this terminal.' : user.privilege === 14 ? 'This user is already an administrator.' : rowBusy ? `Operation ${user.current_command_state} is already active.` : user.read_only ? 'This record is read-only.' : leaseCapabilityReason
    const deleteReason = user.privilege === 14 ? 'Permanent administrators cannot be deleted here.' : rowBusy ? `Operation ${user.current_command_state} is already active.` : user.read_only ? 'This record is read-only.' : activeDeletionJob ? 'A durable deletion job is already active for this terminal.' : deleteCapabilityReason
    const rowCanEdit = canEditProfile && !user.read_only && !rowBusy
    const rowCanLease = canGrantLease && user.privilege === 0 && !user.read_only && !rowBusy
    const rowCanDelete = canDeleteProfile && user.privilege !== 14 && !user.read_only && !rowBusy && !activeDeletionJob
    return (
      <article ref={virtualIndex == null ? undefined : userVirtualizer.measureElement} data-index={virtualIndex} style={style} key={user.user_key} className={`user-directory-row ${user.identity_conflict_resolved ? 'identity-resolved' : user.identity_conflict_code ? 'identity-conflict' : user.identity_complete ? '' : 'identity-missing'}`} aria-label={`${user.display_name}, user ${user.user_id}`}>
        <div className="user-directory-cell user-person" data-label="Identity">
          <label className="user-select-hit"><input className="user-select" type="checkbox" aria-label={`Select ${user.display_name} for bulk deletion`} checked={selectedUserKeys.has(user.user_key)} disabled={!rowCanDelete} onChange={(event) => toggleUser(user.user_key, event.target.checked)} /><span className="sr-only">Select user</span></label>
          <span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span>
          <span><strong>{user.display_name}</strong><small>{user.identity_conflict_code ? identityConflictText(user) : user.cnic_masked || 'CNIC missing · punches blocked until enriched'}</small></span>
        </div>
        <div className="user-directory-cell" data-label="Terminal record"><strong>User {user.user_id}</strong><small>UID {user.uid} · version {user.row_version}</small><code>{user.machine_name_preview || 'No machine preview'}</code></div>
        <div className="user-directory-cell user-state-stack" data-label="Role and state"><StatusBadge state={user.privilege === 14 ? 'ADMINISTRATOR' : 'REGULAR'} />{user.current_command_state ? <StatusBadge state={user.current_command_state} /> : <small>{user.shift_worker ? 'Shift worker' : 'Standard worker'}</small>}</div>
        <div className="user-directory-cell" data-label="Last synchronized"><strong>{relativeTime(user.observed_at)}</strong><small>{dateTime(user.observed_at)}</small></div>
        <UserActionMenu user={user} canEdit={rowCanEdit} canLease={rowCanLease} canDelete={rowCanDelete} editReason={editReason} leaseReason={leaseReason} deleteReason={deleteReason} onEdit={() => setDialog({ mode: 'edit', user })} onLease={() => setDialog({ mode: 'lease', user })} onDelete={() => setDialog({ mode: 'delete', user })} />
      </article>
    )
  }

  const directoryPanel = (
    <section className="panel user-directory-panel">
      <header className="panel-header"><div><h2>Terminal directory</h2><p>{rows.length.toLocaleString()} loaded · mutations remain scoped to {selected?.display_name}</p></div><div className="directory-header-actions"><button className="button secondary" type="button" onClick={() => void refreshWorkspace()}><Icon name="refresh" /> Refresh view</button><button className="button secondary" type="button" disabled={!selected?.connected || Boolean(trackedCommand && !terminalCommandStates.has(trackedCommand.status))} onClick={() => void syncFromTerminal()}><Icon name="server" /> Sync from terminal</button></div></header>
      <div className="users-directory-toolbar">
        <label className="search-field"><span className="sr-only">Search user name, user ID, or UID</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, user ID, or UID" /></label>
        <label><span className="sr-only">Exact CNIC search</span><input inputMode="numeric" autoComplete="off" value={cnicQuery} onChange={(event) => setCnicQuery(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="Exact 13-digit CNIC" aria-invalid={Boolean(cnicError)} /></label>
        <label><span className="sr-only">Identity completeness</span><select value={identity} onChange={(event) => setIdentity(event.target.value)}><option value="ALL">All identities</option><option value="COMPLETE">CNIC complete</option><option value="MISSING">CNIC missing or unresolved</option><option value="CONFLICT">CNIC conflict</option><option value="RESOLVED_ALIAS">Verified aliases</option></select></label>
        <label><span className="sr-only">User role</span><select value={role} onChange={(event) => setRole(event.target.value)}><option value="ALL">All roles</option><option value="0">Regular users</option><option value="14">Administrators</option></select></label>
      </div>
      {activeFilters.length > 0 && <div className="active-filter-bar" aria-label="Active user filters"><span>{activeFilters.length} active</span>{activeFilters.map((filter) => <button type="button" key={filter.key} onClick={() => clearFilter(filter.key)}>{filter.label}<Icon name="x" /></button>)}<button className="text-button" type="button" onClick={clearFilters}>Clear all</button></div>}
      {directoryError && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{directoryError}</span><button className="button secondary" type="button" onClick={() => void loadDirectory()}>Retry directory</button></div>}
      <div className="users-selection-row">
        <label className="check-field"><input type="checkbox" checked={eligibleRows.length > 0 && selectedUsers.length === eligibleRows.length} disabled={!canDeleteProfile || activeDeletionJob || !eligibleRows.length} onChange={(event) => setSelectedUserKeys(event.target.checked ? new Set(eligibleRows.map((user) => user.user_key)) : new Set())} /><span><strong>Select eligible loaded users</strong><small>{selectedUsers.length} selected of {eligibleRows.length} eligible</small></span></label>
        <span>Administrators, read-only rows, and active operations are always excluded.</span>
      </div>
      <div ref={userTableRef} className={`user-directory-table ${rows.length > 200 ? 'is-virtualized' : ''}`} aria-busy={loadingDirectory} aria-label="Selected terminal users">
        <div className="user-directory-head" aria-hidden="true"><span>Identity</span><span>Terminal record</span><span>Role & state</span><span>Last synchronized</span><span>Actions</span></div>
        {loadingDirectory && !rows.length && <div className="users-loading-list" role="status">{Array.from({ length: 5 }, (_, index) => <span key={index}><i /><i /><i /></span>)}</div>}
        {rows.length <= 200 && rows.map((user) => renderUserRow(user))}
        {rows.length > 200 && <div className="virtual-user-rows" style={{ height: userVirtualizer.getTotalSize() }}>{userVirtualizer.getVirtualItems().map((item) => renderUserRow(rows[item.index], { position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${item.start}px)` }, item.index))}</div>}
        {!loadingDirectory && !rows.length && !directoryError && <div className="empty-state"><Icon name="users" /><h3>No users match this directory view.</h3><p>{activeFilters.length ? 'Clear filters or search another identity.' : 'The selected terminal has no ADD-managed users.'}</p>{activeFilters.length > 0 && <button className="button secondary" type="button" onClick={clearFilters}>Clear filters</button>}</div>}
      </div>
      {nextCursor && <div className="load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadDirectory(nextCursor, true)}>{loadingMore ? 'Loading more users…' : 'Load more terminal users'}</button><small>{rows.length.toLocaleString()} users loaded · selection applies only to loaded rows</small></div>}
    </section>
  )

  const identityPanel = (
    <section className="panel users-review-panel">
      <header className="panel-header"><div><p className="eyebrow">REVERSIBLE IDENTITY REVIEW</p><h2>Exact-CNIC identity review</h2><p>Review duplicate terminal identities without merging users, fingerprints, UIDs, or attendance.</p></div><StatusBadge state={identityCount ? 'REVIEW REQUIRED' : 'RESOLVED'} /></header>
      {diagnosticErrors.identity && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{diagnosticErrors.identity}</span><button className="button secondary" type="button" onClick={() => void loadDiagnostics()}>Retry review</button></div>}
      {loadingDiagnostics && !conflictReport && <div className="empty-state compact"><Icon name="refresh" /><p>Loading identity evidence…</p></div>}
      {conflictReport?.groups.map((group) => <article key={group.group_token} className={`identity-review-card pattern-${group.status === 'UNRESOLVED' ? 'blocked' : 'confirmed'}`}><header><div><strong>{group.cnic_masked || 'Masked CNIC'}</strong><small>{group.classification.replaceAll('_', ' ')}</small></div><StatusBadge state={group.status} /></header><div className="identity-review-members">{group.members.map((member) => <div key={member.user_key}><span className="avatar">{member.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{member.display_name}</strong><small>User {member.user_id} · UID {member.uid}</small></span><span><strong>{member.punch_evidence.captured_count.toLocaleString()} captured</strong><small>{member.punch_evidence.last_captured_at ? `Last ${dateTime(member.punch_evidence.last_captured_at)}` : 'No punches in ADD evidence'}</small></span></div>)}</div><footer><small>{group.status === 'UNRESOLVED' ? group.recommended_action.replaceAll('_', ' ') : group.resolution_reason}</small><button className={`button ${group.status === 'UNRESOLVED' ? 'secondary' : 'text-button'}`} type="button" onClick={() => setResolutionDialog({ mode: group.status === 'UNRESOLVED' ? 'resolve' : 'revoke', group })}><Icon name={group.status === 'UNRESOLVED' ? 'shield' : 'alert'} />{group.status === 'UNRESOLVED' ? 'Review same-employee alias' : 'Revoke resolution'}</button></footer></article>)}
      {!loadingDiagnostics && !diagnosticErrors.identity && !conflictReport?.groups.length && <div className="empty-state"><Icon name="shield" /><h3>No exact-CNIC groups need review.</h3><p>The selected terminal’s current identity snapshot is clear.</p></div>}
    </section>
  )

  const historicalPanel = (
    <section className="panel users-review-panel">
      <header className="panel-header"><div><p className="eyebrow">PRESERVED ATTENDANCE · IDENTITY REQUIRED</p><h2>Historical identity backlog</h2><p>{historyCount.toLocaleString()} events remain fail-closed until exact authoritative evidence is supplied.</p></div><StatusBadge state={historyCount ? 'HR EVIDENCE REQUIRED' : 'RESOLVED'} /></header>
      {diagnosticErrors.history && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{diagnosticErrors.history}</span><button className="button secondary" type="button" onClick={() => void loadDiagnostics()}>Retry backlog</button></div>}
      {loadingDiagnostics && !historicalReport && <div className="empty-state compact"><Icon name="refresh" /><p>Loading preserved attendance cohorts…</p></div>}
      <div className="historical-review-grid">{[...(historicalReport?.rows || []), ...(historicalReport?.unassigned_groups || [])].map((candidate) => <article key={candidate.source_user_key || candidate.group_token} className="historical-review-card pattern-blocked"><header><div><strong>{candidate.display_name}</strong><small>User {candidate.user_id} · UID {candidate.uid || 'missing'}{candidate.row_version == null ? ' · exact event cohort' : ` · version ${candidate.row_version}`}</small></div><StatusBadge state={candidate.resolution_path.replaceAll('_', ' ')} /></header><div className="historical-review-facts"><span><strong>{candidate.event_count.toLocaleString()}</strong><small>Preserved events</small></span><span><strong>{candidate.blocked_count.toLocaleString()}</strong><small>Identity blocked</small></span><span><strong>{candidate.quarantined_count.toLocaleString()}</strong><small>Quarantined</small></span></div><p>{candidate.operator_actionable ? 'Requires exact authoritative HR evidence before preserved attendance can be requeued.' : 'This cohort remains fail-closed until its identity conflict or reuse review is resolved.'}</p><button className="button secondary" type="button" disabled={!candidate.operator_actionable} onClick={() => { if (candidate.resolution_path === 'ACTIVE_USER_ENRICHMENT') { const activeUser = rows.find((row) => row.user_key === candidate.active_user_key); if (activeUser) setDialog({ mode: 'edit', user: activeUser }); else toast.error('Linked current user is unavailable. Refresh and retry.') } else setHistoricalDialog({ candidate }) }}><Icon name="shield" />{candidate.resolution_path === 'ACTIVE_USER_ENRICHMENT' ? 'Enrich current user' : candidate.resolution_path === 'CURRENT_IDENTITY_EVIDENCE' ? 'Verify against current identity' : 'Enter verified HR evidence'}</button></article>)}</div>
      {!loadingDiagnostics && !diagnosticErrors.history && !historyCount && <div className="empty-state"><Icon name="shield" /><h3>No preserved attendance awaits identity evidence.</h3><p>Historical identity delivery is clear for this terminal.</p></div>}
    </section>
  )

  return (
    <div className="users-workspace">
      <PageHeader eyebrow="SELECTED-TERMINAL USER CONTROL" title="Device users" description="Manage certified terminal identities with clearer capability, identity, and safety evidence." action={selected && <div className="page-action-stack"><button className="button primary" type="button" disabled={!canCreate} title={!canCreate ? createReason : undefined} aria-describedby={!canCreate ? 'create-user-disabled-reason' : undefined} onClick={() => setDialog({ mode: 'create' })}><Icon name="userPlus" /> Add user</button>{!canCreate && <small id="create-user-disabled-reason">{createReason}</small>}</div>} />
      <TerminalPicker devices={devices} selectedDeviceId={selectedDeviceId} open={pickerOpen} onOpenChange={setPickerOpen} onSelect={onSelectDevice} />
      {!selected ? <section className="panel empty-state users-select-empty"><Icon name="users" /><h2>Select a terminal to manage its users.</h2><p>Every mutation, lease, and identity review remains explicitly scoped to one authorized ZKT device.</p></section> : <>
        <section className="selected-terminal-context" aria-label="Selected terminal context"><div><span className="terminal-context-icon"><Icon name="server" /></span><span><small>Terminal context</small><strong>{selected.zkt?.model || 'ZKT terminal'} · {selected.zkt?.ip_address || 'No IP reported'}</strong></span></div><StatusBadge state={selected.state} live={selected.connected} /><span><small>Snapshot</small><strong>{selected.zkt?.snapshot_complete ? 'Complete' : 'Pending'}</strong></span><span><small>Last contact</small><strong>{relativeTime(selected.last_seen_at)}</strong></span></section>

        <section className="metric-grid users-metrics" aria-label="Selected terminal user indicators"><Metric label="Terminal users" value={identityTotal.toLocaleString()} detail={`${rows.length.toLocaleString()} loaded in this view`} icon="users" /><Metric label="CNIC complete" value={`${completeness}%`} detail={`${identityComplete.toLocaleString()} of ${identityTotal.toLocaleString()} identities`} icon="check" tone={completeness === 100 ? 'positive' : 'warning'} /><Metric label="Identity attention" value={identitiesNeedingAttention.toLocaleString()} detail="Missing CNIC or unresolved duplicate" icon="alert" tone={identitiesNeedingAttention ? 'warning' : 'positive'} /><Metric label="Preserved backlog" value={historyCount.toLocaleString()} detail="Events awaiting identity evidence" icon="shield" tone={historyCount ? 'warning' : 'positive'} /></section>

        {!baseWritable && <div className="capability-banner pattern-waiting"><Icon name="shield" /><div><strong>User writes are unavailable.</strong><span>{actionReason}</span></div><StatusBadge state={selected.zkt?.certification_state || 'READ ONLY'} /></div>}
        {diagnosticErrors.device && <div className="capability-banner pattern-waiting"><Icon name="alert" /><div><strong>Live terminal status is temporarily unavailable.</strong><span>{diagnosticErrors.device}</span></div><button className="button secondary" type="button" onClick={() => void loadDiagnostics()}>Retry</button></div>}
        {selected.active_lease && <div className={`active-lease-banner pattern-${statusPattern(selected.active_lease.state)}`}><span className="command-symbol"><Icon name="shield" /></span><div><p className="eyebrow">TEMPORARY ENROLLMENT ACCESS</p><h3>{selected.active_lease.state.replaceAll('_', ' ')}</h3><p>{selected.active_lease.expires_at ? `Expires ${relativeTime(selected.active_lease.expires_at)} · ${dateTime(selected.active_lease.expires_at)}` : 'Waiting for a verified terminal expiry.'}{selected.active_lease.last_error ? ` · ${selected.active_lease.last_error}` : ''}</p></div><button className="button destructive" type="button" disabled={selected.active_lease.state === 'REVOKING'} onClick={() => setRevokeLeaseOpen(true)}>Revoke access</button></div>}
        {trackedCommand && <CommandProgress command={trackedCommand} onCancel={cancelCommand} />}
        {deletionJob && <BulkDeletionProgress job={deletionJob} onCancel={cancelDeletionJob} />}

        <div ref={tabsRef} className="section-tabs users-section-tabs" role="tablist" aria-label="User workspace sections">{userSections.map((item) => { const count = item.id === 'directory' ? identityTotal : item.id === 'identity' ? identityCount : historyCount; return <button key={item.id} id={`users-tab-${item.id}`} role="tab" type="button" aria-selected={section === item.id} aria-controls={`users-panel-${item.id}`} tabIndex={section === item.id ? 0 : -1} className={section === item.id ? 'active' : ''} onClick={() => setSection(item.id)} onKeyDown={handleTabKey}><Icon name={item.icon} /><span>{item.label}</span><strong>{count.toLocaleString()}</strong></button> })}</div>
        <div id={`users-panel-${section}`} role="tabpanel" aria-labelledby={`users-tab-${section}`}>{section === 'directory' ? directoryPanel : section === 'identity' ? identityPanel : historicalPanel}</div>

        {selectedUsers.length > 0 && <aside className="bulk-selection-bar" aria-live="polite"><div><span className="bulk-selection-count">{selectedUsers.length}</span><span><strong>{selectedUsers.length} user{selectedUsers.length === 1 ? '' : 's'} selected</strong><small>Only eligible loaded regular users · attendance remains immutable</small></span></div><div><button className="button secondary" type="button" onClick={() => setSelectedUserKeys(new Set())}>Clear selection</button><button className="button destructive" type="button" disabled={!canDeleteProfile || activeDeletionJob} onClick={() => setBulkDialogOpen(true)}><Icon name="trash" /> Delete selected</button></div></aside>}
      </>}

      {dialog && selected && <UserOperationDialog state={dialog} device={selected} onClose={() => setDialog(null)} onCommand={setCommand} toast={toast} />}
      {bulkDialogOpen && selected && selectedUsers.length > 0 && <BulkDeletionDialog users={selectedUsers} device={selected} onClose={() => setBulkDialogOpen(false)} onCreated={(job) => { setDeletionJob(job); toast.notice('Durable bulk deletion job created. ADD will verify one user at a time.') }} />}
      {resolutionDialog && selected && <IdentityResolutionDialog state={resolutionDialog} device={selected} onClose={() => setResolutionDialog(null)} onComplete={(report) => { setConflictReport(report); void refreshWorkspace() }} toast={toast} />}
      {historicalDialog && selected && <HistoricalIdentityResolutionDialog state={historicalDialog} device={selected} onClose={() => setHistoricalDialog(null)} onComplete={async () => { await Promise.all([refreshWorkspace(), refreshFleet()]) }} toast={toast} />}
      {revokeLeaseOpen && selected && <LeaseRevokeDialog device={selected} onClose={() => setRevokeLeaseOpen(false)} onCommand={setCommand} toast={toast} />}
    </div>
  )
}
