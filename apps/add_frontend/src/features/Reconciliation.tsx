// Recover attendance (`/recover`, 08-advanced ADV-3 … ADV-9). The page frame:
// header with a plain intro, one status line instead of metric tiles, three
// tabs (Recoveries · Unreadable records · Not in HR system yet), and the data
// for the first two. Start, pause, resume, try again, stop, hide, recover
// several and Clear history stay one click; their request bodies are unchanged.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './Reconciliation.css'
import '../ui/tabs.css'
import './recover/recover.css'
import { api, queryString } from '../api'
import { Button, Callout, DivisionScopeChip, EmptyState, ErrorState, FilterBar, PageHeader, Skeleton, useIsPhone, type ActiveFilter } from '../ui'
import { idempotency } from '../lib/idempotency'
import { clock } from '../lib/time'
import { useDivision } from '../lib/useDivision'
import type { Toast } from '../ui/Toast'
import {
  CLEAR_HISTORY,
  RECOVER_PAGE,
  STATUS_FILTER_WORDS,
  STATUS_GROUP,
  clearHistoryBanner,
  type RecoverStatusFilter,
} from '../words/recover'
import { AttendanceRecovery } from './AttendanceRecovery'
import { BulkRecoverSheet } from './recover/BulkRecoverSheet'
import { makeNamer } from './recover/names'
import { RecordChangedSheet } from './recover/RecordChangedSheet'
import { RecoveryCard, type JobAction, type JobControl } from './recover/RecoveryCard'
import { RecoveryDetails } from './recover/RecoveryDetails'
import { StartRecovery } from './recover/StartRecovery'
import { RecoverStatusLine, RecoverTabs } from './recover/StatusLine'
import { UnreadableRecordSheet } from './recover/UnreadableRecordSheet'
import { UnreadableRecords, readUnreadableFilters } from './recover/UnreadableRecords'
import { useRecoverUrl } from './recover/url'
import type {
  Device,
  HikvisionHistoryScope,
  ReconciliationBulkResponse,
  ReconciliationBulkResult,
  ReconciliationClearHistoryResult,
  ReconciliationJob,
  ReconciliationJobTotals,
  ReconciliationListResponse,
  ReconciliationPreflight,
  ReconciliationScheduler,
  SourceException,
  SourceExceptionList,
  SourceExceptionTotals,
} from '../types'

// Realtime revisions arrive on every chunk and check-in. Refetch at most once
// per interval (trailing edge), so a running recovery does not keep reloading.
function useThrottledValue<T>(value: T, intervalMs: number): T {
  const [throttled, setThrottled] = useState(value)
  const lastRef = useRef(0)
  useEffect(() => {
    if (Object.is(value, throttled)) return
    const timer = window.setTimeout(() => {
      lastRef.current = Date.now()
      setThrottled(value)
    }, Math.max(0, lastRef.current + intervalMs - Date.now()))
    return () => window.clearTimeout(timer)
  }, [value, throttled, intervalMs])
  return throttled
}

const REVISION_REFRESH_MS = 5000
// Check-ins arrive in thousands during a release; they move the "Reached the HR
// system" figures only slowly, so they refresh this page at most every 15 s.
const ATTENDANCE_REVISION_REFRESH_MS = 15_000
const JOB_PAGE = 40
const BULK_REQUEST_LIMIT = 100
const STATUS_FILTERS: RecoverStatusFilter[] = ['attention', 'running', 'waiting', 'paused', 'done', 'stopped']
const TOTAL_KEY: Record<RecoverStatusFilter, keyof ReconciliationJobTotals> = {
  attention: 'attention', running: 'active', waiting: 'queued_waiting', paused: 'paused', done: 'completed', stopped: 'cancelled',
}

// A refresh re-reads the loaded window, newest first by job id. Rows older than
// the refreshed window stay as loaded, so "Show older" rows never vanish and
// nothing is reordered; rows missing inside the window were hidden or deleted.
const mergeJobWindow = (current: ReconciliationJob[], fresh: ReconciliationJob[], windowTruncated: boolean) => {
  if (!windowTruncated || !fresh.length) return { rows: fresh, keptTail: false }
  const freshIds = new Set(fresh.map((row) => row.job_id))
  const boundary = current.findIndex((row) => row.job_id === fresh[fresh.length - 1].job_id)
  const tail = boundary < 0 ? [] : current.slice(boundary + 1).filter((row) => !freshIds.has(row.job_id))
  return { rows: [...fresh, ...tail], keptTail: tail.length > 0 }
}

