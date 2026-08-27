import {
  useCallback,
  useEffect,
  useMemo,
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
  useToast,
} from '../App'
import { Icon } from '../Icon'
import type {
  AttendanceRepairCandidates,
  AttendanceRepairJob,
  AttendanceRepairListResponse,
  AttendanceRepairPreflight,
  Device,
  DeviceUser,
} from '../types'
import './EmployeeRepair.css'

type Toast = ReturnType<typeof useToast>
type ControlAction = 'pause' | 'resume' | 'cancel' | 'retry'

const activeJobStates = new Set([
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

function SourceDisclosure({ current }: { current: boolean }) {
  return (
    <div className={`info-copy pattern-${current ? 'notice' : 'waiting'}`}>
      <Icon name={current ? 'shield' : 'refresh'} />
      <div>
        <h3>
          {current
            ? 'Terminal source coverage is certified'
            : 'A complete terminal scan will run first'}
        </h3>
        <p>
          {current
            ? 'The final preview is bound to the certified source cursor and chain digest.'
            : 'ZKT terminals cannot retrieve history for only one employee. This non-destructive scan may recover missing punches for other employees, then the employee preview is rebuilt.'}
        </p>
      </div>
    </div>
  )
}

function JobLedger({
  job,
  loadingMore,
  onLoadMore,
}: {
  job: AttendanceRepairJob
  loadingMore?: boolean
  onLoadMore?: () => void
}) {
  const targetNames = useMemo(
    () => new Map(job.targets.map((target) => [target.user_key, target.display_name])),
    [job.targets],
  )
  const classifications = useMemo(() => {
    const counts = new Map<string, number>()
    for (const item of job.items || [])
      counts.set(
        item.oracle_classification,
        (counts.get(item.oracle_classification) || 0) + 1,
      )
    return [...counts.entries()]
  }, [job.items])
  return (
    <div className="employee-repair-job-detail">
      <div className="employee-repair-job-summary">
        <div>
          <span>Job</span>
          <strong>{job.job_id}</strong>
        </div>
        <div>
          <span>Phase</span>
          <StatusBadge state={job.phase} />
        </div>
        <div>
          <span>Progress</span>
          <strong>
            {job.totals.completed_events.toLocaleString()} /{' '}
            {job.totals.events.toLocaleString()} events
          </strong>
        </div>
        <div>
          <span>Attention</span>
          <strong>{job.totals.attention_events.toLocaleString()}</strong>
        </div>
        <div>
          <span>Affected Pakistan days</span>
          <strong>{job.downstream_impact?.employee_days.toLocaleString() ?? '—'}</strong>
        </div>
        <div>
          <span>Source dependency</span>
          <strong>{job.source_dependency_job_id?.slice(0, 12) || 'Certified directly'}</strong>
        </div>
      </div>
      {classifications.length > 0 && (
        <div className="employee-repair-classifications" aria-label="Oracle classifications">
          {classifications.map(([state, count]) => (
            <span key={state}>
              <StatusBadge state={state} /> <strong>{count.toLocaleString()}</strong>
            </span>
          ))}
        </div>
      )}
      <div className="employee-repair-target-ledger" role="list">
        {job.targets.map((target) => (
          <article key={target.user_key} role="listitem">
            <div>
              <strong>{target.display_name}</strong>
              <small>{target.cnic_masked}</small>
            </div>
            <StatusBadge state={target.status} />
            <span>
              {target.completed_event_count.toLocaleString()} /{' '}
              {target.event_count.toLocaleString()}
            </span>
            <small>
              {target.attention_event_count
                ? `${target.attention_event_count} need attention`
                : 'No exclusions'}
            </small>
          </article>
        ))}
      </div>
      {job.items && job.items.length > 0 && (
        <details className="employee-repair-items">
          <summary>Per-event outcomes ({job.items.length.toLocaleString()} loaded)</summary>
          <div role="list">
            {job.items.map((item) => (
              <article key={item.event_uid} role="listitem">
                <span>
                  <code>{item.event_uid.slice(0, 12)}…</code>
                  <small>{targetNames.get(item.user_key || '') || 'Unknown employee'}</small>
                </span>
                <span>{dateTime(item.event_time)}</span>
                <StatusBadge state={item.state} />
                <small>
                  {item.error_code || item.outcome || humanize(item.oracle_classification)} · Oracle{' '}
                  {item.oracle_attempt_count} · downstream {item.downstream_attempt_count}
                  {item.next_attempt_at ? ` · retry ${relativeTime(item.next_attempt_at)}` : ''}
                </small>
              </article>
            ))}
          </div>
          {job.items_next_cursor && onLoadMore && (
            <button
              className="button secondary"
              disabled={loadingMore}
              onClick={onLoadMore}
            >
              {loadingMore ? 'Loading outcomes…' : 'Load 500 more outcomes'}
            </button>
          )}
        </details>
      )}
    </div>
  )
}

export function EmployeeRepair({
  devices,
  revision,
  toast,
}: {
  devices: Device[]
  revision: number
  toast: Toast
}) {
  const initial = useMemo(() => new URLSearchParams(window.location.search), [])
  const [deviceId, setDeviceId] = useState(initial.get('device_id') || '')
  const [preflight, setPreflight] = useState<AttendanceRepairPreflight | null>(null)
  const [users, setUsers] = useState<DeviceUser[]>([])
  const [userCursor, setUserCursor] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')
  const [dateScoped, setDateScoped] = useState(false)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [candidates, setCandidates] = useState<AttendanceRepairCandidates | null>(null)
  const [aliasTokens, setAliasTokens] = useState<Set<string>>(new Set())
  const [job, setJob] = useState<AttendanceRepairJob | null>(null)
  const [jobs, setJobs] = useState<AttendanceRepairJob[]>([])
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [control, setControl] = useState<ControlAction | null>(null)
  const [controlReason, setControlReason] = useState('')
  const [controlPassword, setControlPassword] = useState('')
  const selectedDevice = devices.find((device) => device.connector_id === deviceId) || null

  const loadJobs = useCallback(async () => {
    try {
      const result = await api<AttendanceRepairListResponse>(
        `/api/v1/attendance-repairs${queryString({ connector_id: deviceId || undefined, limit: 30 })}`,
      )
      setJobs(result.rows)
      const deepLinked = initial.get('repair_job')
      if (!job && deepLinked) {
        const detail = await api<AttendanceRepairJob>(
          `/api/v1/attendance-repairs/${deepLinked}`,
        )
        setJob(detail)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to load repair jobs.')
    }
  }, [deviceId, initial, job])

  const loadTerminal = useCallback(async () => {
    if (!deviceId) {
      setPreflight(null)
      setUsers([])
      setUserCursor(null)
      return
    }
    setLoading(true)
    setError('')
    try {
      const [readiness, directory] = await Promise.all([
        api<AttendanceRepairPreflight>(
          `/api/v1/devices/${deviceId}/attendance-repairs/preflight`,
        ),
        api<{ rows: DeviceUser[]; next_cursor: number | null }>(
          `/api/v2/devices/${deviceId}/users${queryString({ identity: 'COMPLETE', present: true, limit: 500 })}`,
        ),
      ])
      setPreflight(readiness)
      setUsers(
        directory.rows.filter(
          (user) =>
            user.present &&
            user.lifecycle_state === 'ACTIVE' &&
            user.identity_complete &&
            (!user.identity_conflict_code || user.identity_conflict_resolved),
        ),
      )
      setUserCursor(directory.next_cursor)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to certify this terminal.')
    } finally {
      setLoading(false)
    }
  }, [deviceId])

  const loadMoreUsers = async () => {
    if (!deviceId || !userCursor) return
    setLoadingMore(true)
    try {
      const directory = await api<{
        rows: DeviceUser[]
        next_cursor: number | null
      }>(
        `/api/v2/devices/${deviceId}/users${queryString({ identity: 'COMPLETE', present: true, limit: 500, cursor: userCursor })}`,
      )
      setUsers((current) => [
        ...current,
        ...directory.rows.filter(
          (user) =>
            user.present &&
            user.lifecycle_state === 'ACTIVE' &&
            user.identity_complete &&
            (!user.identity_conflict_code || user.identity_conflict_resolved),
        ),
      ])
      setUserCursor(directory.next_cursor)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to load more employees.')
    } finally {
      setLoadingMore(false)
    }
  }

  const loadMoreItems = async () => {
    if (!job?.items_next_cursor) return
    setLoadingMore(true)
    try {
      const page = await api<AttendanceRepairJob>(
        `/api/v1/attendance-repairs/${job.job_id}${queryString({ item_cursor: job.items_next_cursor, item_limit: 500 })}`,
      )
      setJob((current) =>
        current && current.job_id === page.job_id
          ? {
              ...page,
              items: [...(current.items || []), ...(page.items || [])],
            }
          : page,
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to load more outcomes.')
    } finally {
      setLoadingMore(false)
    }
  }

  useEffect(() => {
    void loadTerminal()
    setSelected(new Set())
    setUserCursor(null)
    setCandidates(null)
    setAliasTokens(new Set())
  }, [loadTerminal])

  useEffect(() => {
    void loadJobs()
  }, [loadJobs, revision])

  useEffect(() => {
    // Cohort tokens bind the exact UTC half-open date scope. Never let a
    // preview prepared for one range be submitted after the operator edits it.
    setCandidates(null)
    setAliasTokens(new Set())
  }, [dateScoped, dateFrom, dateTo])

  useEffect(() => {
    if (!job || !activeJobStates.has(job.status) || job.status === 'AWAITING_APPROVAL') return
    const timer = window.setInterval(async () => {
      try {
        const current = await api<AttendanceRepairJob>(
          `/api/v1/attendance-repairs/${job.job_id}`,
        )
        setJob(current)
        if (!activeJobStates.has(current.status)) void loadJobs()
      } catch {
        // Keep the durable last-known ledger visible during transient polling failures.
      }
    }, 4000)
    return () => window.clearInterval(timer)
  }, [job, loadJobs])

  const shownUsers = useMemo(() => {
    const term = query.trim().toLowerCase()
    if (!term) return users
    return users.filter((user) =>
      `${user.display_name} ${user.user_id} ${user.uid} ${user.cnic_masked || ''}`
        .toLowerCase()
        .includes(term),
    )
  }, [query, users])

  const toggleUser = (userKey: string) => {
    setCandidates(null)
    setAliasTokens(new Set())
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(userKey)) next.delete(userKey)
      else if (next.size < 500) next.add(userKey)
      return next
    })
  }

  const queryCandidates = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected.size) return setError('Select at least one employee.')
    if (dateScoped && (!dateFrom || !dateTo))
      return setError('Choose both Pakistan date bounds.')
    setLoading(true)
    setError('')
    try {
      const result = await api<AttendanceRepairCandidates>(
        `/api/v1/devices/${deviceId}/attendance-repair-candidates/query`,
        {
          method: 'POST',
          body: JSON.stringify({
            user_keys: [...selected],
            ...(dateScoped ? { date_from: dateFrom, date_to: dateTo } : {}),
          }),
        },
      )
      setCandidates(result)
      setAliasTokens(new Set())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to build candidates.')
    } finally {
      setLoading(false)
    }
  }

  const prepare = async () => {
    if (!candidates) return
    setLoading(true)
    setError('')
    try {
      const prepared = await api<AttendanceRepairJob>(
        `/api/v1/devices/${deviceId}/attendance-repairs/prepare`,
        {
          method: 'POST',
          body: JSON.stringify({
            targets: candidates.targets.map((target) => ({
              user_key: target.user_key,
              expected_row_version: target.row_version,
              all_provable_history: true,
              cohort_tokens: target.cohorts
                .filter(
                  (cohort) =>
                    cohort.evidence_classification !== 'CURRENT_USER_LINEAGE' &&
                    aliasTokens.has(cohort.cohort_token),
                )
                .map((cohort) => cohort.cohort_token),
            })),
            ...(dateScoped ? { date_from: dateFrom, date_to: dateTo } : {}),
            idempotency_key: idempotency('attendance-repair-prepare'),
          }),
        },
      )
      setJob(prepared)
      const params = new URLSearchParams(window.location.search)
      params.set('tab', 'employee-repair')
      params.set('repair_job', prepared.job_id)
      params.set('device_id', deviceId)
      window.history.pushState(null, '', `${window.location.pathname}?${params}`)
      toast.notice(
        prepared.source_dependency_job_id
          ? 'Source certification is queued; the final preview will be rebuilt automatically.'
          : prepared.status === 'PREPARING_SOURCE'
            ? 'Frozen membership is queued for read-only Oracle classification.'
            : 'Immutable employee repair preview is ready for review.',
      )
      void loadJobs()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to freeze repair preview.')
    } finally {
      setLoading(false)
    }
  }

  const approve = async () => {
    if (!job?.preview_digest || !job.typed_confirmation) return
    setLoading(true)
    setError('')
    try {
      const approved = await api<AttendanceRepairJob>(
        `/api/v1/attendance-repairs/${job.job_id}/approve`,
        {
          method: 'POST',
          body: JSON.stringify({
            reason: reason.trim(),
            password,
            typed_confirmation: confirmation,
            preview_digest: job.preview_digest,
            idempotency_key: idempotency('attendance-repair-approve'),
          }),
        },
      )
      setJob(approved)
      setPassword('')
      setConfirmation('')
      toast.notice('Repair approved. Oracle content verification has started.')
      void loadJobs()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Approval failed.')
    } finally {
      setLoading(false)
    }
  }

  const submitControl = async () => {
    if (!job || !control) return
    setLoading(true)
    setError('')
    try {
      const current = await api<AttendanceRepairJob>(
        `/api/v1/attendance-repairs/${job.job_id}/${control}`,
        {
          method: 'POST',
          body: JSON.stringify({
            reason: controlReason.trim(),
            password: controlPassword,
            idempotency_key: idempotency(`attendance-repair-${control}`),
          }),
        },
      )
      setJob(current)
      setControl(null)
      setControlReason('')
      setControlPassword('')
      toast.notice(`Repair ${control} request was recorded durably.`)
      void loadJobs()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Control request failed.')
    } finally {
      setLoading(false)
    }
  }

  const currentCohorts =
    candidates?.targets.flatMap((target) =>
      target.cohorts.filter(
        (cohort) => cohort.evidence_classification === 'CURRENT_USER_LINEAGE',
      ),
    ) || []
  const explicitAliases =
    candidates?.targets.flatMap((target) =>
      target.cohorts
        .filter((cohort) => cohort.evidence_classification !== 'CURRENT_USER_LINEAGE')
        .map((cohort) => ({ ...cohort, target })),
    ) || []
  const selectedEventCount =
    currentCohorts.reduce((sum, cohort) => sum + cohort.event_count, 0) +
    explicitAliases
      .filter((cohort) => aliasTokens.has(cohort.cohort_token))
      .reduce((sum, cohort) => sum + cohort.event_count, 0)

  return (
    <div
      className="employee-repair-workspace"
      role="tabpanel"
      id="reconciliation-employee-repair-panel"
      aria-labelledby="reconciliation-employee-repair-tab"
    >
      <section className="metric-grid reconciliation-metrics">
        <Metric
          label="Selected employees"
          value={selected.size}
          detail="Maximum 500 on one terminal"
          icon="users"
        />
        <Metric
          label="Frozen events"
          value={job?.totals.events ?? selectedEventCount}
          detail="Physical event UIDs remain unchanged"
          icon="shield"
        />
        <Metric
          label="Repair jobs"
          value={jobs.length}
          detail={`${jobs.filter((row) => activeJobStates.has(row.status)).length} active in this view`}
          icon="refresh"
        />
        <Metric
          label="Needs attention"
          value={preflight?.worker.review_items ?? 0}
          detail="Ambiguous or unsafe events remain unchanged"
          icon="alert"
          tone={preflight?.worker.review_items ? 'warning' : 'positive'}
        />
      </section>

      <section className="panel employee-repair-builder">
        <header className="panel-header">
          <div>
            <p className="eyebrow">EMPLOYEE REPAIR · TERMINAL SCOPED</p>
            <h2>Repair effective attendance identity</h2>
            <p>
              Select current certified employees, review exact historical source cohorts,
              then approve one immutable Oracle correction preview.
            </p>
          </div>
          <button className="button secondary" onClick={() => void loadTerminal()}>
            <Icon name="refresh" /> Refresh certification
          </button>
        </header>

        <label className="employee-repair-terminal">
          Terminal source
          <select
            value={deviceId}
            onChange={(event) => setDeviceId(event.target.value)}
          >
            <option value="">Select one terminal</option>
            {devices.map((device) => (
              <option key={device.connector_id} value={device.connector_id}>
                {device.display_name} · {device.zone_name} · {device.zkt?.serial || 'serial pending'}
              </option>
            ))}
          </select>
        </label>

        {preflight && (
          <>
            <SourceDisclosure current={!preflight.requires_source_reconciliation} />
            <div className="employee-repair-readiness">
              <span>
                <StatusBadge state={preflight.preview_enabled ? 'PREVIEW ENABLED' : 'PREVIEW DISABLED'} />
                <small>Preview gate</small>
              </span>
              <span>
                <StatusBadge state={preflight.execution_enabled ? 'EXECUTION ENABLED' : 'EXECUTION DISABLED'} />
                <small>Mutation gate</small>
              </span>
              <span>
                <StatusBadge state={preflight.oracle.available ? 'ORACLE READY' : 'ORACLE DEGRADED'} />
                <small>{preflight.oracle.error_code || 'ADD-only capability verified'}</small>
              </span>
              <span>
                <StatusBadge state={preflight.terminal?.snapshot_stable ? 'SNAPSHOT STABLE' : 'SNAPSHOT WAITING'} />
                <small>Revision {preflight.terminal?.snapshot_revision ?? '—'}</small>
              </span>
            </div>
            {[...preflight.hard_blockers, ...preflight.waitable_blockers].length > 0 && (
              <div className="employee-repair-blockers">
                {[...preflight.hard_blockers, ...preflight.waitable_blockers].map(
                  (blocker) => (
                    <p key={blocker.code}>
                      <strong>{humanize(blocker.code)}</strong> {blocker.message}
                    </p>
                  ),
                )}
              </div>
            )}
          </>
        )}

        {selectedDevice && users.length > 0 && !job && (
          <form onSubmit={queryCandidates} className="employee-repair-selection">
            <div className="employee-repair-selection-head">
              <label className="search-field">
                <Icon name="search" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search current certified employees"
                  aria-label="Search eligible employees"
                />
              </label>
              <span>{selected.size.toLocaleString()} selected</span>
            </div>
            <div className="employee-repair-user-list" role="listbox" aria-multiselectable="true">
              {shownUsers.map((user) => (
                <label
                  key={user.user_key}
                  role="option"
                  aria-selected={selected.has(user.user_key)}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(user.user_key)}
                    onChange={() => toggleUser(user.user_key)}
                  />
                  <span>
                    <strong>{user.display_name}</strong>
                    <small>
                      {user.cnic_masked} · UID {user.uid} · source user {user.user_id}
                    </small>
                  </span>
                  <code>v{user.row_version}</code>
                </label>
              ))}
            </div>
            {userCursor && (
              <button
                className="button secondary"
                type="button"
                disabled={loadingMore}
                onClick={() => void loadMoreUsers()}
              >
                {loadingMore ? 'Loading employees…' : 'Load more employees'}
              </button>
            )}
            <div className="employee-repair-date-scope">
              <label>
                <input
                  type="checkbox"
                  checked={dateScoped}
                  onChange={(event) => setDateScoped(event.target.checked)}
                />
                Limit to Pakistan dates
              </label>
              {dateScoped && (
                <>
                  <label>
                    From
                    <input
                      type="date"
                      value={dateFrom}
                      onChange={(event) => setDateFrom(event.target.value)}
                    />
                  </label>
                  <label>
                    Through
                    <input
                      type="date"
                      value={dateTo}
                      min={dateFrom}
                      onChange={(event) => setDateTo(event.target.value)}
                    />
                  </label>
                </>
              )}
            </div>
            <button
              className="button primary"
              type="submit"
              disabled={!selected.size || loading || !preflight?.preview_enabled}
            >
              <Icon name="search" /> {loading ? 'Building evidence…' : 'Build repair candidates'}
            </button>
          </form>
        )}

        {candidates && !job && (
          <div className="employee-repair-cohorts">
            <header>
              <div>
                <h3>Exact source cohorts</h3>
                <p>
                  Current-user lineage is included. Historical aliases require an explicit
                  selection and remain unavailable when UID reuse is ambiguous.
                </p>
              </div>
              <strong>{selectedEventCount.toLocaleString()} events</strong>
            </header>
            {candidates.targets.map((target) => (
              <article key={target.user_key}>
                <div className="employee-repair-cohort-target">
                  <strong>{target.display_name}</strong>
                  <span>{target.cnic_masked}</span>
                  <StatusBadge state={target.eligible ? 'ELIGIBLE' : target.exclusion_code || 'BLOCKED'} />
                </div>
                {target.cohorts.map((cohort) => {
                  const current = cohort.evidence_classification === 'CURRENT_USER_LINEAGE'
                  return (
                    <label key={cohort.cohort_token}>
                      <input
                        type="checkbox"
                        checked={current || aliasTokens.has(cohort.cohort_token)}
                        disabled={current || !cohort.selectable}
                        onChange={() =>
                          setAliasTokens((selectedTokens) => {
                            const next = new Set(selectedTokens)
                            if (next.has(cohort.cohort_token)) next.delete(cohort.cohort_token)
                            else next.add(cohort.cohort_token)
                            return next
                          })
                        }
                      />
                      <span>
                        <strong>{humanize(cohort.evidence_classification)}</strong>
                        <small>
                          UID {cohort.source_uid || '—'} · source user{' '}
                          {cohort.source_user_id || '—'} · {dateTime(cohort.first_event_at)} to{' '}
                          {dateTime(cohort.last_event_at)}
                        </small>
                        <small>
                          Device cohort {cohort.source_device_user_key.slice(0, 16)} · manifest{' '}
                          {cohort.source_evidence.terminal_manifest_events.toLocaleString()}/
                          {cohort.event_count.toLocaleString()} · tombstone{' '}
                          {cohort.source_evidence.exact_tombstone ? 'yes' : 'no'} · sources{' '}
                          {cohort.source_evidence.source_types.join(', ') || 'unknown'}
                        </small>
                        <small>
                          Historical identity{' '}
                          {cohort.masked_identity.variants
                            .map(
                              (identity) =>
                                `${identity.display_name_masked || 'name unavailable'} · ${identity.cnic_masked || 'CNIC unavailable'}`,
                            )
                            .join(' / ') || 'unavailable'}
                          {cohort.masked_identity.truncated
                            ? ` / +${cohort.masked_identity.variant_count - cohort.masked_identity.variants.length} more`
                            : ''}
                        </small>
                        {!cohort.selectable && <em>{humanize(cohort.exclusion_code)}</em>}
                      </span>
                      <b>{cohort.event_count.toLocaleString()}</b>
                    </label>
                  )
                })}
              </article>
            ))}
            <div className="employee-repair-actions">
              <button className="button secondary" onClick={() => setCandidates(null)}>
                Change selection
              </button>
              <button
                className="button primary"
                disabled={!selectedEventCount || loading}
                onClick={() => void prepare()}
              >
                {loading ? 'Freezing preview…' : 'Freeze immutable preview'}
              </button>
            </div>
          </div>
        )}

        {error && (
          <div className="inline-retry pattern-blocked" role="alert">
            <Icon name="alert" />
            <div>
              <strong>Employee repair could not continue</strong>
              <p>{error}</p>
            </div>
          </div>
        )}
      </section>

      {job && (
        <section className="panel employee-repair-current-job">
          <header className="panel-header">
            <div>
              <p className="eyebrow">DURABLE JOB LEDGER</p>
              <h2>{humanize(job.status)}</h2>
              <p>
                Created {relativeTime(job.created_at)} by {job.actor} · {job.device_id} · preview{' '}
                {job.preview_digest?.slice(0, 12) || 'preparing'}
                {job.preparation_attempt_count
                  ? ` · ${job.preparation_attempt_count} preview retries`
                  : ''}
                {job.next_attempt_at ? ` · next ${relativeTime(job.next_attempt_at)}` : ''}
              </p>
            </div>
            <div className="employee-repair-job-actions">
              {['QUEUED', 'RUNNING', 'WAITING_ORACLE', 'WAITING_DOWNSTREAM'].includes(job.status) && (
                <button className="button secondary" onClick={() => setControl('pause')}>
                  Pause
                </button>
              )}
              {job.status === 'PAUSED' && (
                <button className="button primary" onClick={() => setControl('resume')}>
                  Resume
                </button>
              )}
              {['NEEDS_ATTENTION', 'COMPLETED_WITH_ATTENTION'].includes(job.status) && (
                <button className="button primary" onClick={() => setControl('retry')}>
                  Retry safe work
                </button>
              )}
              {!['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status) && (
                <button className="button destructive" onClick={() => setControl('cancel')}>
                  Cancel untouched
                </button>
              )}
              <a
                className="button secondary"
                href={`/api/v1/attendance-repairs/${job.job_id}/evidence`}
              >
                Download evidence
              </a>
              <button className="button text-button" onClick={() => setJob(null)}>
                Close
              </button>
            </div>
          </header>
          {job.status === 'PREPARING_SOURCE' && job.source_dependency_job_id && (
            <>
              <SourceDisclosure current={false} />
              <p className="employee-repair-dependency-id">
                Reconciliation dependency <code>{job.source_dependency_job_id}</code>
              </p>
            </>
          )}
          {job.status === 'PREPARING_SOURCE' && !job.source_dependency_job_id && (
            <div className="info-copy pattern-notice">
              <Icon name="search" />
              <div>
                <h3>Oracle content classification is running</h3>
                <p>
                  This read-only phase classifies every frozen event as matching,
                  missing, mismatched, or unsafe before approval becomes available.
                </p>
              </div>
            </div>
          )}
          {job.error_code && (
            <div className="info-copy pattern-blocked">
              <Icon name="alert" />
              <div>
                <h3>{humanize(job.error_code)}</h3>
                <p>{job.error_message || humanize(job.wait_reason)}</p>
              </div>
            </div>
          )}
          <JobLedger
            job={job}
            loadingMore={loadingMore}
            onLoadMore={job.items_next_cursor ? () => void loadMoreItems() : undefined}
          />
          {job.status === 'AWAITING_APPROVAL' && (
            <div className="employee-repair-approval">
              <div className="info-copy pattern-blocked">
                <Icon name="shield" />
                <div>
                  <h3>Approval expires {dateTime(job.preview_expires_at)}</h3>
                  <p>
                    Oracle will be content-verified before ADD activates each identity.
                    Downstream employee/day attendance must also converge before completion.
                  </p>
                </div>
              </div>
              <label>
                Audited reason
                <textarea
                  value={reason}
                  maxLength={500}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="At least 10 characters; do not include CNIC"
                />
              </label>
              <label>
                Type exactly <code>{job.typed_confirmation}</code>
                <input
                  value={confirmation}
                  autoComplete="off"
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </label>
              <label>
                Administrator password
                <input
                  type="password"
                  value={password}
                  autoComplete="current-password"
                  onChange={(event) => setPassword(event.target.value)}
                />
              </label>
              <button
                className="button primary"
                disabled={
                  loading ||
                  reason.trim().length < 10 ||
                  !password ||
                  confirmation !== job.typed_confirmation ||
                  !preflight?.execution_enabled
                }
                onClick={() => void approve()}
              >
                {loading ? 'Verifying approval…' : 'Approve repair and resync'}
              </button>
            </div>
          )}
        </section>
      )}

      <section className="panel employee-repair-history">
        <header className="panel-header">
          <div>
            <h2>Employee repair history</h2>
            <p>Successful employees stay complete when another employee needs review.</p>
          </div>
          <button className="button secondary" onClick={() => void loadJobs()}>
            <Icon name="refresh" /> Refresh ledger
          </button>
        </header>
        <div role="list">
          {jobs.map((row) => (
            <button
              key={row.job_id}
              role="listitem"
              onClick={async () =>
                setJob(
                  await api<AttendanceRepairJob>(
                    `/api/v1/attendance-repairs/${row.job_id}`,
                  ),
                )
              }
            >
              <span>
                <strong>{row.device_id}</strong>
                <small>
                  {row.job_id.slice(0, 12)} · {row.actor} · {relativeTime(row.created_at)}
                </small>
              </span>
              <StatusBadge state={row.status} />
              <span>{row.totals.employees} employees</span>
              <span>{row.totals.completed_events.toLocaleString()} / {row.totals.events.toLocaleString()}</span>
              <Icon name="chevron" />
            </button>
          ))}
          {!jobs.length && <p className="empty-state">No employee repair jobs yet.</p>}
        </div>
      </section>

      {control && job && (
        <Dialog
          titleId="attendance-repair-control-title"
          title={`${humanize(control)} employee repair`}
          description={job.job_id}
          onClose={() => setControl(null)}
        >
          <div className="dialog-body">
            <div className="info-copy pattern-waiting">
              <Icon name="shield" />
              <div>
                <h3>Oracle-committed work always forward-completes</h3>
                <p>
                  Cancellation applies only to untouched events. It never rolls back a
                  committed identity correction or changes physical punch facts.
                </p>
              </div>
            </div>
            <label>
              Audited reason
              <textarea
                value={controlReason}
                maxLength={500}
                onChange={(event) => setControlReason(event.target.value)}
              />
            </label>
            <label>
              Administrator password
              <input
                type="password"
                value={controlPassword}
                autoComplete="current-password"
                onChange={(event) => setControlPassword(event.target.value)}
              />
            </label>
            <div className="dialog-actions">
              <button className="button secondary" onClick={() => setControl(null)}>
                Keep current state
              </button>
              <button
                className={control === 'cancel' ? 'button destructive' : 'button primary'}
                disabled={loading || controlReason.trim().length < 10 || !controlPassword}
                onClick={() => void submitControl()}
              >
                Confirm {control}
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  )
}
