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
  dateTime,
  idempotency,
  relativeTime,
} from '../App'
import { Icon } from '../Icon'
import { StatusChip, TechnicalDetails } from '../ui'
import { plainErrorText } from '../lib/plainApiError'
import { STEP_UP } from '../words/glossary'
import { explainReleaseLock, oldReleaseStatus } from './attendance/attendanceWords'
import { friendlyName } from './device/deviceNaming'
import type {
  AttendanceRepairJob,
  AttendanceRepairItemOutcome,
  AttendanceRepairListResponse,
  Device,
} from '../types'

// Old release tasks (Attendance › History, no longer used). Plain words only;
// codes, ids and digests stay inside Technical details (ATT-12).

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

const reachedHr = (item: AttendanceRepairItemOutcome) =>
  item.downstream_status === 'VERIFIED' || Boolean(item.downstream_verified_at) || Boolean(item.outcome?.includes('DOWNSTREAM_VERIFIED'))

const outcomeWords = (item: AttendanceRepairItemOutcome): { label: string; tone: 'working' | 'attention' | 'neutral'; detail: string } => {
  if (reachedHr(item)) return { label: 'Sent', tone: 'working', detail: 'The HR system has it.' }
  if (item.error_code) return { label: 'Not sent', tone: 'attention', detail: explainReleaseLock(item.error_code) }
  return { label: 'Waiting', tone: 'neutral', detail: 'Waiting its turn.' }
}

const kindWords = (punch?: string | null) => {
  if (punch === '0') return 'Check in'
  if (punch === '1') return 'Check out'
  return punch ? 'Other kind' : 'Kind not known'
}

