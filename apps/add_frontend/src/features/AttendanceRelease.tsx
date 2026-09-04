import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { api, queryString } from '../api'
import {
  Dialog,
  Metric,
  StatusBadge,
  dateTime,
  idempotency,
  relativeTime,
} from '../App'
import { Icon } from '../Icon'
import type {
  AttendanceReleaseCandidate,
  AttendanceReleaseCandidates,
  AttendanceReleaseQueueResponse,
  AttendanceReleaseQueueRow,
  AttendanceRepairJob,
  AttendanceRepairItemOutcome,
  AttendanceRepairListResponse,
  Device,
} from '../types'

type Toast = {
  notice: (message: string) => void
  error: (message: string) => void
}
type ControlAction = 'pause' | 'resume' | 'cancel' | 'retry'

const activeStates = new Set([
  'PREPARING_SOURCE',
  'AWAITING_APPROVAL',
  'QUEUED',
  'RUNNING',
  'WAITING_ORACLE',
  'WAITING_DOWNSTREAM',
  'PAUSED',
  'NEEDS_ATTENTION',
])

const pakistanDay = new Intl.DateTimeFormat('en-PK', {
  timeZone: 'Asia/Karachi',
  dateStyle: 'full',
})

const humanize = (value?: string | null) =>
  (value || 'Unknown')
    .replaceAll('_', ' ')
    .toLowerCase()
    .replace(/^./, (letter) => letter.toUpperCase())

const lockCopy: Record<string, string> = {
  PREVIEW_DISABLED: 'Release review is not enabled yet.',
  EXECUTION_DISABLED: 'Release execution is paused by the rollout gate.',
  CONNECTOR_INACTIVE: 'This connector is inactive.',
  NO_CONNECTOR: 'No connector owns this terminal.',
  NO_TERMINAL: 'The terminal record is unavailable.',
  TARGET_NOT_ACTIVE: 'The employee is not active in the latest terminal directory.',
  TARGET_CNIC_MISSING: 'Add a valid CNIC before releasing punches.',
  TARGET_CNIC_INVALID: 'The saved CNIC is invalid.',
  TARGET_CNIC_UNREADABLE: 'The protected CNIC cannot be verified.',
  TARGET_NAME_MISSING: 'The current employee name is missing.',
  TARGET_NOT_IN_CURRENT_SNAPSHOT: 'The employee is not in the latest complete snapshot.',
  TARGET_SNAPSHOT_UNSTABLE: 'The latest terminal employee snapshot is not stable.',
  IDENTITY_SNAPSHOT_UNSTABLE: 'The latest terminal employee snapshot is not stable.',
  TARGET_DUPLICATE_CNIC_UNRESOLVED: 'Resolve the duplicate CNIC identity conflict first.',
  TARGET_IDENTITY_AMBIGUOUS: 'The captured identity maps to more than one employee.',
  SOURCE_IDENTITY_AMBIGUOUS: 'A competing active identity owns this UID or user ID.',
  SOURCE_IDENTITY_UNPROVEN: 'The capture cannot be safely linked to this employee.',
  REUSE_SOURCE_UNPROVEN: 'Identity-reuse ownership is not supported by current evidence.',
  REUSE_NAME_MISMATCH: 'The captured name does not exactly match the current employee.',
  SOURCE_NAME_MISMATCH: 'The captured and current employee names differ.',
  CLOCK_NOT_OK: 'The terminal clock was not valid for this punch.',
  SOURCE_RECERTIFICATION_REQUIRED: 'A fresh complete terminal source certificate is required.',
  LIVE_ORDS_BACKLOG_HIGH: 'Live Oracle delivery must catch up before new releases.',
  RELEASE_ALREADY_IN_PROGRESS: 'This punch already belongs to an active release.',
  TERMINAL_RELEASE_IN_PROGRESS: 'Another release is active for this terminal.',
  ALREADY_RELEASED: 'This punch is already Oracle and downstream verified.',
}

const explainLock = (code?: string | null) =>
  code ? lockCopy[code] || humanize(code) : 'This punch cannot be released safely.'

const outcomeDetail = (item: AttendanceRepairItemOutcome) => {
  if (item.error_message) return item.error_message
  if (
    item.downstream_status === 'VERIFIED' ||
    item.downstream_verified_at ||
    item.outcome?.includes('DOWNSTREAM_VERIFIED')
  ) return 'Oracle content and downstream attendance are verified.'
  if (item.error_code) return explainLock(item.error_code)
  return humanize(item.state)
}

const punchCopy = (punch?: string | null) => {
  if (punch === '0') return 'Check in'
  if (punch === '1') return 'Check out'
  return punch ? `Punch ${punch}` : 'Punch not reported'
}

const selectedCount = (
  candidates: AttendanceReleaseCandidates | null,
  mode: 'EXPLICIT' | 'ALL_FILTERED',
  included: Set<string>,
  excluded: Set<string>,
) =>
  mode === 'ALL_FILTERED'
    ? Math.max(0, (candidates?.totals.eligible || 0) - excluded.size)
    : included.size

