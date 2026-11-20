import {
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
        <Metric label="All releases" value={(list?.totals?.all || 0).toLocaleString()} detail="Immutable job history" icon="list" />
        <Metric label="Active" value={(list?.totals?.active || 0).toLocaleString()} detail="Preparing, queued or verifying" icon="refresh" tone={list?.totals?.active ? 'warning' : 'positive'} />
        <Metric label="Needs attention" value={(list?.totals?.attention || 0).toLocaleString()} detail="Safe retry or operator review" icon="alert" tone={list?.totals?.attention ? 'critical' : 'positive'} />
        <Metric label="Worker" value={list?.worker?.heartbeat?.state || 'Unknown'} detail={`${list?.worker?.waiting_downstream_items || 0} waiting downstream`} icon="server" />
      </section>
      {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>{error}</span></div>}
      <div className={`release-history-layout ${job ? 'has-detail' : ''}`}>
        <section className="panel release-history-list-panel">
          <header className="panel-header"><div><h2>Old release tasks (no longer used)</h2><p>Search by name, place or Employee ID</p></div></header>
          <div className="release-history-filters">
            <label className="search-field"><span className="sr-only">Search release history</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search releases" /></label>
            <select aria-label="Filter release history by terminal" value={deviceId} onChange={(event) => setDeviceId(event.target.value)}><option value="">All terminals</option>{devices.map((device) => <option value={device.connector_id} key={device.connector_id}>{device.display_name}</option>)}</select>
            <select aria-label="Filter release history by status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All states</option><option value="PREPARING_SOURCE">Preparing</option><option value="AWAITING_APPROVAL">Awaiting approval</option><option value="QUEUED">Queued</option><option value="RUNNING">Processing</option><option value="PAUSED">Paused</option><option value="COMPLETED">Released</option><option value="COMPLETED_WITH_ATTENTION">Completed with attention</option><option value="CANCELLED">Cancelled</option></select>
          </div>
          <div className="release-history-list" aria-busy={loading && !job}>
            {jobs.map((row) => (
              <button className={`release-history-row ${job?.job_id === row.job_id ? 'is-selected' : ''}`} type="button" key={row.job_id} onClick={() => void openJob(row.job_id)}>
                <span><strong>{row.targets[0]?.display_name || `User ${row.release_target_user_id || '—'}`}</strong><small>{devices.find((device) => device.connector_id === row.connector_id)?.display_name || `Terminal ${row.device_id}`} · {row.totals.events.toLocaleString()} selected · {row.totals.excluded.toLocaleString()} excluded</small></span>
                <StatusBadge state={row.release_state || row.status} />
                <span><strong>{row.actor}</strong><small>{relativeTime(row.created_at)}</small></span>
                <Icon name="chevron" />
              </button>
            ))}
            {!loading && !jobs.length && <div className="empty-state"><Icon name="shield" /><h3>No old release tasks.</h3><p>Old release tasks would show here. They are read only.</p></div>}
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
              {canCancel && <button className="button destructive" type="button" onClick={() => setControl('cancel')}><Icon name="x" /> Cancel uncommitted</button>}
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
          <form className="dialog-body" onSubmit={submitControl}>
            {control === 'cancel' && <div className="info-copy pattern-blocked"><Icon name="alert" /><span><strong>Oracle-confirmed punches are never reversed</strong><small>Cancellation stops only work that has not committed.</small></span></div>}
            <label>Reason<textarea minLength={10} maxLength={500} value={controlReason} onChange={(event) => setControlReason(event.target.value)} /></label>
            <label>Current administrator password<input type="password" autoComplete="current-password" value={controlPassword} onChange={(event) => setControlPassword(event.target.value)} /></label>
            <footer className="dialog-actions"><button className="button secondary" type="button" onClick={() => setControl(null)}>Keep current state</button><button className={`button ${control === 'cancel' ? 'destructive' : 'primary'}`} type="submit" disabled={loading || controlReason.trim().length < 10 || !controlPassword}>{loading ? 'Saving…' : controlCopy[control]}</button></footer>
          </form>
        </Dialog>
      )}
    </div>
  )
}