const isAbort = (reason: unknown) => reason instanceof DOMException && reason.name === 'AbortError'
const errorText = (error: unknown, fallback: string) => (error instanceof Error && error.message ? error.message : fallback)

export function ReconciliationView({
  devices,
  revision,
  attendanceRevision = 0,
  toast,
}: {
  devices: Device[]
  /** Recovery (reconciliation) live events. */
  revision: number
  /** Check-in live events, throttled apart (ADV-24). */
  attendanceRevision?: number
  toast: Toast
}) {
  const { params, tab, update, setTab } = useRecoverUrl()
  const [division, setDivision] = useDivision()
  const phone = useIsPhone()
  const namer = useMemo(() => makeNamer(devices), [devices])
  const divisionParam = division === 'all' ? undefined : division

  // --- Recoveries list ----------------------------------------------------
  const statusParam = params.get('status') as RecoverStatusFilter | null
  const statusFilter = statusParam && STATUS_FILTERS.includes(statusParam) ? statusParam : null
  const zoneFilter = params.get('device') || ''
  const queryFilter = params.get('q') || ''
  const [jobSearch, setJobSearch] = useState(queryFilter)
  useEffect(() => setJobSearch(queryFilter), [queryFilter])
  useEffect(() => {
    if (jobSearch === queryFilter) return
    const timer = window.setTimeout(() => update({ q: jobSearch.trim() || null }, { replace: true }), 250)
    return () => window.clearTimeout(timer)
  }, [jobSearch, queryFilter, update])

  const [rows, setRows] = useState<ReconciliationJob[]>([])
  const [scheduler, setScheduler] = useState<ReconciliationScheduler | null>(null)
  const [jobTotals, setJobTotals] = useState<ReconciliationJobTotals | null>(null)
  const [jobCursor, setJobCursor] = useState<number | null>(null)
  const [jobFilteredTotal, setJobFilteredTotal] = useState(0)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [jobLoading, setJobLoading] = useState(true)
  const [jobLoadingMore, setJobLoadingMore] = useState(false)
  const [jobError, setJobError] = useState<unknown>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const jobAbortRef = useRef<AbortController | null>(null)
  const rowsRef = useRef<ReconciliationJob[]>([])
  const jobCursorRef = useRef<number | null>(null)
  useEffect(() => {
    rowsRef.current = rows
    jobCursorRef.current = jobCursor
  }, [rows, jobCursor])

  // --- Start a recovery ---------------------------------------------------
  const [selectedId, setSelectedId] = useState('')
  const [deviceDetail, setDeviceDetail] = useState<Device | null>(null)
  const [preflight, setPreflight] = useState<ReconciliationPreflight | null>(null)
  const [preflightLoading, setPreflightLoading] = useState(false)
  const [preflightError, setPreflightError] = useState(false)
  const [historyScope, setHistoryScope] = useState<HikvisionHistoryScope>('ALL_RECORDS')
  const [startedFor, setStartedFor] = useState<string | null>(null)
  const preflightAbortRef = useRef<AbortController | null>(null)

  // --- Sheets, busy state, banners ---------------------------------------
  const [jobSheet, setJobSheet] = useState<ReconciliationJob | null>(null)
  const jobSheetReturnRef = useRef<HTMLElement | null>(null)
  const [divergenceId, setDivergenceId] = useState<string | null>(null)
  const [busy, setBusy] = useState<Set<string>>(() => new Set())
  const [bulkOpen, setBulkOpen] = useState(false)
  const [clearBanner, setClearBanner] = useState<{ text: string; more: boolean } | null>(null)
  const [highlight, setHighlight] = useState<string | null>(null)
  const [liveStatus, setLiveStatus] = useState('')

  // --- Unreadable records -------------------------------------------------
  const exceptionFilters = useMemo(() => readUnreadableFilters(params), [params])
  const exceptionKey = JSON.stringify(exceptionFilters)
  const [exceptionRows, setExceptionRows] = useState<SourceException[]>([])
  const [exceptionTotals, setExceptionTotals] = useState<SourceExceptionTotals | null>(null)
  const [exceptionScope, setExceptionScope] = useState<SourceExceptionList['scope'] | null>(null)
  const [exceptionFilteredTotal, setExceptionFilteredTotal] = useState(0)
  const [exceptionCursor, setExceptionCursor] = useState<number | null>(null)
  const [exceptionLoading, setExceptionLoading] = useState(true)
  const [exceptionLoadingMore, setExceptionLoadingMore] = useState(false)
  const [exceptionError, setExceptionError] = useState<unknown>(null)
  const [recordSheet, setRecordSheet] = useState<SourceException | null>(null)
  const exceptionAbortRef = useRef<AbortController | null>(null)
  const [heldRefresh, setHeldRefresh] = useState(0)

  const refreshRevision = useThrottledValue(revision, REVISION_REFRESH_MS)
    + useThrottledValue(attendanceRevision, ATTENDANCE_REVISION_REFRESH_MS)

  const markBusy = (key: string, on: boolean) => setBusy((current) => {
    const next = new Set(current)
    if (on) next.add(key)
    else next.delete(key)
    return next
  })

  const loadJobs = useCallback(async ({ cursor, append = false, quiet = false, refresh = false }: { cursor?: number; append?: boolean; quiet?: boolean; refresh?: boolean } = {}) => {
    if (append) setJobLoadingMore(true)
    else if (!quiet) setJobLoading(true)
    jobAbortRef.current?.abort()
    const controller = new AbortController()
    jobAbortRef.current = controller
    // A refresh re-reads the whole loaded window instead of page 1, so the
    // rows the person paged in stay put.
    const limit = refresh ? Math.min(500, Math.max(JOB_PAGE, rowsRef.current.length)) : JOB_PAGE
    try {
      const response = await api<ReconciliationListResponse>(
        `/api/v1/reconciliations${queryString({ q: queryFilter.trim() || undefined, zone_id: zoneFilter || undefined, status_group: statusFilter ? STATUS_GROUP[statusFilter] : undefined, division: divisionParam, cursor, limit })}`,
        { signal: controller.signal },
      )
      let nextCursor = response.next_cursor
      if (append) {
        setRows((current) => [...new Map([...current, ...response.rows].map((row) => [row.job_id, row])).values()])
      } else if (refresh) {
        const merged = mergeJobWindow(rowsRef.current, response.rows, response.next_cursor != null)
        if (merged.keptTail) nextCursor = jobCursorRef.current
        rowsRef.current = merged.rows
        setRows(merged.rows)
      } else {
        setRows(response.rows)
      }
      setEnabled(response.enabled)
      setScheduler(response.scheduler || null)
      setJobTotals(response.totals || null)
      setJobCursor(nextCursor)
      setJobFilteredTotal(response.filtered_total)
      setJobError(null)
      setUpdatedAt(Date.now())
    } catch (reason) {
      if (!isAbort(reason)) setJobError(reason)
    } finally {
      if (jobAbortRef.current === controller) {
        setJobLoading(false)
        setJobLoadingMore(false)
      }
    }
  }, [divisionParam, queryFilter, statusFilter, zoneFilter])

  const loadExceptions = useCallback(async ({ cursor, append = false, quiet = false }: { cursor?: number; append?: boolean; quiet?: boolean } = {}) => {
    if (append) setExceptionLoadingMore(true)
    else if (!quiet) setExceptionLoading(true)
    exceptionAbortRef.current?.abort()
    const controller = new AbortController()
    exceptionAbortRef.current = controller
    const filters = JSON.parse(exceptionKey) as ReturnType<typeof readUnreadableFilters>
    try {
      const response = await api<SourceExceptionList>(
        `/api/v1/source-exceptions${queryString({
          job_id: filters.job_id || undefined,
          device_id: filters.device_id || undefined,
          disposition: filters.problem ? { time: 'INVALID_TIME', damaged: 'MALFORMED', unmatched: 'IDENTITY_UNRESOLVED' }[filters.problem] : undefined,
          review_state: filters.review === 'open' ? 'OPEN' : filters.review === 'reviewed' ? 'REVIEWED' : undefined,
          error_code: filters.code || undefined,
          ordinal: filters.record || undefined,
          corrected: filters.corrected ? true : undefined,
          division: divisionParam,
          cursor,
          limit: 50,
        })}`,
        { signal: controller.signal },
      )
      setExceptionRows((current) => (append ? [...new Map([...current, ...response.rows].map((row) => [row.id, row])).values()] : response.rows))
      setExceptionTotals(response.totals)
      setExceptionScope(response.scope || null)
      setExceptionFilteredTotal(response.filtered_total ?? response.rows.length)
      setExceptionCursor(response.next_cursor)
      setExceptionError(null)
    } catch (reason) {
      if (!isAbort(reason)) setExceptionError(reason)
    } finally {
      if (exceptionAbortRef.current === controller) {
        setExceptionLoading(false)
        setExceptionLoadingMore(false)
      }
    }
  }, [divisionParam, exceptionKey])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadJobs(), 250)
    return () => window.clearTimeout(timer)
  }, [loadJobs])
  useEffect(() => { void loadExceptions() }, [loadExceptions])
  // Revisions refresh in place; the first loads above are the mount's only
  // loads, so a refresh never races them and replaces rows paged in.
  const listRevisionRef = useRef(refreshRevision)
  useEffect(() => {
    if (listRevisionRef.current === refreshRevision) return
    listRevisionRef.current = refreshRevision
    if (tab === 'recoveries') void loadJobs({ quiet: true, refresh: true })
    else if (tab === 'unreadable') void loadExceptions({ quiet: true })
  }, [refreshRevision]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => {
    jobAbortRef.current?.abort()
    exceptionAbortRef.current?.abort()
    preflightAbortRef.current?.abort()
  }, [])

  const loadPreflight = useCallback(async (id: string, quiet: boolean) => {
    preflightAbortRef.current?.abort()
    if (!id) return
    const controller = new AbortController()
    preflightAbortRef.current = controller
    if (!quiet) setPreflightLoading(true)
    const [detail, check] = await Promise.allSettled([
      api<Device>(`/api/v1/devices/${id}`, { signal: controller.signal }),
      api<ReconciliationPreflight>(`/api/v1/devices/${id}/reconciliations/preflight`, { signal: controller.signal }),
    ])
    if (controller.signal.aborted) return
    // A quiet refresh swaps new data in and keeps the old answer on failure.
    if (detail.status === 'fulfilled') setDeviceDetail(detail.value)
    if (check.status === 'fulfilled') {
      setPreflight(check.value)
      setPreflightError(false)
    } else if (!quiet) setPreflightError(true)
    setPreflightLoading(false)
  }, [])
  // Only a new device clears the answer; revisions refresh it in place.
  useEffect(() => {
    setDeviceDetail(null)
    setPreflight(null)
    setPreflightError(false)
    setPreflightLoading(false)
    setStartedFor(null)
    void loadPreflight(selectedId, false)
  }, [selectedId, loadPreflight])
  const preflightRevisionRef = useRef(refreshRevision)
  useEffect(() => {
    if (preflightRevisionRef.current === refreshRevision) return
    preflightRevisionRef.current = refreshRevision
    if (selectedId) void loadPreflight(selectedId, true)
  }, [refreshRevision]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!highlight) return
    const timer = window.setTimeout(() => setHighlight(null), 1200)
    return () => window.clearTimeout(timer)
  }, [highlight])

  const failure = (error: unknown, fallback: string) => toast.error(errorText(error, fallback))
  const refreshJobsAndReadiness = async () => {
    await Promise.all([
      loadJobs({ quiet: true, refresh: true }),
      selectedId ? loadPreflight(selectedId, true) : Promise.resolve(),
    ])
  }
  const refreshPage = async () => {
    setRefreshing(true)
    try {
      if (tab === 'recoveries') await refreshJobsAndReadiness()
      else if (tab === 'unreadable') await loadExceptions({ quiet: true })
      else setHeldRefresh((value) => value + 1)
    } finally {
      setRefreshing(false)
    }
  }

  const selected = devices.find((device) => device.connector_id === selectedId) || null
  const showJob = (jobId?: string | null) => {
    const target = jobId
      || preflight?.hard_blockers.map((item) => /([0-9a-f]{8}-[0-9a-f-]{27,})/i.exec(item.message)?.[1]).find(Boolean)
      || rows.find((row) => row.connector?.connector_id === selectedId && !['COMPLETED', 'CANCELLED', 'FAILED', 'INVALIDATED'].includes(row.status))?.job_id
    if (!target) return
    if (tab !== 'recoveries') setTab('recoveries')
    setHighlight(target)
    window.requestAnimationFrame(() => {
      const node = document.querySelector<HTMLElement>(`[data-job-id="${target}"]`)
      const reduce = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      node?.scrollIntoView?.({ block: 'center', behavior: reduce ? 'auto' : 'smooth' })
    })
  }

  const start = async () => {
    if (!selected) return
    const scope = selected.firmware_family === 'hikvision' ? historyScope : 'ALL_RECORDS'
    markBusy('start', true)
    try {
      const created = await api<ReconciliationJob>(`/api/v1/devices/${selected.connector_id}/reconciliations/full-history`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: idempotency('full-history'), scope }),
      })
      const place = namer(selected)
      const message = `Recovery started for ${place.name} (${place.division === 'life' ? 'Life' : 'Health'}).`
      toast.notice(message)
      setLiveStatus(message)
      setStartedFor(selected.connector_id)
      await refreshJobsAndReadiness()
      if (created?.job_id) setHighlight(created.job_id)
    } catch (error) {
      failure(error, "Couldn't start the recovery.")
    } finally {
      markBusy('start', false)
    }
  }

  const control = async (job: ReconciliationJob, action: JobControl) => {
    const key = `${job.job_id}:${action}`
    markBusy(key, true)
    try {
      const updated = await api<ReconciliationJob>(`/api/v1/reconciliations/${job.job_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: idempotency(`reconcile-${action}`) }),
      })
      setRows((current) => current.map((row) => (row.job_id === updated.job_id ? { ...row, ...updated } : row)))
      const name = namer(job.connector).name
      const words: Record<JobControl, string> = { pause: `Paused at ${name}.`, resume: `Resumed at ${name}.`, cancel: `Stopped at ${name}. Check-ins already copied are kept.`, retry: `Trying again at ${name}.` }
      toast.notice(words[action])
      setLiveStatus(words[action])
      setHighlight(job.job_id)
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, "That didn't work. Try again.")
    } finally {
      markBusy(key, false)
    }
  }

  const archive = async (job: ReconciliationJob, cancelFirst: boolean) => {
    const action: JobAction = cancelFirst ? 'cancel-and-archive' : 'archive'
    const key = `${job.job_id}:${action}`
    markBusy(key, true)
    try {
      await api<ReconciliationJob>(`/api/v1/reconciliations/${job.job_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: idempotency(`reconcile-${action}`) }),
      })
      // Remove the card in place; the quiet refresh then fills the window.
      setRows((current) => {
        const next = current.filter((row) => row.job_id !== job.job_id)
        rowsRef.current = next
        return next
      })
      setJobFilteredTotal((total) => Math.max(0, total - 1))
      const message = cancelFirst ? 'Stopped and hidden from the list.' : 'Hidden from the list.'
      toast.notice(message)
      setLiveStatus(message)
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, "That didn't work. Try again.")
    } finally {
      markBusy(key, false)
    }
  }

  const startBulk = async (connectorIds: string[], progress: (done: number, total: number) => void): Promise<ReconciliationBulkResult[]> => {
    markBusy('bulk', true)
    const results: ReconciliationBulkResult[] = []
    try {
      for (let index = 0; index < connectorIds.length; index += BULK_REQUEST_LIMIT) {
        progress(index, connectorIds.length)
        const response = await api<ReconciliationBulkResponse>('/api/v1/reconciliations/bulk', {
          method: 'POST',
          body: JSON.stringify({
            connector_ids: connectorIds.slice(index, index + BULK_REQUEST_LIMIT),
            scope: 'ALL_RECORDS',
            idempotency_key: idempotency('reconcile-bulk'),
          }),
        })
        results.push(...response.results)
      }
      progress(connectorIds.length, connectorIds.length)
      const created = results.filter((row) => row.outcome === 'CREATED').length
      const message = `${created === 1 ? '1 recovery' : `${created} recoveries`} started; ${results.length - created} not started.`
      toast.notice(message)
      setLiveStatus(message)
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, "Couldn't start the recoveries.")
    } finally {
      markBusy('bulk', false)
    }
    return results
  }

  const clearHistory = async () => {
    markBusy('clear', true)
    setClearBanner(null)
    try {
      const result = await api<ReconciliationClearHistoryResult>('/api/v1/reconciliations/clear-history', {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: idempotency('reconcile-clear-history') }),
      })
      rowsRef.current = []
      setRows([])
      setJobCursor(null)
      const running = Object.values(result.kept_running || {}).reduce((total, count) => total + count, 0)
      setClearBanner({ text: clearHistoryBanner(result, running), more: Boolean(result.busy) })
      await refreshJobsAndReadiness()
    } catch (error) {
      const status = (error as { status?: number } | null)?.status
      if (status === 503) setClearBanner({ text: 'Some recoveries were busy. Press Clear history again to finish them.', more: true })
      else failure(error, "Couldn't clear the history.")
    } finally {
      markBusy('clear', false)
    }
  }

  const openRecords = (job: ReconciliationJob) => {
    setJobSheet(null)
    setTab('unreadable', { job_id: job.job_id, device_id: job.connector?.connector_id || null })
  }

  // Queue position among jobs waiting for or using a scan slot (oldest first).
  const queuePositions = useMemo(() => {
    const queue = rows
      .filter((job) => !job.capture_certified_at && ['QUEUED', 'RUNNING'].includes(job.status))
      .sort((left, right) => left.requested_at.localeCompare(right.requested_at))
    return new Map(queue.filter((job) => job.status === 'QUEUED').map((job, index) => [job.job_id, index + 1]))
  }, [rows])

  const statusChips = STATUS_FILTERS.map((id) => ({ id, label: STATUS_FILTER_WORDS[id], count: jobTotals ? jobTotals[TOTAL_KEY[id]] : null }))
  const zones = useMemo(() => {
    const seen = new Map<string, Device>()
    devices.forEach((device) => { if (device.zone_id && !device.is_spare && !seen.has(device.zone_id)) seen.set(device.zone_id, device) })
    return [...seen.values()].map((device) => ({ zone: device.zone_id, place: namer(device) }))
      .sort((left, right) => (left.place.division === right.place.division ? 0 : left.place.division === 'life' ? -1 : 1) || left.place.name.localeCompare(right.place.name, undefined, { numeric: true }))
  }, [devices, namer])
  const activeJobFilters: ActiveFilter[] = []
  if (statusFilter) activeJobFilters.push({ id: 'status', label: STATUS_FILTER_WORDS[statusFilter], onRemove: () => update({ status: null }) })
  if (zoneFilter) activeJobFilters.push({ id: 'device', label: `Device: ${zones.find((item) => item.zone === zoneFilter)?.place.name || 'chosen device'}`, onRemove: () => update({ device: null }) })
  if (queryFilter) activeJobFilters.push({ id: 'q', label: `Search: ${queryFilter}`, onRemove: () => { setJobSearch(''); update({ q: null }) } })
  const clearJobFilters = () => { setJobSearch(''); update({ status: null, device: null, q: null }) }
  const runningCount = jobTotals?.active ?? 0
  const sheetJobPlace = jobSheet ? namer({ ...jobSheet.connector, ...(jobSheet as ReconciliationJob & { friendly_name?: string; division?: string }) }) : null
  const recordIndex = recordSheet ? exceptionRows.findIndex((row) => row.id === recordSheet.id) : -1
  const jobStartedAt = exceptionScope ? rows.find((row) => row.job_id === exceptionScope.job_id)?.requested_at ?? null : null

  const severalButton = (
    <Button icon="list" aria-haspopup="dialog" onClick={() => setBulkOpen(true)}>{RECOVER_PAGE.several}</Button>
  )
  const refreshButton = (
    <Button icon="refresh" busy={refreshing} busyLabel={RECOVER_PAGE.refreshing} onClick={() => refreshPage()}>
      {updatedAt ? RECOVER_PAGE.updatedAt(clock(updatedAt)) : RECOVER_PAGE.refresh}
    </Button>
  )

  return (
    <div className="recover-page">
      <PageHeader
        title={RECOVER_PAGE.title}
        primaryAction={phone ? undefined : <div className="recover-header-actions">{refreshButton}{severalButton}</div>}
        moreActions={phone ? [
          { id: 'several', label: RECOVER_PAGE.several, icon: 'list', onSelect: () => setBulkOpen(true) },
          { id: 'refresh', label: RECOVER_PAGE.refresh, icon: 'refresh', onSelect: () => void refreshPage() },
        ] : undefined}
      >
        <p className="recover-intro">{phone ? RECOVER_PAGE.introPhone : RECOVER_PAGE.intro}</p>
        <DivisionScopeChip division={division} onShowAll={() => setDivision('all')} />
      </PageHeader>
      <RecoverStatusLine
        totals={jobTotals}
        scheduler={scheduler}
        enabled={enabled}
        error={jobError}
        onRetry={() => loadJobs()}
        onFilter={(filter) => setTab('recoveries', { status: filter })}
      />
      <RecoverTabs
        value={tab}
        phone={phone}
        onChange={(next) => setTab(next)}
        counts={{
          recoveries: jobTotals ? jobTotals.attention : jobError ? undefined : null,
          unreadable: exceptionTotals ? exceptionTotals.open : exceptionError ? undefined : null,
        }}
      />
      <p className="sr-only" role="status" aria-live="polite">{liveStatus}</p>

      {tab === 'held' && (
        <div role="tabpanel" id="recover-panel-held" aria-labelledby="recover-tab-held">
          <AttendanceRecovery revision={refreshRevision} refreshToken={heldRefresh} toast={toast} onShowUnreadable={() => setTab('unreadable')} />
        </div>
      )}

      {tab === 'recoveries' && (
        <div role="tabpanel" id="recover-panel-recoveries" aria-labelledby="recover-tab-recoveries">
          {enabled !== false && (
            <StartRecovery
              devices={devices}
              namer={namer}
              division={division}
              enabled
              selectedId={selectedId}
              onSelect={setSelectedId}
              preflight={preflight}
              preflightLoading={preflightLoading}
              preflightError={preflightError}
              onRetryPreflight={() => void loadPreflight(selectedId, false)}
              deviceDetail={deviceDetail}
              scope={historyScope}
              onScope={setHistoryScope}
              starting={busy.has('start')}
              started={startedFor === selectedId && Boolean(selectedId)}
              onStart={() => void start()}
              onShowIt={() => showJob()}
            />
          )}
          <section className="recover-list" aria-labelledby="recover-list-title">
            <div className="recover-list-head">
              <div>
                <h2 id="recover-list-title">Recoveries</h2>
                <p>Newest first. Shows how much was read from each machine and how much reached the HR system.</p>
              </div>
              <div className="recover-clear">
                <Button variant="danger-outline" icon="trash" busy={busy.has('clear')} busyLabel={CLEAR_HISTORY.busy} onClick={() => clearHistory()}>
                  {CLEAR_HISTORY.label}
                </Button>
                <small>{CLEAR_HISTORY.helper}</small>
              </div>
            </div>
            {clearBanner && (
              <Callout
                tone="info"
                role="status"
                className="recover-banner"
                action={(
                  <span className="recover-banner-actions">
                    {clearBanner.more && <Button size="sm" busy={busy.has('clear')} busyLabel={CLEAR_HISTORY.busy} onClick={() => clearHistory()}>{CLEAR_HISTORY.again}</Button>}
                    <Button size="sm" variant="ghost" aria-label="Close message" onClick={() => setClearBanner(null)}>Close</Button>
                  </span>
                )}
              >
                {clearBanner.text}
              </Callout>
            )}
            <FilterBar
              search={{ value: jobSearch, onChange: setJobSearch, label: 'Search recoveries', placeholder: 'Search by place, device or serial number' }}
              chips={[{ id: 'all', label: 'All', count: jobTotals ? jobTotals.all : null }, ...statusChips]}
              chipValue={statusFilter || 'all'}
              chipsLabel="Status"
              onChipChange={(id) => update({ status: id === 'all' ? null : id })}
              moreFilters={(
                <label className="recover-more-filters">
                  <span>Device</span>
                  <select value={zoneFilter} onChange={(event) => update({ device: event.target.value || null })}>
                    <option value="">All devices</option>
                    {(['life', 'health'] as const).map((group) => (
                      <optgroup key={group} label={group === 'life' ? 'Life' : 'Health'}>
                        {zones.filter((item) => item.place.division === group).map((item) => <option key={item.zone} value={item.zone}>{item.place.name}</option>)}
                      </optgroup>
                    ))}
                  </select>
                </label>
              )}
              moreFiltersCount={zoneFilter ? 1 : 0}
              active={activeJobFilters}
              onClearAll={clearJobFilters}
            />
            {Boolean(jobError) && rows.length > 0 && (
              <ErrorState compact title="Couldn't refresh the list." message="Showing the last list we had." error={jobError} onRetry={() => loadJobs()} />
            )}
            {jobLoading && !rows.length && <Skeleton variant="block" count={3} label="Loading the list" className="recover-card-skeleton" />}
            <div className="recover-cards" data-loading={jobLoading && rows.length ? '' : undefined}>
              {rows.map((job) => (
                <RecoveryCard
                  key={job.job_id}
                  job={job}
                  place={namer({ ...job.connector, ...(job as ReconciliationJob & { friendly_name?: string; division?: string }) })}
                  queuePosition={queuePositions.get(job.job_id)}
                  running={runningCount}
                  busy={busy}
                  highlighted={highlight === job.job_id}
                  onControl={control}
                  onArchive={archive}
                  onReview={openRecords}
                  onDetails={(item, trigger) => {
                    jobSheetReturnRef.current = trigger || (document.activeElement as HTMLElement | null)
                    setJobSheet(item)
                  }}
                  onShowHeld={() => setTab('held')}
                />
              ))}
              {!jobLoading && !rows.length && !jobError && (
                activeJobFilters.length
                  ? <EmptyState icon="search" title="No recoveries match your filters." action={<Button onClick={clearJobFilters}>Clear all</Button>} />
                  : <EmptyState icon="refresh" title="No recoveries yet.">Choose a device above and press Start recovery.</EmptyState>
              )}
              {!jobLoading && !rows.length && Boolean(jobError) && jobTotals && (
                <ErrorState compact title="Couldn't refresh the list." error={jobError} onRetry={() => loadJobs()} />
              )}
            </div>
            {jobCursor && (
              <div className="recover-more">
                <Button busy={jobLoadingMore} busyLabel="Loading…" onClick={() => loadJobs({ cursor: jobCursor, append: true, quiet: true })}>Show older recoveries</Button>
                <small>{`Showing ${rows.length.toLocaleString('en-PK')} of ${jobFilteredTotal.toLocaleString('en-PK')}`}</small>
              </div>
            )}
          </section>
        </div>
      )}

      {tab === 'unreadable' && (
        <div role="tabpanel" id="recover-panel-unreadable" aria-labelledby="recover-tab-unreadable">
          <UnreadableRecords
            filters={exceptionFilters}
            onFilters={update}
            rows={exceptionRows}
            totals={exceptionTotals}
            scope={exceptionScope}
            filteredTotal={exceptionFilteredTotal}
            hasMore={Boolean(exceptionCursor)}
            loading={exceptionLoading}
            loadingMore={exceptionLoadingMore}
            error={exceptionError}
            devices={devices}
            namer={namer}
            jobStartedAt={jobStartedAt}
            onRetry={() => loadExceptions()}
            onLoadMore={() => (exceptionCursor ? loadExceptions({ cursor: exceptionCursor, append: true, quiet: true }) : undefined)}
            onOpen={(row) => setRecordSheet(row)}
          />
        </div>
      )}

      {jobSheet && sheetJobPlace && (
        <RecoveryDetails
          seed={jobSheet}
          place={sheetJobPlace}
          returnFocusRef={jobSheetReturnRef}
          onClose={() => setJobSheet(null)}
          onReview={openRecords}
          onRecordChanged={(id) => {
            setJobSheet(null)
            setDivergenceId(id)
          }}
        />
      )}
      {recordSheet && (
        <UnreadableRecordSheet
          seed={recordSheet}
          namer={namer}
          remaining={exceptionScope?.source_exception_assurance ? exceptionScope.source_exception_assurance.open : null}
          hasNext={recordIndex >= 0 && recordIndex < exceptionRows.length - 1}
          onNext={() => setRecordSheet(exceptionRows[recordIndex + 1] || null)}
          onClose={() => setRecordSheet(null)}
          onChanged={async () => {
            await Promise.all([loadExceptions({ quiet: true }), loadJobs({ quiet: true })])
          }}
          toast={toast}
        />
      )}
      {divergenceId && <RecordChangedSheet divergenceId={divergenceId} onClose={() => setDivergenceId(null)} toast={toast} />}
      {bulkOpen && <BulkRecoverSheet devices={devices} namer={namer} onStart={startBulk} onClose={() => setBulkOpen(false)} />}
    </div>
  )
}