function CandidateRows({
  candidates,
  rows,
  selectionMode,
  included,
  excluded,
  onToggle,
  selectionDisabled,
}: {
  candidates: AttendanceReleaseCandidates
  rows: AttendanceReleaseCandidate[]
  selectionMode: 'EXPLICIT' | 'ALL_FILTERED'
  included: Set<string>
  excluded: Set<string>
  onToggle: (row: AttendanceReleaseCandidate) => void
  selectionDisabled: boolean
}) {
  let currentDay = ''
  return (
    <div className="release-punch-list" aria-label="Held punches">
      {rows.map((row) => {
        const day = pakistanDay.format(new Date(row.device_event_time))
        const showDay = day !== currentDay
        currentDay = day
        const checked = Boolean(
          row.event_token &&
            (selectionMode === 'ALL_FILTERED'
              ? !excluded.has(row.event_token)
              : included.has(row.event_token)),
        )
        return (
          <Fragment key={row.event_uid}>
            {showDay && <h3 className="release-day-heading">{day}</h3>}
            <article className={`release-punch-row ${row.eligible ? '' : 'is-locked'}`}>
              <label className="release-punch-select">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={selectionDisabled || !row.eligible || !row.event_token}
                  onChange={() => onToggle(row)}
                  aria-label={`Select ${punchCopy(row.punch)} at ${dateTime(row.device_event_time)}`}
                />
                <span />
              </label>
              <div className="release-punch-primary">
                <strong>{punchCopy(row.punch)}</strong>
                <small>{dateTime(row.device_event_time)}</small>
              </div>
              <div>
                <strong>{humanize(row.source)}</strong>
                <small>{row.device_serial || 'Terminal serial unavailable'}</small>
              </div>
              <div>
                <StatusBadge state={row.source_ords_status} />
                <small>{row.risk_class === 'IDENTITY_REUSE' ? 'Elevated identity-reuse risk' : 'Ordinary identity hold'}</small>
              </div>
              <div>
                <strong>User {row.user_id || '—'} · UID {row.uid || '—'}</strong>
                <small>{row.evidence_classification ? humanize(row.evidence_classification) : 'No safe ownership evidence'}</small>
              </div>
              <div className="release-punch-eligibility">
                <StatusBadge state={row.eligible ? 'ELIGIBLE' : 'LOCKED'} />
                <small>{row.eligible ? 'May be included in preview' : explainLock(row.lock_reason)}</small>
              </div>
            </article>
          </Fragment>
        )
      })}
      {!rows.length && (
        <div className="empty-state">
          <Icon name="clock" />
          <h3>No held punches match these filters.</h3>
          <p>Change the PKT date, hold type, punch, or capture-source filter.</p>
        </div>
      )}
      <p className="sr-only" aria-live="polite">
        {selectedCount(candidates, selectionMode, included, excluded)} punches selected.
      </p>
    </div>
  )
}