const controlCopy: Record<ControlAction, string> = {
  pause: 'Pause',
  resume: 'Resume',
  cancel: 'Stop the work not yet sent',
  retry: 'Try the safe ones again',
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
      setError(plainErrorText(reasonValue, "Couldn't load the old release tasks. Try again."))
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
      setError(plainErrorText(reasonValue, "Couldn't open this task. Try again."))
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
      setError(plainErrorText(reasonValue, "Couldn't load more check-ins. Try again."))
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
      toast.notice('Saved.')
      void loadJobs()
    } catch (reasonValue) {
      setError(plainErrorText(reasonValue, "Couldn't save this. Try again."))
    } finally {
      setLoading(false)
    }
  }

  const canPause = job && ['QUEUED', 'RUNNING', 'WAITING_ORACLE', 'WAITING_DOWNSTREAM'].includes(job.status)
  const canResume = job?.status === 'PAUSED'
  const canRetry = job && ['NEEDS_ATTENTION', 'COMPLETED_WITH_ATTENTION'].includes(job.status)
  const canCancel = job && !['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status)

  const placeOf = (connectorId: string) => {
    const device = devices.find((candidate) => candidate.connector_id === connectorId)
    return device ? friendlyName(device) : 'Machine not known'
  }
  const personOf = (row: AttendanceRepairJob) => row.targets[0]?.display_name || (row.release_target_user_id ? `Employee ID ${row.release_target_user_id}` : 'Name not known')
  const chip = (row: AttendanceRepairJob) => {
    const words = oldReleaseStatus(row.status)
    return <StatusChip tone={words.tone} label={words.label} code={row.status} />
  }

  return (
    <div className="release-history-workspace">
      <section className="metric-grid attendance-metrics" aria-label="Old release task totals">
        <Metric label="All old tasks" value={(list?.totals?.all || 0).toLocaleString()} detail="Kept as a record" icon="list" />
        <Metric label="Working" value={(list?.totals?.active || 0).toLocaleString()} detail="Getting ready or waiting" icon="refresh" tone={list?.totals?.active ? 'warning' : 'positive'} />
        <Metric label="Waiting for you" value={(list?.totals?.attention || 0).toLocaleString()} detail="Can be tried again or checked" icon="alert" tone={list?.totals?.attention ? 'critical' : 'positive'} />
        <Metric label="Waiting for the HR system" value={(list?.worker?.waiting_downstream_items || 0).toLocaleString()} detail="Check-ins not confirmed yet" icon="server" />
      </section>
      {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>{error}</span></div>}
      <div className={`release-history-layout ${job ? 'has-detail' : ''}`}>
        <section className="panel release-history-list-panel">
          <header className="panel-header"><div><h2>Old release tasks (no longer used)</h2><p>Read only.</p></div></header>
          <div className="release-history-filters">
            <label className="search-field"><span className="sr-only">Search by name, place or Employee ID</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by name, place or Employee ID" /></label>
            <select aria-label="Filter by machine" value={deviceId} onChange={(event) => setDeviceId(event.target.value)}><option value="">All machines</option>{devices.map((device) => <option value={device.connector_id} key={device.connector_id}>{friendlyName(device)}</option>)}</select>
            <select aria-label="Filter by status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All</option>{['PREPARING_SOURCE', 'AWAITING_APPROVAL', 'QUEUED', 'RUNNING', 'PAUSED', 'COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].map((code) => <option key={code} value={code}>{oldReleaseStatus(code).label}</option>)}</select>
          </div>
          <div className="release-history-list" aria-busy={loading && !job}>
            {jobs.map((row) => (
              <button className={`release-history-row ${job?.job_id === row.job_id ? 'is-selected' : ''}`} type="button" key={row.job_id} onClick={() => void openJob(row.job_id)}>
                <span><strong>{personOf(row)}</strong><small>{placeOf(row.connector_id)} · {row.totals.events.toLocaleString()} chosen · {row.totals.excluded.toLocaleString()} left out</small></span>
                {chip(row)}
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
              <div><p className="eyebrow">Old release task</p><h2>{personOf(job)}</h2><p>{placeOf(job.connector_id)} · started {dateTime(job.created_at)} by {job.actor}</p></div>
              <button className="icon-button" type="button" aria-label="Close details" onClick={() => setJob(null)}><Icon name="x" /></button>
            </header>
            <div className="release-detail-state">{chip(job)}<span><small>{job.wait_reason ? explainReleaseLock(job.wait_reason) : 'Up to date.'}</small></span></div>
            <div className="release-preview-counts">
              <span><strong>{job.totals.selected ?? job.totals.events}</strong><small>chosen</small></span>
              <span><strong>{job.totals.safe ?? Math.max(0, job.totals.events - job.totals.excluded)}</strong><small>can be sent</small></span>
              <span><strong>{job.totals.excluded}</strong><small>left out</small></span>
              <span><strong>{job.totals.completed_events}</strong><small>sent</small></span>
              <span><strong>{job.totals.attention_events}</strong><small>waiting for you</small></span>
            </div>
            <dl className="release-detail-metadata">
              <div><dt>Why</dt><dd>{job.reason || 'Not approved yet'}</dd></div>
              <div><dt>Approved</dt><dd>{dateTime(job.approved_at)}</dd></div>
              <div><dt>Started</dt><dd>{dateTime(job.started_at)}</dd></div>
              <div><dt>Finished</dt><dd>{dateTime(job.completed_at)}</dd></div>
            </dl>
            {job.reuse_attestation && <div className="info-copy pattern-confirmed"><Icon name="shield" /><span><strong>Reused Employee IDs were checked</strong><small>{job.reuse_attestation.event_count} check-ins</small></span></div>}
            <TechnicalDetails>
              <p>Job {job.job_id} · {job.status} · {job.phase} · machine {job.device_id}</p>
              <p>Selection {job.selection_mode} · {job.totals.operator_excluded || 0} left out by hand · digest {job.selection_manifest_digest || '—'}</p>
              {Boolean(job.totals.operator_excluded) && <p>Left-out list {job.selection_exclusion_manifest_digest || '—'}</p>}
              <p>Candidates {job.candidate_membership_digest || '—'} · preview {job.preview_digest || '—'} · prepare retries {job.preparation_attempt_count}</p>
              {job.wait_reason && <p>Wait {job.wait_reason}</p>}
              {job.reuse_attestation && <p>Attestation {job.reuse_attestation.attestation_id}</p>}
              <p><a href={`/api/v2/attendance-releases/${job.job_id}/evidence`}>Download the record for IT</a></p>
            </TechnicalDetails>
            <div className="release-detail-actions">
              {canPause && <button className="button secondary" type="button" onClick={() => setControl('pause')}><Icon name="pause" /> Pause</button>}
              {canResume && <button className="button secondary" type="button" onClick={() => setControl('resume')}><Icon name="refresh" /> Resume</button>}
              {canRetry && <button className="button secondary" type="button" onClick={() => setControl('retry')}><Icon name="refresh" /> Try again</button>}
              {canCancel && <button className="button destructive" type="button" onClick={() => setControl('cancel')}><Icon name="x" /> {controlCopy.cancel}</button>}
            </div>
            <div className="release-outcome-list">
              <div className="release-outcome-head"><span>Check-in</span><span>Kind</span><span>Result</span><span>Reached the HR system</span><span>Tries</span></div>
              {(job.items || []).map((item) => {
                const result = outcomeWords(item)
                return (
                  <article className="release-outcome-row" key={item.event_uid}>
                    <span><strong>{dateTime(item.event_time)}</strong><small data-technical>{item.event_uid.slice(0, 12)}… · {item.capture_source || '—'}</small></span>
                    <span><strong>{kindWords(item.punch)}</strong><small data-technical>{item.oracle_classification} · {item.risk_class || '—'}</small></span>
                    <span><StatusChip tone={result.tone} label={result.label} code={item.outcome || item.state} /><small>{result.detail}</small>{item.error_message && <small data-technical>{item.error_message}</small>}</span>
                    <span><strong>{reachedHr(item) ? 'Yes' : 'Not yet'}</strong><small>{item.downstream_verified_at ? relativeTime(item.downstream_verified_at) : ''}</small>{item.oracle_receipt_id && <small data-technical>{item.oracle_receipt_id}</small>}</span>
                    <span><strong>{item.attempt_count}</strong></span>
                  </article>
                )
              })}
            </div>
            {job.items_next_cursor && <div className="load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadMoreItems()}>{loadingMore ? 'Loading…' : 'Show more'}</button></div>}
          </section>
        )}
      </div>

      {control && job && (
        <Dialog titleId="release-control-title" title={controlCopy[control]} description="This is saved with your name." onClose={() => setControl(null)}>
          <form className="dialog-body" onSubmit={submitControl}>
            {control === 'cancel' && <div className="info-copy pattern-blocked"><Icon name="alert" /><span><strong>Check-ins the HR system has confirmed are never undone.</strong><small>Stopping only affects work not yet sent.</small></span></div>}
            <label>{STEP_UP.why}<textarea minLength={10} maxLength={500} value={controlReason} onChange={(event) => setControlReason(event.target.value)} /></label>
            <small className="step-up-help">{STEP_UP.whyHelp(10)}</small>
            <label>{STEP_UP.password}<input type="password" autoComplete="current-password" value={controlPassword} onChange={(event) => setControlPassword(event.target.value)} /></label>
            <small className="step-up-help">{STEP_UP.passwordHelp}</small>
            <footer className="dialog-actions"><button className="button secondary" type="button" onClick={() => setControl(null)}>Keep it as it is</button><button className={`button ${control === 'cancel' ? 'destructive' : 'primary'}`} type="submit" disabled={loading || controlReason.trim().length < 10 || !controlPassword}>{loading ? 'Saving…' : controlCopy[control]}</button></footer>
          </form>
        </Dialog>
      )}
    </div>
  )
}
