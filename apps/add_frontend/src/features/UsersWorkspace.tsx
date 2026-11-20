// People (07-people): /people is the device picker, /people/:deviceId the
// people on one machine. Every write keeps today's safeguards (password,
// typed confirmations, reasons, re-checked selections); only the words, the
// layout and the progress feedback changed.
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import './UsersWorkspace.css'
import './people/people.css'
import { api, queryString } from '../api'
import type { Command, Device, HistoricalIdentityCandidate, HistoricalIdentityReport, IdentityConflictGroup, IdentityConflictReport, UserDeletionJob } from '../types'
import { EmptyState, ErrorState, FilterBar, PageHeader, Sheet, Skeleton, TabPanel, Tabs, useIsPhone } from '../ui'
import { Icon } from '../Icon'
import { flag, oneOf, text, useUrlState } from '../lib/urlState'
import { useDivision } from '../lib/useDivision'
import { idempotency } from '../lib/idempotency'
import { clock } from '../lib/time'
import type { UserDialogState } from '../lib/dashboardTypes'
import { isFinalCommand, type CommandStep } from '../lib/useCommandTracker'
import {
  CAPABILITY_OFF, CHANGES, DAMAGED_ENTRY, HEADER, LIST, PICKER, REMOVE_MANY, changeDone, changeLabel, writesOff,
} from '../words/people'
import type { CommandMeta } from './Users'
import { BulkDeletionProgress, isActiveRemoval } from './people/BulkRemovalCard'
import { AdminAccessBanner, EndAdminAccessDialog } from './people/AdminAccessBanner'
import { ChangesTray, changeFailure } from './people/ChangesTray'
import { DevicePickerList, type PickerShow } from './people/DevicePickerList'
import { MachineCheckDialog } from './people/MachineCheck'
import { OldCheckinsTab, oldCandidates } from './people/OldCheckinsTab'
import { PeopleDeviceHeader, PeopleNotice, StatusFailedLine } from './people/PeopleHeader'
import { PeopleList } from './people/PeopleList'
import { PersonSheet, type PersonActions } from './people/PersonSheet'
import { SameCnicTab } from './people/SameCnicTab'
import { useCommandTray, type RetryState, type TrayEntry } from './people/useCommandTray'
import { usePeopleSummary } from './people/usePeopleSummary'
import { peopleErrorText } from './people/errors'
import {
  baseWritable, cnicDigits, divisionOf, friendlyName, isBulkSelectionEligible, isCnicSearch, mergeDevice,
  type PeopleDevice, type PeopleIntegrity, type PeopleListResponse, type PeopleUser,
} from './people/model'

// The dialogs load when first opened, so the People list itself stays small.
const UserOperationDialog = lazy(() => import('./Users').then((module) => ({ default: module.UserOperationDialog })))
const BulkDeletionDialog = lazy(() => import('./Users').then((module) => ({ default: module.BulkDeletionDialog })))
const IdentityResolutionDialog = lazy(() => import('./Users').then((module) => ({ default: module.IdentityResolutionDialog })))
const HistoricalIdentityResolutionDialog = lazy(() => import('./Users').then((module) => ({ default: module.HistoricalIdentityResolutionDialog })))

type ToastLike = {
  notice: (text: string) => void
  error: (text: string) => void
  show?: (input: { tone: 'success' | 'info' | 'error'; text: string; action?: { label: string; onClick: () => void } }) => void
}

type UserSelectionValidationResponse = { rows: PeopleUser[]; missing_user_keys: string[] }

const PAGE = 200
const MAX_PAGE = 500
const FINISHED_JOB_MS = 24 * 60 * 60_000

const SHOW = ['', 'fixing', 'admins', 'new'] as const
const PROBLEM = ['', 'missing', 'shared', 'typed', 'confirmed'] as const
const ROLE = ['', 'regular', 'admin'] as const
const SORT = ['problems', 'name', 'id', 'newest'] as const
const TAB = ['people', 'shared', 'old'] as const

const workspaceSchema = {
  q: text(),
  show: oneOf(SHOW, ''),
  problem: oneOf(PROBLEM, ''),
  role: oneOf(ROLE, ''),
  shift: flag(),
  changing: flag(),
  sort: oneOf(SORT, 'problems'),
  tab: oneOf(TAB, 'people'),
  decision: oneOf(['', 'done'] as const, ''),
  fixable: oneOf(['', '0'] as const, ''),
}

const pickerSchema = {
  q: text(),
  show: oneOf(['', 'fixing', 'admin-on'] as const, ''),
  spares: flag(),
}

/** Filters kept when you change device (Open questions 7): the quick view and the rest, not the search. */
const KEPT_ON_DEVICE_CHANGE = ['division', 'show', 'problem', 'role', 'shift', 'changing', 'sort']

const IDENTITY_FOR_PROBLEM: Record<string, string> = { missing: 'NO_CNIC', shared: 'CONFLICT', typed: 'TYPED_WRONG', confirmed: 'RESOLVED_ALIAS' }
const SORT_PARAM: Record<string, string> = { problems: 'problems', name: 'name', id: 'employee_id', newest: 'newest' }

export function UsersView({
  devices,
  devicesLoaded = true,
  devicesError,
  selectedDeviceId,
  onSelectDevice,
  revision,
  toast,
  refreshFleet,
}: {
  devices: Device[]
  /** False until the first device list answer (HOME-6): never show "no device matches" before it. */
  devicesLoaded?: boolean
  devicesError?: unknown
  selectedDeviceId: string
  onSelectDevice: (id: string) => void
  revision: number
  toast: ToastLike
  refreshFleet: () => Promise<void>
}) {
  const lastDevice = useRef(selectedDeviceId)
  const switched = Boolean(lastDevice.current && lastDevice.current !== selectedDeviceId)
  useEffect(() => { lastDevice.current = selectedDeviceId }, [selectedDeviceId])
  if (!selectedDeviceId) {
    return <PeoplePickerPage devices={devices} loaded={devicesLoaded || devices.length > 0} error={devicesError} onRetry={refreshFleet} onSelect={onSelectDevice} />
  }
  return (
    <PeopleWorkspace
      key={selectedDeviceId}
      devices={devices}
      devicesLoaded={devicesLoaded || devices.length > 0}
      devicesError={devicesError}
      deviceId={selectedDeviceId}
      switched={switched}
      revision={revision}
      toast={toast}
      refreshFleet={refreshFleet}
    />
  )
}