export function AttendanceReleaseReview({
  devices,
  revision,
  toast,
  initialConnectorId,
  initialUserKey,
  onOpenHistory,
}: {
  devices: Device[]
  revision: number
  toast: Toast
  initialConnectorId?: string | null
  initialUserKey?: string | null
  onOpenHistory: (jobId?: string) => void
}) {
  const [queue, setQueue] = useState<AttendanceReleaseQueueResponse | null>(null)
  const [queueRows, setQueueRows] = useState<AttendanceReleaseQueueRow[]>([])
  const [queueQuery, setQueueQuery] = useState('')
  const [deviceId, setDeviceId] = useState(initialConnectorId || '')
  const [group, setGroup] = useState<AttendanceReleaseQueueRow | null>(null)
  const [candidates, setCandidates] = useState<AttendanceReleaseCandidates | null>(null)
  const [candidateRows, setCandidateRows] = useState<AttendanceReleaseCandidate[]>([])
  const [selectionMode, setSelectionMode] = useState<'EXPLICIT' | 'ALL_FILTERED'>('EXPLICIT')
  const [included, setIncluded] = useState<Set<string>>(new Set())
  const [excluded, setExcluded] = useState<Set<string>>(new Set())
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [holdStatus, setHoldStatus] = useState<'ALL' | 'BLOCKED_IDENTITY' | 'QUARANTINED_IDENTITY_REUSE'>('ALL')
  const [punch, setPunch] = useState('')
  const [source, setSource] = useState('')
  const [filtersDirty, setFiltersDirty] = useState(false)
  const [job, setJob] = useState<AttendanceRepairJob | null>(null)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [reuseCnic, setReuseCnic] = useState('')
  const [reuseName, setReuseName] = useState('')
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const deepLinkOpened = useRef(false)

  const loadQueue = useCallback(async (cursor = 0, append = false) => {
    setLoading(true)
    setError('')
    try {
      const result = await api<AttendanceReleaseQueueResponse>(
        `/api/v2/attendance-release-queue${queryString({
          connector_id: deviceId || undefined,
          q: queueQuery.trim() || undefined,
          cursor,
          limit: 100,
        })}`,
      )
      setQueue(result)
      setQueueRows((current) => append ? [...current, ...result.rows] : result.rows)
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'The release queue could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [deviceId, queueQuery])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadQueue(), 250)
    return () => window.clearTimeout(timer)
  }, [loadQueue, revision])

  const candidateBody = useCallback((row: AttendanceReleaseQueueRow, cursor = 0, token?: string) => ({
    user_key: row.user_key,
    ...(dateFrom && dateTo ? { date_from: dateFrom, date_to: dateTo } : {}),
    hold_statuses: holdStatus === 'ALL'
      ? ['BLOCKED_IDENTITY', 'QUARANTINED_IDENTITY_REUSE']
      : [holdStatus],
    punch: punch || null,
    source: source || null,
    cursor,
    limit: 100,
    candidate_set_token: token || null,
  }), [dateFrom, dateTo, holdStatus, punch, source])

  const loadCandidates = useCallback(async (
    row: AttendanceReleaseQueueRow,
    cursor = 0,
    append = false,
    token?: string,
  ) => {
    if (!row.connector_id || !row.user_key) return
    if ((dateFrom && !dateTo) || (!dateFrom && dateTo)) {
      setError('Choose both PKT date bounds or clear both.')
      return
    }
    setLoading(true)
    setError('')
    try {
      const result = await api<AttendanceReleaseCandidates>(
        `/api/v2/devices/${row.connector_id}/attendance-release-candidates/query`,
        { method: 'POST', body: JSON.stringify(candidateBody(row, cursor, token)) },
      )
      setCandidates(result)
      setCandidateRows((current) => append ? [...current, ...result.rows] : result.rows)
      if (!append) {
        setFiltersDirty(false)
        setSelectionMode('EXPLICIT')
        setIncluded(new Set())
        setExcluded(new Set())
        setJob(null)
      }
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'Held punches could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [candidateBody, dateFrom, dateTo])

  const openGroup = useCallback((row: AttendanceReleaseQueueRow) => {
    if (!row.eligible || !row.user_key || !row.connector_id) return
    setGroup(row)
    void loadCandidates(row)
  }, [loadCandidates])

  useEffect(() => {
    if (deepLinkOpened.current || !initialUserKey || !queueRows.length) return
    const match = queueRows.find(
      (row) =>
        row.user_key === initialUserKey &&
        (!initialConnectorId || row.connector_id === initialConnectorId),
    )
    if (match) {
      deepLinkOpened.current = true
      openGroup(match)
    }
  }, [initialConnectorId, initialUserKey, openGroup, queueRows])

  useEffect(() => {
    if (!job || !activeStates.has(job.status) || job.status === 'AWAITING_APPROVAL' || job.status === 'PAUSED' || job.status === 'NEEDS_ATTENTION') return
    const timer = window.setInterval(async () => {
      try {
        const current = await api<AttendanceRepairJob>(
          `/api/v2/attendance-releases/${job.job_id}`,
        )
        setJob(current)
      } catch {
        // Keep the durable last-known state visible during transient polling errors.
      }
    }, 3000)
    return () => window.clearInterval(timer)
  }, [job])

  const togglePunch = (row: AttendanceReleaseCandidate) => {
    if (filtersDirty || job || !row.event_token || !row.eligible) return
    if (selectionMode === 'ALL_FILTERED') {
      setExcluded((current) => {
        const next = new Set(current)
        if (next.has(row.event_token!)) next.delete(row.event_token!)
        else next.add(row.event_token!)
        return next
      })
      return
    }
    setIncluded((current) => {
      const next = new Set(current)
      if (next.has(row.event_token!)) next.delete(row.event_token!)
      else next.add(row.event_token!)
      return next
    })
  }

  const applyFilters = (event: FormEvent) => {
    event.preventDefault()
    if (group) void loadCandidates(group)
  }

  const changeCandidateFilter = (change: () => void) => {
    change()
    setFiltersDirty(true)
    setSelectionMode('EXPLICIT')
    setIncluded(new Set())
    setExcluded(new Set())
  }

  const prepare = async () => {
    if (!group?.connector_id || !candidates) return
    const count = selectedCount(candidates, selectionMode, included, excluded)
    if (!count) {
      setError('Select at least one eligible punch.')
      return
    }
    setLoading(true)
    setError('')
    try {
      const prepared = await api<AttendanceRepairJob>(
        `/api/v2/devices/${group.connector_id}/attendance-releases/prepare`,
        {
          method: 'POST',
          body: JSON.stringify({
            candidate_set_token: candidates.candidate_set_token,
            selection_mode: selectionMode,
            included_event_tokens: selectionMode === 'EXPLICIT' ? [...included] : [],
            excluded_event_tokens: selectionMode === 'ALL_FILTERED' ? [...excluded] : [],
            idempotency_key: idempotency('attendance-release-prepare'),
          }),
        },
      )
      setJob(prepared)
      toast.notice('The exact punch selection is frozen. Oracle safety checks are running.')
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'The release preview could not be prepared.')
    } finally {
      setLoading(false)
    }
  }

  const approve = async (event: FormEvent) => {
    event.preventDefault()
    if (!job?.preview_digest || !job.typed_confirmation) return
    if (reason.trim().length < 10 || reason.trim().length > 500) {
      setError('Enter a reason between 10 and 500 characters.')
      return
    }
    setLoading(true)
    setError('')
    try {
      const approved = await api<AttendanceRepairJob>(
        `/api/v2/attendance-releases/${job.job_id}/approve`,
        {
          method: 'POST',
          body: JSON.stringify({
            reason: reason.trim(),
            password,
            typed_confirmation: confirmation,
            preview_digest: job.preview_digest,
            idempotency_key: idempotency('attendance-release-approve'),
            ...(job.totals.safe_reuse
              ? { reuse_cnic: reuseCnic, reuse_employee_name: reuseName.trim() }
              : {}),
          }),
        },
      )
      setPassword('')
      setReuseCnic('')
      setReuseName('')
      setConfirmation('')
      toast.notice('Release approved. Durable background processing has started.')
      onOpenHistory(approved.job_id)
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'The release could not be approved.')
    } finally {
      setLoading(false)
    }
  }

  const selectionCount = selectedCount(candidates, selectionMode, included, excluded)
  const unsafeItems = job?.items?.filter((item) => item.state === 'NEEDS_REVIEW') || []

  return (
    <div className="release-review-workspace">
      <section className="metric-grid attendance-metrics" aria-label="Attendance release queue totals">
        <Metric label="Employees in queue" value={(queue?.totals.employees || 0).toLocaleString()} detail="Grouped by employee and terminal" icon="users" />
        <Metric label="Held punches" value={(queue?.totals.events || 0).toLocaleString()} detail="Original capture dispositions retained" icon="clock" />
        <Metric label="Eligible now" value={(queue?.totals.eligible || 0).toLocaleString()} detail="Current, stable and CNIC-complete" icon="check" tone="positive" />
        <Metric label="Locked" value={(queue?.totals.locked || 0).toLocaleString()} detail="Reason shown on every group" icon="shield" tone={queue?.totals.locked ? 'warning' : 'positive'} />
      </section>

      {!queue?.preview_enabled && (
        <div className="message pattern-waiting" role="status">
          <Icon name="pause" />
          <span>Preview-only rollout is currently disabled. Held punches remain visible and unchanged.</span>
        </div>
      )}
      {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>{error}</span></div>}

      <div className={`release-review-layout ${group ? 'has-detail' : ''}`}>
        <section className="panel release-queue-panel">
          <header className="panel-header">
            <div><h2>Employees needing review</h2><p>One release always stays within one employee and one terminal.</p></div>
          </header>
          <div className="release-queue-filters">
            <label className="search-field"><span className="sr-only">Search release queue</span><Icon name="search" /><input value={queueQuery} onChange={(event) => setQueueQuery(event.target.value)} placeholder="Search employee, user ID, UID or terminal" /></label>
            <label><span className="sr-only">Filter release queue by device</span><select value={deviceId} onChange={(event) => { setDeviceId(event.target.value); setGroup(null); setCandidates(null) }}><option value="">All terminals</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name}</option>)}</select></label>
          </div>
          <div className="release-queue-list" aria-busy={loading && !group}>
            {queueRows.map((row) => (
              <article className={`release-queue-row ${row.eligible ? '' : 'is-locked'} ${group?.user_key === row.user_key && group.connector_id === row.connector_id ? 'is-selected' : ''}`} key={`${row.connector_id}:${row.user_key || `${row.user_id}:${row.uid}`}`}>
                <div className="release-queue-person"><span className="avatar">{(row.display_name || '?').slice(0, 2).toUpperCase()}</span><span><strong>{row.display_name}</strong><small>User {row.user_id} · UID {row.uid || '—'} · {row.cnic_masked || 'CNIC missing'}</small></span></div>
                <div className="release-queue-terminal"><strong>{row.device_name}</strong><small>{row.device_id || 'Connector unavailable'}</small></div>
                <div className="release-queue-counts"><span><strong>{row.counts.ordinary_blocked}</strong><small>ordinary</small></span><span><strong>{row.counts.identity_reuse}</strong><small>reuse</small></span><span><strong>{row.counts.eligible}</strong><small>eligible</small></span></div>
                <div className="release-queue-window"><small>{dateTime(row.first_event_at)}</small><span aria-hidden="true">→</span><small>{dateTime(row.last_event_at)}</small></div>
                <div className="release-queue-action">
                  {row.eligible ? (
                    <button className="button secondary" type="button" onClick={() => openGroup(row)}>Review punches</button>
                  ) : row.active_release_job_id ? (
                    <button className="button secondary" type="button" onClick={() => onOpenHistory(row.active_release_job_id!)}>Open active release</button>
                  ) : (
                    <><StatusBadge state="LOCKED" /><small>{explainLock(row.lock_reason)}</small>{row.connector_id && row.lock_reasons.includes('TARGET_CNIC_MISSING') && <a className="text-button" href={`/users/${row.connector_id}?user_id=${encodeURIComponent(row.user_id)}`}>Add CNIC</a>}</>
                  )}
                </div>
              </article>
            ))}
            {!loading && !queueRows.length && (
              <div className="empty-state"><Icon name="check" /><h3>No employees need review.</h3><p>No identity-held punches match the current queue filters.</p></div>
            )}
          </div>
          {queue?.next_cursor != null && <div className="load-more"><button className="button secondary" type="button" disabled={loading} onClick={() => void loadQueue(queue.next_cursor!, true)}>Load more employees</button></div>}
        </section>

        {group && candidates && (
          <section className="panel release-candidate-panel">
            <header className="panel-header release-candidate-header">
              <div><p className="eyebrow">EXACT EVENT SELECTION</p><h2>{candidates.target.display_name}</h2><p>User {candidates.target.user_id} · UID {candidates.target.uid || '—'} · {candidates.target.cnic_masked || 'CNIC missing'} · {group.device_name}</p></div>
              <button className="icon-button" type="button" aria-label="Close employee review" onClick={() => { setGroup(null); setCandidates(null); setJob(null) }}><Icon name="x" /></button>
            </header>

            <form className="release-candidate-filters" onSubmit={applyFilters}>
              <label>From date (PKT)<input type="date" value={dateFrom} disabled={Boolean(job)} onChange={(event) => changeCandidateFilter(() => setDateFrom(event.target.value))} /></label>
              <label>To date (PKT)<input type="date" value={dateTo} disabled={Boolean(job)} onChange={(event) => changeCandidateFilter(() => setDateTo(event.target.value))} /></label>
              <label>Held status<select value={holdStatus} disabled={Boolean(job)} onChange={(event) => changeCandidateFilter(() => setHoldStatus(event.target.value as typeof holdStatus))}><option value="ALL">All reviewable holds</option><option value="BLOCKED_IDENTITY">Ordinary blocked</option><option value="QUARANTINED_IDENTITY_REUSE">Identity reuse</option></select></label>
              <label>Punch<select value={punch} disabled={Boolean(job)} onChange={(event) => changeCandidateFilter(() => setPunch(event.target.value))}><option value="">All punches</option><option value="0">Check in</option><option value="1">Check out</option></select></label>
              <label>Capture source<select value={source} disabled={Boolean(job)} onChange={(event) => changeCandidateFilter(() => setSource(event.target.value))}><option value="">All sources</option><option value="LIVE">Live capture</option><option value="LIVE_POLL">Live poll</option><option value="FULL_HISTORY">Full history</option><option value="DUMP_STARTUP">Startup recovery</option><option value="DUMP_RECONNECT">Reconnect recovery</option><option value="MANUAL_REPROCESS">Manual reprocess</option></select></label>
              <button className="button secondary" type="submit" disabled={loading || Boolean(job)}><Icon name="search" /> Apply filters</button>
            </form>

            {filtersDirty && <div className="message pattern-waiting" role="status"><Icon name="alert" /><span>Apply the changed filters before selecting punches. The previous selection was cleared.</span></div>}

            <div className="release-selection-bar">
              <div><strong>{selectionCount.toLocaleString()} selected</strong><small>Nothing is selected automatically. New punches never join this frozen candidate set.</small></div>
              <div>
                <button className="button secondary" type="button" disabled={Boolean(job) || filtersDirty || !candidates.totals.eligible} onClick={() => { setSelectionMode('ALL_FILTERED'); setIncluded(new Set()); setExcluded(new Set()) }}>Select all {candidates.totals.eligible.toLocaleString()} eligible matching filters</button>
                <button className="text-button" type="button" disabled={Boolean(job)} onClick={() => { setSelectionMode('EXPLICIT'); setIncluded(new Set()); setExcluded(new Set()) }}>Clear selection</button>
              </div>
            </div>
            <div className="release-candidate-summary">
              <span>{candidates.totals.ordinary_blocked} ordinary</span>
              <span>{candidates.totals.identity_reuse} reuse</span>
              <span>{candidates.totals.locked} locked</span>
              <span>Candidate set expires {relativeTime(candidates.expires_at)}</span>
            </div>

            <CandidateRows candidates={candidates} rows={candidateRows} selectionMode={selectionMode} included={included} excluded={excluded} onToggle={togglePunch} selectionDisabled={filtersDirty || Boolean(job)} />
            {candidates.next_cursor != null && <div className="load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={async () => { setLoadingMore(true); await loadCandidates(group, candidates.next_cursor!, true, candidates.candidate_set_token); setLoadingMore(false) }}>{loadingMore ? 'Loading punches…' : 'Load more punches'}</button></div>}

            {!job && (
              <footer className="release-prepare-footer">
                <div><strong>Prepare exact Oracle safety check</strong><small>{selectionMode === 'ALL_FILTERED' ? `${excluded.size} explicit exclusion${excluded.size === 1 ? '' : 's'}` : 'Only individually checked punches'} · no mutation yet</small></div>
                <button className="button primary" type="button" disabled={loading || filtersDirty || !selectionCount || !candidates.source_current} onClick={() => void prepare()}><Icon name="shield" /> {loading ? 'Preparing…' : `Prepare ${selectionCount.toLocaleString()} punches`}</button>
              </footer>
            )}

            {job && (
              <div className="release-preview">
                <header><div><p className="eyebrow">DURABLE RELEASE {job.job_id.slice(0, 8)}</p><h3>{job.release_state || humanize(job.status)}</h3></div><StatusBadge state={job.status} /></header>
                <div className="release-preview-counts">
                  <span><strong>{job.totals.selected ?? job.totals.events}</strong><small>selected</small></span>
                  <span><strong>{job.totals.safe ?? Math.max(0, job.totals.events - job.totals.excluded)}</strong><small>safe</small></span>
                  <span><strong>{job.totals.excluded}</strong><small>excluded</small></span>
                  <span><strong>{job.totals.ordinary ?? 0}</strong><small>ordinary</small></span>
                  <span><strong>{job.totals.reuse ?? 0}</strong><small>reuse</small></span>
                  <span><strong>{job.downstream_impact?.employee_days ?? 0}</strong><small>employee-days</small></span>
                </div>
                {unsafeItems.length > 0 && (
                  <details className="release-exclusions" open>
                    <summary>{unsafeItems.length} unsafe punch{unsafeItems.length === 1 ? '' : 'es'} excluded</summary>
                    <div>{unsafeItems.map((item) => <p key={item.event_uid}><code>{item.event_uid.slice(0, 12)}…</code><span>{humanize(item.oracle_classification)} · {item.error_message || explainLock(item.error_code)}</span></p>)}</div>
                  </details>
                )}
                {job.status === 'PREPARING_SOURCE' && <div className="info-copy pattern-waiting"><Icon name="refresh" /><span><strong>Oracle check is running</strong><small>The exact membership is frozen and no punch has been changed.</small></span></div>}
                {job.status === 'COMPLETED_WITH_ATTENTION' && <div className="info-copy pattern-blocked"><Icon name="alert" /><span><strong>No unsafe item was released</strong><small>{job.error_message || 'Review the exclusions and prepare a fresh selection.'}</small></span></div>}
                {job.status === 'AWAITING_APPROVAL' && (
                  <form className="release-approval-form" onSubmit={approve}>
                    <div className="info-copy pattern-waiting"><Icon name="shield" /><span><strong>Review the safe subset before approval</strong><small>Approval cannot alter membership. Unsafe exclusions stay untouched.</small></span></div>
                    {Boolean(job.totals.safe_reuse) && (
                      <fieldset className="release-reuse-proof">
                        <legend>Elevated identity-reuse attestation</legend>
                        <p>Re-enter the full CNIC and authoritative current name. Values are verified transiently and are never stored or logged in plaintext.</p>
                        <label>Full CNIC<input inputMode="numeric" autoComplete="off" value={reuseCnic} onChange={(event) => setReuseCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" /></label>
                        <label>Authoritative employee name<input autoComplete="off" value={reuseName} onChange={(event) => setReuseName(event.target.value)} /></label>
                      </fieldset>
                    )}
                    <label>Release reason <span>{reason.trim().length}/500</span><textarea value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} placeholder="Explain why this exact punch set belongs to the employee" /></label>
                    <label>Current administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
                    <label>Type the server confirmation exactly<code>{job.typed_confirmation}</code><input autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
                    <div className="release-preview-digest"><small>Frozen preview digest</small><code>{job.preview_digest}</code><small>Expires {relativeTime(job.preview_expires_at)}</small></div>
                    <button className="button primary" type="submit" disabled={loading || reason.trim().length < 10 || !password || confirmation !== job.typed_confirmation || (Boolean(job.totals.safe_reuse) && (!/^\d{13}$/.test(reuseCnic) || !reuseName.trim()))}><Icon name="check" /> {loading ? 'Approving…' : 'Approve safe punches'}</button>
                  </form>
                )}
                <button className="text-button" type="button" onClick={() => onOpenHistory(job.job_id)}>Open in release history</button>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  )
}

const controlCopy: Record<ControlAction, string> = {
  pause: 'Pause release',
  resume: 'Resume release',
  cancel: 'Cancel uncommitted work',
  retry: 'Retry safe work',
}

export function AttendanceReleaseHistory({
  devices,
  revision,
  toast,
  initialJobId,
}: {
  devices: Device[]
  revision: number
  toast: Toast
  initialJobId?: string | null
}) {
  const [jobs, setJobs] = useState<AttendanceRepairJob[]>([])
  const [list, setList] = useState<AttendanceRepairListResponse | null>(null)
  const [job, setJob] = useState<AttendanceRepairJob | null>(null)
  const [query, setQuery] = useState('')
  const [deviceId, setDeviceId] = useState('')
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [control, setControl] = useState<ControlAction | null>(null)
  const [controlReason, setControlReason] = useState('')
  const [controlPassword, setControlPassword] = useState('')
  const deepLinkOpened = useRef(false)

  const loadJobs = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await api<AttendanceRepairListResponse>(
        `/api/v2/attendance-releases${queryString({
          connector_id: deviceId || undefined,
          status: status || undefined,
          q: query.trim() || undefined,
          limit: 100,
        })}`,
      )
      setList(result)
      setJobs(result.rows)
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'Release history could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [deviceId, query, status])

  const openJob = useCallback(async (jobId: string) => {
    setLoading(true)
    setError('')
    try {
      setJob(await api<AttendanceRepairJob>(`/api/v2/attendance-releases/${jobId}`))
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'Release details could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadJobs(), 250)
    return () => window.clearTimeout(timer)
  }, [loadJobs, revision])

  useEffect(() => {
    if (!initialJobId || deepLinkOpened.current) return
    deepLinkOpened.current = true
    void openJob(initialJobId)
  }, [initialJobId, openJob])

  useEffect(() => {
    if (!job || !activeStates.has(job.status) || job.status === 'PAUSED' || job.status === 'NEEDS_ATTENTION' || job.status === 'AWAITING_APPROVAL') return
    const timer = window.setInterval(async () => {
      try {
        const current = await api<AttendanceRepairJob>(`/api/v2/attendance-releases/${job.job_id}`)
        setJob(current)
        if (!activeStates.has(current.status)) void loadJobs()
      } catch {
        // Keep the last durable state visible while connectivity recovers.
      }
    }, 3000)
    return () => window.clearInterval(timer)
  }, [job, loadJobs])

  const loadMoreItems = async () => {
    if (!job?.items_next_cursor) return
    setLoadingMore(true)
    try {
      const page = await api<AttendanceRepairJob>(
        `/api/v2/attendance-releases/${job.job_id}${queryString({ item_cursor: job.items_next_cursor, item_limit: 500 })}`,
      )
      setJob((current) => current ? { ...page, items: [...(current.items || []), ...(page.items || [])] } : page)
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'More punch outcomes could not be loaded.')
    } finally {
      setLoadingMore(false)
    }
  }

  const submitControl = async (event: FormEvent) => {
    event.preventDefault()
    if (!job || !control) return
    setLoading(true)
    setError('')
    try {
      const current = await api<AttendanceRepairJob>(
        `/api/v2/attendance-releases/${job.job_id}/${control}`,
        {
          method: 'POST',
          body: JSON.stringify({
            reason: controlReason.trim(),
            password: controlPassword,
            idempotency_key: idempotency(`attendance-release-${control}`),
          }),
        },
      )
      setJob(current)
      setControl(null)
      setControlReason('')
      setControlPassword('')
      toast.notice(`${controlCopy[control]} request was recorded.`)
      void loadJobs()
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'The release control could not be saved.')
    } finally {
      setLoading(false)
    }
  }

  const canPause = job && ['QUEUED', 'RUNNING', 'WAITING_ORACLE', 'WAITING_DOWNSTREAM'].includes(job.status)
  const canResume = job?.status === 'PAUSED'
  const canRetry = job && ['NEEDS_ATTENTION', 'COMPLETED_WITH_ATTENTION'].includes(job.status)
  const canCancel = job && !['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status)

  return (
    <div className="release-history-workspace">
      <section className="metric-grid attendance-metrics" aria-label="Release history totals">
        <Metric label="All releases" value={(list?.totals.all || 0).toLocaleString()} detail="Immutable job history" icon="list" />
        <Metric label="Active" value={(list?.totals.active || 0).toLocaleString()} detail="Preparing, queued or verifying" icon="refresh" tone={list?.totals.active ? 'warning' : 'positive'} />
        <Metric label="Needs attention" value={(list?.totals.attention || 0).toLocaleString()} detail="Safe retry or operator review" icon="alert" tone={list?.totals.attention ? 'critical' : 'positive'} />
        <Metric label="Worker" value={list?.worker?.heartbeat?.state || 'Unknown'} detail={`${list?.worker?.waiting_downstream_items || 0} waiting downstream`} icon="server" />
      </section>
      {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>{error}</span></div>}
      <div className={`release-history-layout ${job ? 'has-detail' : ''}`}>
        <section className="panel release-history-list-panel">
          <header className="panel-header"><div><h2>Release history</h2><p>Search durable jobs by employee, terminal, user ID or job ID.</p></div></header>
          <div className="release-history-filters">
            <label className="search-field"><span className="sr-only">Search release history</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search releases" /></label>
            <select aria-label="Filter release history by terminal" value={deviceId} onChange={(event) => setDeviceId(event.target.value)}><option value="">All terminals</option>{devices.map((device) => <option value={device.connector_id} key={device.connector_id}>{device.display_name}</option>)}</select>
            <select aria-label="Filter release history by status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All states</option><option value="PREPARING_SOURCE">Preparing</option><option value="AWAITING_APPROVAL">Awaiting approval</option><option value="QUEUED">Queued</option><option value="RUNNING">Processing</option><option value="PAUSED">Paused</option><option value="COMPLETED">Released</option><option value="COMPLETED_WITH_ATTENTION">Completed with attention</option><option value="CANCELLED">Cancelled</option></select>
          </div>
          <div className="release-history-list" aria-busy={loading && !job}>
            {jobs.map((row) => (
              <button className={`release-history-row ${job?.job_id === row.job_id ? 'is-selected' : ''}`} type="button" key={row.job_id} onClick={() => void openJob(row.job_id)}>
                <span><strong>{row.targets[0]?.display_name || `User ${row.release_target_user_id || '—'}`}</strong><small>{row.device_id} · {row.totals.events} selected · {row.totals.excluded} excluded</small></span>
                <StatusBadge state={row.release_state || row.status} />
                <span><strong>{row.actor}</strong><small>{relativeTime(row.created_at)}</small></span>
                <Icon name="chevron" />
              </button>
            ))}
            {!loading && !jobs.length && <div className="empty-state"><Icon name="shield" /><h3>No releases recorded.</h3><p>Prepared attendance releases will appear here durably.</p></div>}
          </div>
        </section>

        {job && (
          <section className="panel release-history-detail">
            <header className="panel-header">
              <div><p className="eyebrow">RELEASE {job.job_id}</p><h2>{job.targets[0]?.display_name || `User ${job.release_target_user_id || '—'}`}</h2><p>{job.device_id} · created {dateTime(job.created_at)} by {job.actor}</p></div>
              <button className="icon-button" type="button" aria-label="Close release details" onClick={() => setJob(null)}><Icon name="x" /></button>
            </header>
            <div className="release-detail-state"><StatusBadge state={job.release_state || job.status} /><span><strong>{humanize(job.phase)}</strong><small>{job.wait_reason ? explainLock(job.wait_reason) : 'Durable processing state is current.'}</small></span></div>
            <div className="release-preview-counts">
              <span><strong>{job.totals.selected ?? job.totals.events}</strong><small>selected</small></span>
              <span><strong>{job.totals.safe ?? Math.max(0, job.totals.events - job.totals.excluded)}</strong><small>safe</small></span>
              <span><strong>{job.totals.excluded}</strong><small>excluded</small></span>
              <span><strong>{job.totals.completed_events}</strong><small>released</small></span>
              <span><strong>{job.totals.attention_events}</strong><small>attention</small></span>
              <span><strong>{job.preparation_attempt_count}</strong><small>prepare retries</small></span>
            </div>
            <dl className="release-detail-metadata">
              <div><dt>Reason</dt><dd>{job.reason || 'Not approved yet'}</dd></div>
              <div><dt>Selection</dt><dd>{humanize(job.selection_mode)} · {job.totals.operator_excluded || 0} omitted by operator · digest <code>{job.selection_manifest_digest?.slice(0, 16) || '—'}…</code></dd></div>
              {Boolean(job.totals.operator_excluded) && <div><dt>Omitted membership</dt><dd><code>{job.selection_exclusion_manifest_digest?.slice(0, 16) || '—'}…</code></dd></div>}
              <div><dt>Candidate membership</dt><dd><code>{job.candidate_membership_digest?.slice(0, 16) || '—'}…</code></dd></div>
              <div><dt>Preview</dt><dd><code>{job.preview_digest?.slice(0, 16) || '—'}…</code></dd></div>
              <div><dt>Approved</dt><dd>{dateTime(job.approved_at)}</dd></div>
              <div><dt>Started</dt><dd>{dateTime(job.started_at)}</dd></div>
              <div><dt>Completed</dt><dd>{dateTime(job.completed_at)}</dd></div>
            </dl>
            {job.reuse_attestation && <div className="info-copy pattern-confirmed"><Icon name="shield" /><span><strong>Identity-reuse attestation verified</strong><small>{job.reuse_attestation.event_count} exact safe reuse punches · {job.reuse_attestation.attestation_id}</small></span></div>}
            <div className="release-detail-actions">
              {canPause && <button className="button secondary" type="button" onClick={() => setControl('pause')}><Icon name="pause" /> Pause</button>}
              {canResume && <button className="button secondary" type="button" onClick={() => setControl('resume')}><Icon name="refresh" /> Resume</button>}
              {canRetry && <button className="button secondary" type="button" onClick={() => setControl('retry')}><Icon name="refresh" /> Retry</button>}
              {canCancel && <button className="button danger" type="button" onClick={() => setControl('cancel')}><Icon name="x" /> Cancel uncommitted</button>}
              <a className="button secondary" href={`/api/v2/attendance-releases/${job.job_id}/evidence`}><Icon name="shield" /> Download evidence JSON</a>
            </div>
            <div className="release-outcome-list">
              <div className="release-outcome-head"><span>Punch</span><span>Classification</span><span>Outcome</span><span>Oracle / downstream proof</span><span>Retries</span></div>
              {(job.items || []).map((item) => (
                <article className="release-outcome-row" key={item.event_uid}>
                  <span><strong>{punchCopy(item.punch)}</strong><small>{dateTime(item.event_time)} · {humanize(item.capture_source)}</small><code>{item.event_uid.slice(0, 12)}…</code></span>
                  <span><StatusBadge state={item.oracle_classification} /><small>{humanize(item.risk_class)}</small></span>
                  <span><StatusBadge state={item.outcome || item.state} /><small>{outcomeDetail(item)}</small></span>
                  <span><strong>{item.oracle_receipt_id || 'No receipt yet'}</strong><small>{item.downstream_status ? `${humanize(item.downstream_status)} ${item.downstream_verified_at ? relativeTime(item.downstream_verified_at) : ''}` : 'Downstream proof pending'}</small></span>
                  <span><strong>{item.attempt_count}</strong><small>Oracle {item.oracle_attempt_count} · downstream {item.downstream_attempt_count}</small></span>
                </article>
              ))}
            </div>
            {job.items_next_cursor && <div className="load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadMoreItems()}>{loadingMore ? 'Loading outcomes…' : 'Load more outcomes'}</button></div>}
          </section>
        )}
      </div>

      {control && job && (
        <Dialog titleId="release-control-title" title={controlCopy[control]} description="This control is recorded in the hash-chained release evidence." onClose={() => setControl(null)}>
          <form className="dialog-form" onSubmit={submitControl}>
            {control === 'cancel' && <div className="info-copy pattern-blocked"><Icon name="alert" /><span><strong>Oracle-confirmed punches are never reversed</strong><small>Cancellation stops only work that has not committed.</small></span></div>}
            <label>Reason<textarea minLength={10} maxLength={500} value={controlReason} onChange={(event) => setControlReason(event.target.value)} /></label>
            <label>Current administrator password<input type="password" autoComplete="current-password" value={controlPassword} onChange={(event) => setControlPassword(event.target.value)} /></label>
            <footer className="dialog-actions"><button className="button secondary" type="button" onClick={() => setControl(null)}>Keep current state</button><button className={`button ${control === 'cancel' ? 'danger' : 'primary'}`} type="submit" disabled={loading || controlReason.trim().length < 10 || !controlPassword}>{loading ? 'Saving…' : controlCopy[control]}</button></footer>
          </form>
        </Dialog>
      )}
    </div>
  )
}