function PeoplePickerPage({ devices, loaded, error, onRetry, onSelect }: {
  devices: Device[]
  loaded: boolean
  error: unknown
  onRetry: () => Promise<void>
  onSelect: (id: string) => void
}) {
  const [filters, setFilters] = useUrlState(pickerSchema)
  const [division, setDivision] = useDivision()
  const [query, setQuery] = useState(filters.q)
  const summary = usePeopleSummary()
  return (
    <div className="users-workspace people-page">
      <PageHeader title={PICKER.title} description={PICKER.description} />
      <DevicePickerList
        devices={devices}
        loaded={loaded}
        error={error}
        onRetry={onRetry}
        summary={summary.rows}
        summaryFailed={summary.failed}
        query={query}
        onQueryChange={(value) => { setQuery(value); setFilters({ q: value }) }}
        show={filters.show as PickerShow}
        onShowChange={(value) => setFilters({ show: value })}
        spares={filters.spares}
        onSparesChange={(value) => setFilters({ spares: value })}
        division={division}
        onShowAllDivisions={() => setDivision('all')}
        hrefFor={(id) => `/people/${encodeURIComponent(id)}${division !== 'all' ? `?division=${division}` : ''}`}
        onPick={(id, event) => { event.preventDefault(); onSelect(id) }}
      />
    </div>
  )
}

function PeopleWorkspace({ devices, devicesLoaded, devicesError, deviceId, switched, revision, toast, refreshFleet }: {
  devices: Device[]
  devicesLoaded: boolean
  devicesError: unknown
  deviceId: string
  switched: boolean
  revision: number
  toast: ToastLike
  refreshFleet: () => Promise<void>
}) {
  const location = useLocation()
  const navigate = useNavigate()
  const phone = useIsPhone()
  const [division] = useDivision()
  const [filters, setFilters] = useUrlState(workspaceSchema)
  const requestedUserId = useMemo(() => new URLSearchParams(location.search).get('user_id')?.trim() || '', [])
  const requestedQuery = useMemo(() => (switched ? '' : filters.q), [])
  const [searchText, setSearchText] = useState(requestedUserId || requestedQuery)
  const [debouncedSearch, setDebouncedSearch] = useState(searchText)
  const fleetDevice = devices.find((device) => device.connector_id === deviceId) as PeopleDevice | undefined
  const [detail, setDetail] = useState<PeopleDevice | null>(null)
  const selected: PeopleDevice | null = detail ?? fleetDevice ?? null
  const [rows, setRows] = useState<PeopleUser[]>([])
  const [integrity, setIntegrity] = useState<PeopleIntegrity | null>(null)
  const [filteredTotal, setFilteredTotal] = useState<number | null>(null)
  const [nextOffset, setNextOffset] = useState<number | null>(null)
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [loadingList, setLoadingList] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [listError, setListError] = useState<unknown>(null)
  const [loadedAt, setLoadedAt] = useState<Date | null>(null)
  const [conflictReport, setConflictReport] = useState<IdentityConflictReport | null>(null)
  const [historicalReport, setHistoricalReport] = useState<HistoricalIdentityReport | null>(null)
  const [deletionJob, setDeletionJob] = useState<UserDeletionJob | null>(null)
  const [jobCleared, setJobCleared] = useState('')
  const [diagnostics, setDiagnostics] = useState<{ device?: unknown; identity?: unknown; history?: unknown }>({})
  const [loadingDiagnostics, setLoadingDiagnostics] = useState(true)
  const [dialog, setDialog] = useState<{ state: Exclude<UserDialogState, null>; draft?: Record<string, unknown> } | null>(null)
  const [resolutionDialog, setResolutionDialog] = useState<{ mode: 'resolve' | 'revoke'; group: IdentityConflictGroup } | null>(null)
  const [historicalDialog, setHistoricalDialog] = useState<{ candidate: HistoricalIdentityCandidate } | null>(null)
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false)
  const [validating, setValidating] = useState(false)
  const [endLeaseOpen, setEndLeaseOpen] = useState(false)
  const [machineDialogOpen, setMachineDialogOpen] = useState(false)
  const [machineCommand, setMachineCommand] = useState<Command | null>(null)
  const [changeDeviceOpen, setChangeDeviceOpen] = useState(false)
  const [changeQuery, setChangeQuery] = useState('')
  const [changeShow, setChangeShow] = useState<PickerShow>('')
  const [changeSpares, setChangeSpares] = useState(false)
  const [sheetKey, setSheetKey] = useState('')
  const [selectMode, setSelectMode] = useState(false)
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set())
  const [highlightUserKey, setHighlightUserKey] = useState('')
  const [oldQuery, setOldQuery] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [changedKeys, setChangedKeys] = useState<Set<string>>(new Set())
  const tray = useCommandTray()
  const summary = usePeopleSummary(changeDeviceOpen ? 1 : 0)
  const listRequest = useRef<AbortController | null>(null)
  const diagnosticsRequest = useRef<AbortController | null>(null)
  const knownUsers = useRef<Map<string, PeopleUser>>(new Map())
  const activeDevice = useRef(deviceId)
  activeDevice.current = deviceId
  const revisionRef = useRef(revision)
  const deepLinkDone = useRef(!requestedUserId)
  const qLinkDone = useRef(!requestedQuery || switched)
  const refreshKey = useRef<string>('')
  const rowsRef = useRef(rows)
  rowsRef.current = rows

  // A new device: clear the search (and its tab) but keep the quick view.
  useEffect(() => {
    if (switched) setFilters({ q: '', tab: 'people', decision: '', fixable: '' }, { history: 'replace' })
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(searchText), 250)
    return () => window.clearTimeout(timer)
  }, [searchText])

  const cnicSearch = isCnicSearch(debouncedSearch)
  const apiParams = useMemo(() => {
    const identity = filters.problem ? IDENTITY_FOR_PROBLEM[filters.problem] : filters.show === 'fixing' ? 'MISSING' : undefined
    const privilege = filters.show === 'admins' ? 14 : filters.role === 'admin' ? 14 : filters.role === 'regular' ? 0 : undefined
    return {
      q: cnicSearch ? undefined : debouncedSearch.trim() || undefined,
      cnic: cnicSearch ? cnicDigits(debouncedSearch) : undefined,
      identity,
      privilege,
      added_within_days: filters.show === 'new' ? 7 : undefined,
      shift_worker: filters.shift ? 'true' : undefined,
      changing: filters.changing ? 'true' : undefined,
      sort: SORT_PARAM[filters.sort] || 'problems',
    }
  }, [cnicSearch, debouncedSearch, filters.changing, filters.problem, filters.role, filters.shift, filters.show, filters.sort])
  const paramsKey = JSON.stringify(apiParams)

  const pruneSelection = useCallback((fresh: PeopleUser[]) => {
    setSelectedKeys((current) => {
      const next = new Set(current)
      fresh.forEach((row) => { if (!isBulkSelectionEligible(row)) next.delete(row.user_key) })
      return next.size === current.size ? current : next
    })
  }, [])

  const loadList = useCallback(async (mode: 'replace' | 'append' | 'window' = 'replace') => {
    listRequest.current?.abort()
    const controller = new AbortController()
    listRequest.current = controller
    const append = mode === 'append'
    const windowLimit = mode === 'window' ? Math.min(MAX_PAGE, Math.max(PAGE, rowsRef.current.length)) : PAGE
    if (append) setLoadingMore(true)
    else setLoadingList(true)
    try {
      const params: Record<string, string | number | undefined> = { ...apiParams, limit: windowLimit }
      if (append) {
        if (nextOffset != null) params.offset = nextOffset
        else if (nextCursor != null) params.cursor = nextCursor
      }
      const response = await api<PeopleListResponse>(`/api/v2/devices/${encodeURIComponent(deviceId)}/users${queryString(params)}`, { signal: controller.signal })
      if (controller.signal.aborted || listRequest.current !== controller) return
      const fresh = response.rows || []
      fresh.forEach((row) => knownUsers.current.set(row.user_key, row))
      pruneSelection(fresh)
      if (append) {
        setRows((current) => [...current, ...fresh.filter((row) => !current.some((item) => item.user_key === row.user_key))])
        setNextOffset(response.next_offset ?? null)
      } else if (mode === 'window') {
        const freshKeys = new Set(fresh.map((row) => row.user_key))
        const before = new Map(rowsRef.current.map((row) => [row.user_key, row]))
        const changed = new Set(fresh.filter((row) => { const old = before.get(row.user_key); return old && old.row_version !== row.row_version }).map((row) => row.user_key))
        if (changed.size) setChangedKeys(changed)
        const beyond = rowsRef.current.slice(windowLimit).filter((row) => !freshKeys.has(row.user_key))
        setRows([...fresh, ...beyond])
        setNextOffset(response.next_offset == null && !beyond.length ? null : Math.max(response.next_offset ?? 0, fresh.length + beyond.length))
      } else {
        setRows(fresh)
        setNextOffset(response.next_offset ?? null)
      }
      setNextCursor(response.next_offset == null ? response.next_cursor ?? null : null)
      if (response.identity_integrity) setIntegrity(response.identity_integrity)
      setFilteredTotal(response.filtered_total ?? null)
      if (response.device) setDetail((current) => mergeDevice(current, response.device))
      setListError(null)
      setLoadedAt(new Date())
    } catch (reason) {
      if (controller.signal.aborted || (reason instanceof DOMException && reason.name === 'AbortError')) return
      setListError(reason)
    } finally {
      if (listRequest.current === controller) {
        setLoadingList(false)
        setLoadingMore(false)
      }
    }
  }, [apiParams, deviceId, nextCursor, nextOffset, pruneSelection])
  const loadListRef = useRef(loadList)
  loadListRef.current = loadList

  const loadDiagnostics = useCallback(async () => {
    diagnosticsRequest.current?.abort()
    const controller = new AbortController()
    diagnosticsRequest.current = controller
    setLoadingDiagnostics(true)
    const fail = (key: 'device' | 'identity' | 'history', reason: unknown) => {
      if (!controller.signal.aborted) setDiagnostics((current) => ({ ...current, [key]: reason }))
    }
    const clear = (key: 'device' | 'identity' | 'history') => setDiagnostics((current) => (current[key] ? { ...current, [key]: undefined } : current))
    const id = encodeURIComponent(deviceId)
    await Promise.allSettled([
      api<Device>(`/api/v1/devices/${id}`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) { setDetail((current) => mergeDevice(current, value)); clear('device') } }).catch((reason) => fail('device', reason)),
      api<IdentityConflictReport>(`/api/v2/devices/${id}/identity-conflicts`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) { setConflictReport(value); clear('identity') } }).catch((reason) => fail('identity', reason)),
      api<HistoricalIdentityReport>(`/api/v2/devices/${id}/historical-identities`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) { setHistoricalReport(value?.totals ? value : null); clear('history') } }).catch((reason) => fail('history', reason)),
      api<{ job: UserDeletionJob | null }>(`/api/v2/devices/${id}/user-deletion-jobs/latest`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setDeletionJob(value.job) }).catch(() => undefined),
    ])
    if (diagnosticsRequest.current === controller) setLoadingDiagnostics(false)
  }, [deviceId])

  const refreshWorkspace = useCallback(async () => {
    await Promise.all([loadListRef.current('window'), loadDiagnostics()])
  }, [loadDiagnostics])

  useEffect(() => {
    void loadListRef.current('replace')
    return undefined
  }, [paramsKey])

  useEffect(() => {
    void loadDiagnostics()
    return () => diagnosticsRequest.current?.abort()
  }, [loadDiagnostics])

  useEffect(() => () => listRequest.current?.abort(), [])

  useEffect(() => {
    if (revision === revisionRef.current) return
    revisionRef.current = revision
    void refreshWorkspace()
  }, [refreshWorkspace, revision])

  useEffect(() => {
    if (!changedKeys.size) return
    const timer = window.setTimeout(() => setChangedKeys(new Set()), 1200)
    return () => window.clearTimeout(timer)
  }, [changedKeys])

  // Deep link from attendance: ?user_id= opens Edit for that person (kept from today).
  useEffect(() => {
    if (deepLinkDone.current || loadingList || debouncedSearch !== requestedUserId) return
    deepLinkDone.current = true
    const found = rows.find((row) => row.user_id === requestedUserId)
    if (found) setDialog({ state: { mode: 'edit', user: found } })
    else {
      toast.error(LIST.notOnMachine(requestedUserId))
      setSearchText('')
    }
  }, [debouncedSearch, loadingList, requestedUserId, rows, toast])

  // ?q={employee id} opens the person sheet when exactly one person has that Employee ID.
  useEffect(() => {
    if (qLinkDone.current || loadingList || debouncedSearch !== requestedQuery) return
    qLinkDone.current = true
    const matches = rows.filter((row) => row.user_id === requestedQuery.trim())
    if (matches.length === 1) setSheetKey(matches[0].user_key)
  }, [debouncedSearch, loadingList, requestedQuery, rows])

  // A change already running on the machine joins the tray.
  const activeCommand = selected?.active_command
  useEffect(() => {
    if (activeCommand && !isFinalCommand(activeCommand)) tray.track(activeCommand)
  }, [activeCommand?.command_id])

  // Removing several people: follow the job until it ends.
  useEffect(() => {
    if (!deletionJob || !isActiveRemoval(deletionJob)) return
    const timeout = window.setTimeout(async () => {
      try {
        const response = await api<{ job: UserDeletionJob }>(`/api/v2/user-deletion-jobs/${deletionJob.job_id}`)
        setDeletionJob(response.job)
        if (!isActiveRemoval(response.job)) {
          setSelectedKeys(new Set())
          await Promise.all([refreshWorkspace(), refreshFleet()])
          if (response.job.status === 'SUCCEEDED') toast.notice(REMOVE_MANY.toastDone(response.job.counts.succeeded))
          else toast.error(REMOVE_MANY.toastProblems(response.job.counts.succeeded, response.job.counts.requested - response.job.counts.succeeded))
        }
      } catch {
        toast.error(REMOVE_MANY.lostTouch)
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [deletionJob, refreshFleet, refreshWorkspace, toast])

  const place = selected ? friendlyName(selected) : ''
  const zkt = selected?.zkt
  const capabilities = zkt?.capabilities || {}
  const writable = baseWritable(selected)
  const canCreate = writable && capabilities.create_user === true
  const canEditProfile = writable && capabilities.user_write === true
  const canGrantLease = writable && capabilities.admin_lease === true && !selected?.active_lease
  const canDeleteProfile = writable && capabilities.delete_user === true
  const activeDeletionJob = isActiveRemoval(deletionJob)
  const offReason = writesOff(zkt?.writes_disabled_reason).sentence
  const editCapabilityReason = !writable ? offReason : capabilities.user_write !== true ? CAPABILITY_OFF.update : ''
  const deleteCapabilityReason = !writable ? offReason : capabilities.delete_user !== true ? CAPABILITY_OFF.delete : ''
  const stepFor = useCallback((userKey: string): CommandStep | null => tray.rowSteps.get(userKey) ?? null, [tray.rowSteps])
  const rowBusy = (user: PeopleUser) => {
    const step = stepFor(user.user_key)
    return Boolean(user.current_command_state) || step === 'waiting' || step === 'sent'
  }

  const actionsFor = (user: PeopleUser): PersonActions => {
    const busy = rowBusy(user)
    const editReason = busy ? LIST.busy : user.read_only ? LIST.readOnly : editCapabilityReason
    const leaseReason = selected?.active_lease ? LIST.leaseTaken : user.privilege === 14 ? LIST.alreadyAdmin : busy ? LIST.busy : user.read_only ? LIST.readOnly : !writable ? offReason : ''
    const removeReason = user.looks_damaged ? DAMAGED_ENTRY.reason : user.privilege === 14 ? LIST.adminCantRemove : busy ? LIST.busy : user.read_only ? LIST.readOnly : activeDeletionJob ? LIST.removalRunning : deleteCapabilityReason
    return {
      edit: { allowed: canEditProfile && !user.read_only && !busy, reason: editReason },
      lease: { shown: capabilities.admin_lease === true, allowed: canGrantLease && user.privilege === 0 && !user.read_only && !busy, reason: leaseReason },
      remove: { shown: true, allowed: canDeleteProfile && user.privilege !== 14 && !user.read_only && !busy && !activeDeletionJob && !user.looks_damaged, reason: removeReason },
    }
  }

  const eligible = (user: PeopleUser) => isBulkSelectionEligible(user) && !rowBusy(user)
  const canSelect = canDeleteProfile && !activeDeletionJob
  const eligibleRows = rows.filter((user) => eligible(user) && actionsFor(user).remove.allowed)
  const selectedOnPage = eligibleRows.filter((user) => selectedKeys.has(user.user_key))
  const selectedUsers = [...selectedKeys].map((key) => knownUsers.current.get(key)).filter((user): user is PeopleUser => Boolean(user && isBulkSelectionEligible(user)))
  const allOnPageSelected = eligibleRows.length > 0 && eligibleRows.every((user) => selectedKeys.has(user.user_key))
  const selectAllRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selectedOnPage.length > 0 && selectedOnPage.length < eligibleRows.length
  }, [eligibleRows.length, selectedOnPage.length])

  const toggleUser = (user: PeopleUser, checked: boolean) => setSelectedKeys((current) => {
    const next = new Set(current)
    if (checked) next.add(user.user_key); else next.delete(user.user_key)
    return next
  })
  const toggleAll = (checked: boolean) => setSelectedKeys((current) => {
    const next = new Set(current)
    eligibleRows.forEach((user) => { if (checked) next.add(user.user_key); else next.delete(user.user_key) })
    return next
  })

  const revalidateSelection = useCallback(async (users: PeopleUser[]) => {
    const connectorId = deviceId
    if (!users.length) return { users: [] as PeopleUser[], changed: false }
    const requestedKeys = users.map((user) => user.user_key)
    const response = await api<UserSelectionValidationResponse>(`/api/v2/devices/${encodeURIComponent(connectorId)}/users/validate-selection`, {
      method: 'POST',
      body: JSON.stringify({ user_keys: requestedKeys }),
    })
    if (activeDevice.current !== connectorId) throw new Error(REMOVE_MANY.deviceChanged)
    const freshByKey = new Map(response.rows.map((user) => [user.user_key, user]))
    const eligibleUsers = requestedKeys.map((key) => freshByKey.get(key)).filter((user): user is PeopleUser => Boolean(user && isBulkSelectionEligible(user)))
    const eligibleKeys = new Set(eligibleUsers.map((user) => user.user_key))
    const changed = users.length !== eligibleUsers.length || users.some((user) => {
      const fresh = freshByKey.get(user.user_key)
      return !fresh || !eligibleKeys.has(user.user_key) || fresh.row_version !== user.row_version || fresh.display_name !== user.display_name
        || fresh.user_id !== user.user_id || fresh.privilege !== user.privilege || fresh.read_only !== user.read_only
        || fresh.current_command_state !== user.current_command_state
    })
    response.rows.forEach((user) => knownUsers.current.set(user.user_key, user))
    setSelectedKeys((current) => {
      const next = new Set(current)
      requestedKeys.forEach((key) => next.delete(key))
      eligibleUsers.forEach((user) => next.add(user.user_key))
      return next
    })
    return { users: eligibleUsers, changed }
  }, [deviceId])

  const openBulkDialog = async () => {
    if (validating || !selectedUsers.length) return
    setValidating(true)
    try {
      const validation = await revalidateSelection(selectedUsers)
      if (!validation.users.length) {
        toast.error(REMOVE_MANY.noneLeft)
        return
      }
      if (validation.changed) {
        const removed = selectedUsers.length - validation.users.length
        toast.notice(removed > 0 ? REMOVE_MANY.someLeft(removed) : REMOVE_MANY.changed)
      }
      setBulkDialogOpen(true)
    } catch (reason) {
      toast.error(reason instanceof Error && reason.message === REMOVE_MANY.deviceChanged ? REMOVE_MANY.deviceChanged : REMOVE_MANY.checkFailed)
    } finally {
      setValidating(false)
    }
  }

  const trackCommand = useCallback((command: Command, meta: CommandMeta = {}) => {
    tray.track(command, meta)
  }, [tray.track])

  const onTrayFinal = useCallback((entry: TrayEntry, command: Command) => {
    void Promise.all([refreshWorkspace(), refreshFleet()])
    if (command.type === 'PIN_TERMINAL_SERIAL') setMachineCommand(command)
    const status = String(command.status).toUpperCase()
    if (status === 'SUCCEEDED') toast.notice(CHANGES.confirmedToast(changeDone(command.type, entry.name), place))
    else if (status === 'FAILED' || status === 'EXPIRED') toast.error(CHANGES.failedToast(changeLabel(command.type, entry.name), status === 'EXPIRED' ? "The machine didn't answer in time. Nothing was changed." : changeFailure(command)))
  }, [place, refreshFleet, refreshWorkspace, toast])

  const retry = (entry: TrayEntry) => {
    const state = entry.retry as RetryState | undefined
    if (!state) return
    tray.dismiss(entry.id)
    const current = state.dialog.mode === 'create' ? null : rows.find((row) => row.user_key === (state.dialog as { user: PeopleUser }).user.user_key)
    setDialog({ state: current && state.dialog.mode !== 'create' ? { ...state.dialog, user: current } : state.dialog, draft: state.draft })
  }

  const refreshPeople = async () => {
    if (!selected?.connected || refreshing) return
    if (!refreshKey.current) refreshKey.current = idempotency('terminal-refresh')
    setRefreshing(true)
    try {
      const response = await api<Command>(`/api/v1/devices/${encodeURIComponent(deviceId)}/users/refresh`, { method: 'POST', body: JSON.stringify({ idempotency_key: refreshKey.current }) })
      refreshKey.current = ''
      trackCommand(response)
      toast.notice(HEADER.refreshQueued)
    } catch (reason) {
      toast.error(peopleErrorText(reason, HEADER.refreshFailed).sentence)
    } finally {
      setRefreshing(false)
    }
  }

  const cancelDeletionJob = async (password: string) => {
    if (!deletionJob) return
    try {
      const response = await api<{ job: UserDeletionJob }>(`/api/v2/user-deletion-jobs/${deletionJob.job_id}/cancel`, { method: 'POST', body: JSON.stringify({ password }) })
      setDeletionJob(response.job)
      toast.notice(REMOVE_MANY.toastStopping)
    } catch (reason) {
      toast.error(peopleErrorText(reason, REMOVE_MANY.stopFailed).sentence)
      throw reason
    }
  }

  const goToDevice = (id: string, event?: MouseEvent<HTMLElement>) => {
    event?.preventDefault()
    setChangeDeviceOpen(false)
    if (id === deviceId) return
    const current = new URLSearchParams(location.search)
    const kept = new URLSearchParams()
    for (const key of KEPT_ON_DEVICE_CHANGE) {
      const value = current.get(key)
      if (value) kept.set(key, value)
    }
    const query = kept.toString()
    navigate(`/people/${encodeURIComponent(id)}${query ? `?${query}` : ''}`)
  }

  const chooseTab = (tab: typeof TAB[number]) => setFilters({ tab })
  const openSameCnic = (userKey: string) => {
    setSheetKey('')
    setHighlightUserKey(userKey)
    chooseTab('shared')
    window.setTimeout(() => setHighlightUserKey(''), 1200)
  }

  const onOldAction = (candidate: HistoricalIdentityCandidate) => {
    if (candidate.resolution_path === 'ACTIVE_USER_ENRICHMENT') {
      const activeUser = rows.find((row) => row.user_key === candidate.active_user_key) || knownUsers.current.get(candidate.active_user_key || '')
      if (activeUser) setDialog({ state: { mode: 'edit', user: activeUser } })
      else toast.error(LIST.linkedMissing)
    } else if (candidate.resolution_path === 'IDENTITY_CONFLICT_REVIEW') {
      chooseTab('shared')
    } else {
      setHistoricalDialog({ candidate })
    }
  }

  // Counts and summary.
  const total = integrity?.total_users ?? null
  const fixingCount = integrity ? integrity.missing_cnic + integrity.unresolved_duplicate_users : null
  const oldRows = oldCandidates(historicalReport)
  const oldEvents = historicalReport?.totals.unresolved_events ?? 0
  const sameCount = conflictReport ? conflictReport.unresolved_groups : null
  const tabs = [
    { id: 'people' as const, label: LIST.tabs.people, count: total },
    { id: 'shared' as const, label: LIST.tabs.shared, count: sameCount },
    { id: 'old' as const, label: LIST.tabs.old, count: historicalReport ? oldRows.length : null },
  ]

  const activeFilters = [
    searchText.trim() && { id: 'q', label: isCnicSearch(searchText) ? LIST.cnicEnding(cnicDigits(searchText).slice(-4)) : LIST.quoted(searchText.trim()), onRemove: () => { setSearchText(''); setFilters({ q: '' }) } },
    filters.show && { id: 'show', label: filters.show === 'fixing' ? LIST.needsFixing : filters.show === 'admins' ? LIST.admins : LIST.addedThisWeek, onRemove: () => setFilters({ show: '' }) },
    filters.problem && { id: 'problem', label: LIST.problems[filters.problem as keyof typeof LIST.problems], onRemove: () => setFilters({ problem: '' }) },
    filters.role && { id: 'role', label: filters.role === 'admin' ? LIST.admins : LIST.regular, onRemove: () => setFilters({ role: '' }) },
    filters.shift && { id: 'shift', label: LIST.shift, onRemove: () => setFilters({ shift: false }) },
    filters.changing && { id: 'changing', label: LIST.changing, onRemove: () => setFilters({ changing: false }) },
  ].filter(Boolean) as Array<{ id: string; label: string; onRemove: () => void }>
  const clearFilters = () => {
    setSearchText('')
    setFilters({ q: '', show: '', problem: '', role: '', shift: false, changing: false })
  }
  const moreCount = [filters.problem, filters.role, filters.shift, filters.changing].filter(Boolean).length

  const cnicTyping = cnicDigits(searchText)
  const cnicHint = cnicTyping.length >= 10 && cnicTyping.length < 13

  const sheetUser = sheetKey ? rows.find((row) => row.user_key === sheetKey) || knownUsers.current.get(sheetKey) || null : null
  const sheetOld = sheetUser ? oldRows.filter((candidate) => candidate.active_user_key === sheetUser.user_key).reduce((sum, candidate) => sum + candidate.event_count, 0) : 0
  const showJob = deletionJob && jobCleared !== deletionJob.job_id
    && (isActiveRemoval(deletionJob) || !deletionJob.completed_at || Date.now() - Date.parse(deletionJob.completed_at) < FINISHED_JOB_MS)
  const firstLoad = loadingList && !rows.length && !loadedAt
  const deviceHref = `/devices/${encodeURIComponent(deviceId)}?tab=tools&advanced=1`

  const changeDeviceSheet = changeDeviceOpen ? (
      <Sheet title={HEADER.changeDevice} onClose={() => setChangeDeviceOpen(false)} initialFocus={phone ? 'close' : 'first-field'} className="people-change-device">
        <DevicePickerList
          devices={devices}
          loaded={devicesLoaded}
          error={devicesError}
          onRetry={refreshFleet}
          summary={summary.rows}
          summaryFailed={summary.failed}
          query={changeQuery}
          onQueryChange={setChangeQuery}
          show={changeShow}
          onShowChange={setChangeShow}
          spares={changeSpares}
          onSparesChange={setChangeSpares}
          division="all"
          selectedId={deviceId}
          hrefFor={(id) => `/people/${encodeURIComponent(id)}`}
          onPick={(id, event) => goToDevice(id, event)}
        />
      </Sheet>
  ) : null


  if (!selected) {
    return (
      <div className="users-workspace">
        <PageHeader title={PICKER.title} description={PICKER.description} />
        {devicesLoaded && !devicesError && !loadingDiagnostics
          ? <EmptyState icon="server" title={HEADER.changeDevice} action={<button type="button" className="button secondary" onClick={() => setChangeDeviceOpen(true)}>{HEADER.changeDevice}</button>} />
          : diagnostics.device && !devicesLoaded ? <ErrorState title={PICKER.loadFailed} error={diagnostics.device} onRetry={loadDiagnostics} /> : <Skeleton variant="block" count={2} label={PICKER.loading} />}
        {changeDeviceSheet}
      </div>
    )
  }

  const summaryParts = [
    fixingCount ? <button key="fixing" type="button" className="text-button" onClick={() => setFilters({ show: 'fixing', tab: 'people' })}>{LIST.summaryFixing(fixingCount)}</button> : null,
    integrity ? <button key="cnic" type="button" className="text-button" onClick={() => setFilters({ problem: integrity.with_cnic >= integrity.total_users ? '' : 'missing', tab: 'people' })}>{LIST.summaryCnic(integrity.with_cnic, integrity.total_users)}</button> : null,
    oldEvents ? <button key="old" type="button" className="text-button" onClick={() => chooseTab('old')}>{LIST.summaryOld(oldEvents)}</button> : null,
  ].filter(Boolean)

  const peoplePanel = (
    <section className="panel user-directory-panel" aria-labelledby="people-list-title">
      <h2 id="people-list-title" className="sr-only">{LIST.heading}</h2>
      <FilterBar
        className="people-list-filters"
        search={{ value: searchText, onChange: (value) => { setSearchText(value); setFilters({ q: value }) }, label: LIST.searchLabel }}
        chips={[
          { id: '', label: LIST.everyone },
          { id: 'fixing', label: LIST.needsFixing, count: fixingCount },
          { id: 'admins', label: LIST.admins, count: integrity?.admins ?? null },
          { id: 'new', label: LIST.addedThisWeek, count: integrity?.added_recently ?? null },
        ]}
        chipValue={filters.show}
        chipsLabel={LIST.chipsLabel}
        onChipChange={(id) => setFilters({ show: id as typeof SHOW[number] })}
        sort={{ value: filters.sort, label: LIST.sortLabel, options: (Object.keys(LIST.sorts) as Array<keyof typeof LIST.sorts>).map((id) => ({ id, label: LIST.sorts[id] })), onChange: (value) => setFilters({ sort: value as typeof SORT[number] }) }}
        moreFilters={(
          <div className="people-more-filters">
            <fieldset>
              <legend>{LIST.problem}</legend>
              {(['', 'missing', 'shared', 'typed', 'confirmed'] as const).map((id) => (
                <label key={id || 'any'} className="check-field"><input type="radio" name="people-problem" checked={filters.problem === id} onChange={() => setFilters({ problem: id })} /><span>{id ? LIST.problems[id] : LIST.everyone}</span></label>
              ))}
            </fieldset>
            <fieldset>
              <legend>{LIST.role}</legend>
              {(['', 'regular', 'admin'] as const).map((id) => (
                <label key={id || 'all'} className="check-field"><input type="radio" name="people-role" checked={filters.role === id} onChange={() => setFilters({ role: id })} /><span>{id ? LIST.roles[id] : LIST.roles.all}</span></label>
              ))}
            </fieldset>
            <label className="check-field"><input type="checkbox" checked={filters.shift} onChange={(event) => setFilters({ shift: event.target.checked })} /><span>{LIST.shift}</span></label>
            <label className="check-field"><input type="checkbox" checked={filters.changing} onChange={(event) => setFilters({ changing: event.target.checked })} /><span>{LIST.changing}</span></label>
          </div>
        )}
        moreFiltersCount={moreCount}
        active={activeFilters}
        onClearAll={clearFilters}
        actions={phone && canSelect && rows.length > 0 ? (
          <button type="button" className="button secondary" aria-pressed={selectMode} onClick={() => { if (selectMode) setSelectedKeys(new Set()); setSelectMode((value) => !value) }}>{selectMode ? LIST.done : LIST.select}</button>
        ) : undefined}
      />
      {cnicHint && <p className="people-cnic-hint">{LIST.cnicHint}</p>}
      <span className="sr-only" aria-live="polite">{!loadingList && loadedAt && searchText ? LIST.found(filteredTotal ?? rows.length) : ''}</span>
      {Boolean(listError) && rows.length > 0 && (
        <p className="people-slim-line" role="status"><Icon name="alert" size="sm" /> {LIST.refreshFailed(loadedAt ? clock(loadedAt) : '')} <button type="button" className="text-button" onClick={() => void loadList('window')}>Try again</button></p>
      )}
      {!phone && canSelect && rows.length > 0 && (
        <div className="users-selection-row">
          <label><input ref={selectAllRef} type="checkbox" checked={allOnPageSelected} disabled={validating || !eligibleRows.length} onChange={(event) => toggleAll(event.target.checked)} /><span>{LIST.selectAll}</span></label>
          <small>{selectedUsers.length === selectedOnPage.length ? LIST.selectedOfPage(selectedOnPage.length, eligibleRows.length) : `${LIST.selectedOfPage(selectedOnPage.length, eligibleRows.length)} · ${selectedUsers.length} selected in total`}</small>
          {eligibleRows.length < rows.length && <span>{LIST.cantSelect}</span>}
        </div>
      )}
      {phone && selectMode && eligibleRows.length < rows.length && <p className="people-muted">{LIST.cantSelect}</p>}
      {firstLoad && <div className="users-loading-list"><Skeleton variant="row" count={6} label={LIST.loading} /></div>}
      {Boolean(listError) && !rows.length && <ErrorState title={LIST.loadFailed} error={listError} onRetry={() => loadList('replace')} />}
      {!firstLoad && !listError && !rows.length && (
        activeFilters.length
          ? <EmptyState icon="search" title={LIST.noMatchTitle} action={<button type="button" className="button secondary" onClick={clearFilters}>{LIST.clearFilters}</button>}>{LIST.noMatchBody}</EmptyState>
          : <EmptyState icon="users" title={LIST.emptyTitle} action={canCreate ? <button type="button" className="button primary" onClick={() => setDialog({ state: { mode: 'create' } })}><Icon name="userPlus" /> {HEADER.addPerson}</button> : undefined}>{LIST.emptyBody}</EmptyState>
      )}
      {rows.length > 0 && (
        <PeopleList
          rows={rows}
          phone={phone}
          writable={writable}
          canSelect={canSelect}
          selectMode={selectMode}
          selectedKeys={selectedKeys}
          eligible={eligible}
          validating={validating}
          onToggle={toggleUser}
          actionsFor={actionsFor}
          stepFor={stepFor}
          changedKeys={changedKeys}
          onOpen={(user) => setSheetKey(user.user_key)}
          onEdit={(user) => setDialog({ state: { mode: 'edit', user } })}
          onLease={(user) => setDialog({ state: { mode: 'lease', user } })}
          onRemove={(user) => setDialog({ state: { mode: 'delete', user } })}
          busy={loadingList && rows.length > 0}
        />
      )}
      {rows.length > 0 && (
        <div className="load-more">
          {(nextOffset != null || nextCursor != null) && <button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadList('append')}>{loadingMore ? LIST.loadingMore : LIST.showMore}</button>}
          <p className="people-result-line" aria-live="polite">{LIST.showing(rows.length, filteredTotal ?? rows.length)}{selectedUsers.length > 0 ? ` · ${LIST.ticksKept}` : ''}</p>
        </div>
      )}
    </section>
  )

  return (
    <div className={`users-workspace people-workspace ${selectedUsers.length ? 'has-bulk-bar' : ''}`}>
      <PageHeader title={PICKER.title} description={PICKER.description} />
      <PeopleDeviceHeader
        device={selected}
        otherDivision={division !== 'all' && division !== divisionOf(selected)}
        canCreate={canCreate}
        onAdd={() => setDialog({ state: { mode: 'create' } })}
        onChangeDevice={() => setChangeDeviceOpen(true)}
        onRefresh={refreshPeople}
        refreshing={refreshing}
        changing={tray.entries.some((entry) => !isFinalCommand(entry.command))}
      />
      <div className="people-notices">
        <PeopleNotice device={selected} command={machineCommand} onConfirmMachine={() => setMachineDialogOpen(true)} deviceHref={deviceHref} />
        {Boolean(diagnostics.device) && <StatusFailedLine onRetry={loadDiagnostics} />}
        {selected.active_lease && <AdminAccessBanner lease={selected.active_lease} onEnd={() => setEndLeaseOpen(true)} />}
      </div>

      <p className="people-summary-line" aria-live="polite">
        {integrity ? summaryParts.flatMap((part, index) => index ? [<span key={`dot-${index}`} aria-hidden="true"> · </span>, part] : [part]) : <span>–</span>}
      </p>

      <Tabs className="section-tabs users-section-tabs" idPrefix="users" label={LIST.sectionsLabel} tabs={tabs} value={filters.tab} onChange={chooseTab} />
      <TabPanel idPrefix="users" id={filters.tab}>
        {filters.tab === 'people' && peoplePanel}
        {filters.tab === 'shared' && (
          <SameCnicTab
            report={conflictReport}
            loading={loadingDiagnostics}
            error={diagnostics.identity}
            onRetry={loadDiagnostics}
            decision={filters.decision}
            onDecisionChange={(value) => setFilters({ decision: value })}
            highlightUserKey={highlightUserKey}
            onConfirm={(group) => setResolutionDialog({ mode: 'resolve', group })}
            onUndo={(group) => setResolutionDialog({ mode: 'revoke', group })}
            onEditMember={(userKey) => {
              const user = rows.find((row) => row.user_key === userKey) || knownUsers.current.get(userKey)
              if (user) setDialog({ state: { mode: 'edit', user } })
              else toast.error(LIST.linkedMissing)
            }}
          />
        )}
        {filters.tab === 'old' && (
          <OldCheckinsTab
            report={historicalReport}
            loading={loadingDiagnostics}
            error={diagnostics.history}
            onRetry={loadDiagnostics}
            fixable={filters.fixable !== '0'}
            onFixableChange={(value) => setFilters({ fixable: value ? '' : '0' })}
            query={oldQuery}
            onQueryChange={setOldQuery}
            onAction={onOldAction}
          />
        )}
      </TabPanel>

      {selectedUsers.length > 0 && (
        <aside className="bulk-selection-bar" aria-live="polite">
          <p><strong>{REMOVE_MANY.selected(selectedUsers.length)}</strong> <span>· {REMOVE_MANY.kept}</span></p>
          <div>
            <button className="button secondary" type="button" disabled={validating} onClick={() => setSelectedKeys(new Set())}>{REMOVE_MANY.clear}</button>
            <button className="button destructive" type="button" disabled={!canDeleteProfile || activeDeletionJob || validating} onClick={() => void openBulkDialog()}><Icon name="trash" /> {validating ? REMOVE_MANY.checking : REMOVE_MANY.remove(selectedUsers.length)}</button>
          </div>
        </aside>
      )}

      <ChangesTray
        entries={tray.entries}
        revision={revision}
        onUpdate={tray.update}
        onFinal={onTrayFinal}
        onDismiss={tray.dismiss}
        onRetry={retry}
        onCancelFailed={() => toast.error(CHANGES.cancelFailed)}
        compact={phone && selectMode}
      >
        {showJob && deletionJob ? <BulkDeletionProgress job={deletionJob} deviceName={place} onCancel={cancelDeletionJob} onClear={() => setJobCleared(deletionJob.job_id)} /> : null}
      </ChangesTray>

      {sheetUser && (
        <PersonSheet
          user={sheetUser}
          actions={actionsFor(sheetUser)}
          step={stepFor(sheetUser.user_key)}
          oldCount={sheetOld}
          onClose={() => setSheetKey('')}
          onEdit={() => { setSheetKey(''); setDialog({ state: { mode: 'edit', user: sheetUser } }) }}
          onLease={() => { setSheetKey(''); setDialog({ state: { mode: 'lease', user: sheetUser } }) }}
          onRemove={() => { setSheetKey(''); setDialog({ state: { mode: 'delete', user: sheetUser } }) }}
          onOpenSameCnic={() => openSameCnic(sheetUser.user_key)}
          onOpenOld={() => { setSheetKey(''); chooseTab('old') }}
        />
      )}
      {changeDeviceSheet}
      <Suspense fallback={null}>
      {dialog && <UserOperationDialog key={JSON.stringify([dialog.state.mode, 'user' in dialog.state ? dialog.state.user.user_key : ''])} state={dialog.state} draft={dialog.draft} device={selected} onClose={() => setDialog(null)} onCommand={trackCommand} toast={toast} />}
      {bulkDialogOpen && selectedUsers.length > 0 && (
        <BulkDeletionDialog
          users={selectedUsers}
          device={selected}
          onRevalidate={async () => {
            const validation = await revalidateSelection(selectedUsers)
            if (!validation.users.length) {
              setBulkDialogOpen(false)
              toast.error(REMOVE_MANY.noneNow)
            }
            return validation
          }}
          onClose={() => setBulkDialogOpen(false)}
          onCreated={(job) => { setDeletionJob(job); setJobCleared(''); setSelectMode(false); toast.notice(REMOVE_MANY.toastStarted(job.counts.requested || selectedUsers.length)) }}
        />
      )}
      {resolutionDialog && <IdentityResolutionDialog state={resolutionDialog} device={selected} onClose={() => setResolutionDialog(null)} onComplete={(report) => { setConflictReport(report); void refreshWorkspace() }} toast={toast} />}
      {historicalDialog && <HistoricalIdentityResolutionDialog state={historicalDialog} device={selected} onClose={() => setHistoricalDialog(null)} onComplete={async () => { await Promise.all([refreshWorkspace(), refreshFleet()]) }} toast={toast} />}
      </Suspense>
      {endLeaseOpen && selected.active_lease && <EndAdminAccessDialog lease={selected.active_lease} deviceName={place} onClose={() => setEndLeaseOpen(false)} onCommand={(command) => trackCommand(command, { name: selected.active_lease?.user_display_name || undefined })} toast={toast} />}
      {machineDialogOpen && <MachineCheckDialog device={selected} place={place} onClose={() => setMachineDialogOpen(false)} onAccepted={(updated, command) => { setDetail((current) => mergeDevice(current, updated)); setMachineCommand(command); trackCommand(command) }} toast={toast} />}
    </div>
  )
}
