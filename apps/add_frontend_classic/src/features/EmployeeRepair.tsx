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

const pakistanDate = new Intl.DateTimeFormat('en-PK', {
  timeZone: 'Asia/Karachi',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
})

const repairGuide = [
  { label: 'Machine', detail: 'Choose where the employee punches' },
  { label: 'Employee', detail: 'Choose one or more people' },
  { label: 'Check', detail: 'Review the past punches' },
  { label: 'Confirm', detail: 'Approve the final list' },
  { label: 'Done', detail: 'Watch the repair finish' },
]

const jobStatusCopy: Record<string, string> = {
  PREPARING_SOURCE: 'Preparing final check',
  AWAITING_APPROVAL: 'Waiting for approval',
  QUEUED: 'Starting repair',
  RUNNING: 'Repairing attendance',
  WAITING_ORACLE: 'Updating attendance records',
  WAITING_DOWNSTREAM: 'Updating reports',
  PAUSED: 'Paused',
  NEEDS_ATTENTION: 'Needs your help',
  COMPLETED: 'Complete',
  COMPLETED_WITH_ATTENTION: 'Finished with items to review',
  CANCELLED: 'Stopped',
}

const targetStatusCopy: Record<string, string> = {
  SOURCE_PENDING: 'Waiting for machine scan',
  FROZEN: 'Ready for approval',
  RUNNING: 'Being repaired',
  COMPLETE: 'Complete',
  COMPLETED: 'Complete',
  NEEDS_REVIEW: 'Needs review',
  CANCELLED: 'Stopped',
}

const controlCopy: Record<
  ControlAction,
  { title: string; button: string; description: string }
> = {
  pause: {
    title: 'Pause this repair',
    button: 'Pause repair',
    description: 'The repair will stop after the current safe checkpoint. You can continue it later.',
  },
  resume: {
    title: 'Continue this repair',
    button: 'Continue repair',
    description: 'The repair will continue from the last saved checkpoint.',
  },
  cancel: {
    title: 'Stop remaining work',
    button: 'Stop remaining work',
    description: 'Only punches that have not been started will be stopped.',
  },
  retry: {
    title: 'Try this repair again',
    button: 'Try again',
    description: 'The saved repair will continue from the last safe checkpoint.',
  },
}

function plural(count: number, one: string, many = `${one}s`) {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`
}

function formatPunchDates(first: string | null, last: string | null) {
  if (!first && !last) return 'Date unavailable'
  const start = first ? pakistanDate.format(new Date(first)) : 'Unknown'
  const end = last ? pakistanDate.format(new Date(last)) : start
  return start === end ? start : `${start} – ${end}`
}

function friendlyBlocker(code: string) {
  const messages: Record<string, string> = {
    PREVIEW_DISABLED: 'Past-attendance checking is temporarily turned off.',
    CONNECTOR_INACTIVE: 'This attendance machine is not connected to ADD.',
    DUPLICATE_SERIAL_QUARANTINE: 'This machine needs an administrator check before it can be used.',
    SERIAL_UNKNOWN: 'ADD has not confirmed this machine yet.',
    IDENTITY_SNAPSHOT_UNSTABLE: 'The employee list is still updating. Please check again shortly.',
    LIVE_ORDS_BACKLOG_HIGH: 'New punches are still syncing. Please wait for them to finish.',
    SOURCE_RECONCILIATION_DISABLED: 'The machine scan is temporarily unavailable.',
    SOURCE_RECERTIFICATION_REQUIRED: 'The machine has new punches. We will scan it before the final check.',
  }
  return messages[code] || 'This machine is not ready yet. Please check again or ask support.'
}

function friendlyRepairError(code?: string | null) {
  if (code?.startsWith('ORDS_'))
    return {
      title: 'The attendance database is not available right now',
      body: 'Your repair is safely saved. Try again when the connection is back.',
    }
  const messages: Record<string, { title: string; body: string }> = {
    TARGET_DRIFT: {
      title: 'The employee details changed',
      body: 'Refresh the employee list and review the repair again. Nothing was changed.',
    },
    SOURCE_DRIFT: {
      title: 'New punches arrived',
      body: 'Run the machine check again so the final list includes the latest punches.',
    },
    COHORT_DRIFT: {
      title: 'The past-punch list changed',
      body: 'Refresh and review the list again. Nothing unsafe was included.',
    },
    SOURCE_DEPENDENCY_FAILED: {
      title: 'The machine scan needs help',
      body: 'Try the scan again. Your repair request is saved and no punch was changed.',
    },
    DOWNSTREAM_TIMEOUT: {
      title: 'Attendance reports are still updating',
      body: 'The corrected punches are safe. Try again later to confirm the reports.',
    },
    DOWNSTREAM_PENDING: {
      title: 'Attendance reports are still updating',
      body: 'The corrected punches are safe. ADD will keep checking the reports.',
    },
    RETRY_EXHAUSTED: {
      title: 'The repair needs an administrator check',
      body: 'The saved repair stopped after several safe retries. No unsafe overwrite was made.',
    },
  }
  return (
    messages[code || ''] || {
      title: 'This repair needs your help',
      body: 'The repair stopped safely. Review the details or try again; unchanged punches remain safe.',
    }
  )
}

function jobExperience(job: AttendanceRepairJob) {
  if (job.status === 'NEEDS_ATTENTION' || job.status === 'COMPLETED_WITH_ATTENTION') {
    const error = friendlyRepairError(job.error_code)
    return { ...error, icon: 'alert' as const, tone: 'attention' }
  }
  if (job.status === 'PREPARING_SOURCE' && job.source_dependency_job_id)
    return {
      title: 'Scanning the attendance machine',
      body: 'We are safely reading all saved punches. People can keep using the machine while this runs.',
      icon: 'refresh' as const,
      tone: 'working',
    }
  if (job.status === 'PREPARING_SOURCE')
    return {
      title: 'Checking the selected punches',
      body: 'We are comparing the punches with the attendance database. Nothing is being changed yet.',
      icon: 'search' as const,
      tone: 'working',
    }
  if (job.status === 'AWAITING_APPROVAL')
    return {
      title: 'Ready for your final confirmation',
      body: 'The punch list is fixed and ready. Check the summary below before starting the repair.',
      icon: 'shield' as const,
      tone: 'ready',
    }
  if (['QUEUED', 'RUNNING', 'WAITING_ORACLE'].includes(job.status))
    return {
      title: 'Repairing past attendance',
      body: 'We are correcting the saved name and CNIC. Punch time, machine, and punch type stay unchanged.',
      icon: 'refresh' as const,
      tone: 'working',
    }
  if (job.status === 'WAITING_DOWNSTREAM')
    return {
      title: 'Updating attendance reports',
      body: 'The saved punches are corrected. We are checking that daily attendance also shows the fix.',
      icon: 'clock' as const,
      tone: 'working',
    }
  if (job.status === 'PAUSED')
    return {
      title: 'This repair is paused',
      body: 'All progress is saved. Continue when you are ready.',
      icon: 'pause' as const,
      tone: 'waiting',
    }
  if (job.status === 'COMPLETED')
    return {
      title: 'Past attendance is fixed',
      body: 'The saved punches and the attendance reports now show the corrected employee details.',
      icon: 'check' as const,
      tone: 'complete',
    }
  if (job.status === 'CANCELLED')
    return {
      title: 'Remaining work was stopped',
      body: 'Untouched punches were left as they were. Any work already completed remains safely saved.',
      icon: 'x' as const,
      tone: 'waiting',
    }
  return {
    title: jobStatusCopy[job.status] || 'Repair in progress',
    body: 'Your progress is saved. This page will update automatically.',
    icon: 'refresh' as const,
    tone: 'working',
  }
}

function repairProcessIndex(job: AttendanceRepairJob) {
  if (job.status === 'PREPARING_SOURCE' && job.source_dependency_job_id) return 0
  if (job.status === 'PREPARING_SOURCE') return 1
  if (job.status === 'AWAITING_APPROVAL') return 2
  if (job.status === 'WAITING_DOWNSTREAM') return 4
  if (job.status === 'COMPLETED') return 5
  if (job.approved_at) return 3
  return job.source_dependency_job_id ? 0 : 1
}

function repairProgress(job: AttendanceRepairJob) {
  const stage = repairProcessIndex(job)
  const stageMinimum = [10, 28, 45, 62, 86, 100][stage] || 8
  if (!job.totals.events) return stageMinimum
  const eventProgress = Math.round(
    (job.totals.completed_events / Math.max(job.totals.events, 1)) * 100,
  )
  return Math.min(100, Math.max(stageMinimum, eventProgress))
}

function GuideSteps({ current, complete }: { current: number; complete: boolean }) {
  return (
    <nav className="employee-repair-guide" aria-label="Repair steps" tabIndex={0}>
      <ol>
        {repairGuide.map((step, index) => {
          const number = index + 1
          const done = complete || number < current
          const active = !complete && number === current
          return (
            <li
              key={step.label}
              className={done ? 'is-done' : active ? 'is-active' : ''}
              aria-current={active ? 'step' : undefined}
            >
              <span>{done ? <Icon name="check" /> : number}</span>
              <div>
                <strong>{step.label}</strong>
                <small>{step.detail}</small>
              </div>
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

function SourceDisclosure({ current }: { current: boolean }) {
  return (
    <div className={`employee-repair-source is-${current ? 'ready' : 'scan'}`} role="status">
      <Icon name={current ? 'shield' : 'refresh'} />
      <div>
        <h3>{current ? 'Machine history is ready' : 'We will scan this machine first'}</h3>
        <p>
          {current
            ? 'ADD can now check this employee’s past punches safely.'
            : 'The scan only reads punches. People can keep punching, and the scan may also find missing punches for other employees.'}
        </p>
      </div>
    </div>
  )
}

function ReadinessSummary({ preflight }: { preflight: AttendanceRepairPreflight }) {
  const blockers = [...preflight.hard_blockers, ...preflight.waitable_blockers]
  const hardBlocked = preflight.hard_blockers.length > 0
  return (
    <div className={`employee-repair-readiness-summary is-${hardBlocked ? 'blocked' : blockers.length ? 'waiting' : 'ready'}`}>
      <div className="employee-repair-readiness-main">
        <Icon name={hardBlocked ? 'alert' : blockers.length ? 'clock' : 'check'} />
        <div>
          <strong>
            {hardBlocked ? 'This machine is not ready' : blockers.length ? 'Ready after one quick check' : 'Ready to continue'}
          </strong>
          <span>
            {hardBlocked
              ? 'Nothing can be changed until the issue below is fixed.'
              : blockers.length
                ? 'ADD will complete the required check before showing the final list.'
                : 'The employee list and attendance connection are available.'}
          </span>
        </div>
      </div>
      {blockers.length > 0 && (
        <ul>
          {blockers.map((blocker) => (
            <li key={blocker.code}>{friendlyBlocker(blocker.code)}</li>
          ))}
        </ul>
      )}
      <details>
        <summary>System checks</summary>
        <div className="employee-repair-system-checks">
          <span className={preflight.preview_enabled ? 'is-good' : 'is-bad'}>
            <Icon name={preflight.preview_enabled ? 'check' : 'x'} /> Past-punch check
          </span>
          <span className={preflight.execution_enabled ? 'is-good' : 'is-wait'}>
            <Icon name={preflight.execution_enabled ? 'check' : 'clock'} /> Repairs
          </span>
          <span className={preflight.oracle.available ? 'is-good' : 'is-bad'}>
            <Icon name={preflight.oracle.available ? 'check' : 'x'} /> Attendance database
          </span>
          <span className={preflight.terminal?.snapshot_stable ? 'is-good' : 'is-wait'}>
            <Icon name={preflight.terminal?.snapshot_stable ? 'check' : 'clock'} /> Employee list
          </span>
        </div>
      </details>
    </div>
  )
}

function JobProgress({
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
  const experience = jobExperience(job)
  const processIndex = repairProcessIndex(job)
  const percent = repairProgress(job)
  const process = [
    ['Scan machine', 'Read saved punches'],
    ['Check punches', 'Compare without changing'],
    ['Your approval', 'Confirm the final list'],
    ['Fix records', 'Update name and CNIC'],
    ['Update reports', 'Confirm daily attendance'],
  ]
  return (
    <div className="employee-repair-progress">
      <div
        className={`employee-repair-live-state is-${experience.tone}`}
        role="status"
        aria-live="polite"
      >
        <span className="employee-repair-live-icon"><Icon name={experience.icon} /></span>
        <div>
          <span className="employee-repair-kicker">{jobStatusCopy[job.status] || 'Repair update'}</span>
          <h3>{experience.title}</h3>
          <p>{experience.body}</p>
        </div>
        <strong>{percent}%</strong>
      </div>

      <div
        className="employee-repair-progress-track"
        role="progressbar"
        aria-label="Repair progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={`${percent}% complete`}
      >
        <span style={{ width: `${percent}%` }} />
      </div>

      <ol className="employee-repair-process" aria-label="Current repair progress">
        {process.map(([label, detail], index) => {
          const done = processIndex > index
          const active = processIndex === index && job.status !== 'COMPLETED'
          return (
            <li key={label} className={done ? 'is-done' : active ? 'is-active' : ''}>
              <span>{done ? <Icon name="check" /> : index + 1}</span>
              <div><strong>{label}</strong><small>{detail}</small></div>
            </li>
          )
        })}
      </ol>

      <div className="employee-repair-result-summary" aria-label="Repair summary">
        <div><strong>{job.totals.employees.toLocaleString()}</strong><span>{job.totals.employees === 1 ? 'employee' : 'employees'}</span></div>
        <div><strong>{job.totals.events.toLocaleString()}</strong><span>{job.totals.events === 1 ? 'punch' : 'punches'}</span></div>
        <div><strong>{job.totals.completed_events.toLocaleString()}</strong><span>finished</span></div>
        <div><strong>{job.downstream_impact?.employee_days.toLocaleString() ?? '—'}</strong><span>attendance days</span></div>
      </div>

      <h3 className="employee-repair-list-title">Employees in this repair</h3>
      <div className="employee-repair-target-ledger" role="list">
        {job.targets.map((target) => (
          <article key={target.user_key} role="listitem">
            <span className="employee-repair-person-icon"><Icon name="users" /></span>
            <div className="employee-repair-target-name">
              <strong>{target.display_name}</strong>
              <small>{target.cnic_masked}</small>
            </div>
            <div className="employee-repair-target-count">
              <strong>{target.completed_event_count.toLocaleString()} / {target.event_count.toLocaleString()}</strong>
              <small>punches finished</small>
            </div>
            <span className={`employee-repair-friendly-state is-${target.attention_event_count ? 'attention' : target.completed_event_count === target.event_count && target.event_count ? 'complete' : 'working'}`}>
              {target.attention_event_count
                ? `${target.attention_event_count} need review`
                : targetStatusCopy[target.status] || humanize(target.status)}
            </span>
          </article>
        ))}
      </div>

      <details className="employee-repair-technical-details">
        <summary>Technical details for support</summary>
        <dl>
          <div><dt>Repair ID</dt><dd><code>{job.job_id}</code></dd></div>
          <div><dt>Internal stage</dt><dd><StatusBadge state={job.phase} /></dd></div>
          <div><dt>Started by</dt><dd>{job.actor}</dd></div>
          <div><dt>Machine ID</dt><dd>{job.device_id}</dd></div>
        </dl>
        {job.error_code && (
          <div className="employee-repair-technical-error">
            <strong>{job.error_code}</strong>
            <p>{job.error_message || humanize(job.wait_reason)}</p>
          </div>
        )}
        {classifications.length > 0 && (
          <div className="employee-repair-classifications" aria-label="Database comparison results">
            {classifications.map(([state, count]) => (
              <span key={state}><StatusBadge state={state} /> <strong>{count.toLocaleString()}</strong></span>
            ))}
          </div>
        )}
        {job.items && job.items.length > 0 && (
          <details className="employee-repair-items">
            <summary>Individual punch results ({job.items.length.toLocaleString()} loaded)</summary>
          <div role="list">
            {job.items.map((item) => (
              <article key={item.event_uid} role="listitem">
                <span>
                  <code>{item.event_uid.slice(0, 12)}…</code>
                  <small>{targetNames.get(item.user_key || '') || 'Employee unavailable'}</small>
                </span>
                <span>{dateTime(item.event_time)}</span>
                <StatusBadge state={item.state} />
                <small>
                  {item.error_code || item.outcome || humanize(item.oracle_classification)} · database{' '}
                  {item.oracle_attempt_count} · reports {item.downstream_attempt_count}
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
              {loadingMore ? 'Loading results…' : 'Load 500 more results'}
            </button>
          )}
          </details>
        )}
      </details>
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
    } catch {
      setError('We could not load past repairs. Please refresh and try again.')
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
    } catch {
      setError('We could not check this machine. Please try again.')
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
    } catch {
      setError('We could not load more employees. Please try again.')
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
    } catch {
      setError('We could not load more punch results. Please try again.')
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
    if (!selected.size) return setError('Choose at least one employee to continue.')
    if (dateScoped && (!dateFrom || !dateTo))
      return setError('Choose both the start date and end date.')
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
    } catch {
      setError('We could not check the past punches. Please try again.')
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
          ? 'The machine scan has started. You can leave this page; progress is saved.'
          : prepared.status === 'PREPARING_SOURCE'
            ? 'We are checking the selected punches now. Nothing is being changed yet.'
            : 'Your final check is ready.',
      )
      void loadJobs()
    } catch {
      setError('We could not prepare the final check. Nothing was changed.')
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
      toast.notice('Repair started. This page will keep showing the latest progress.')
      void loadJobs()
    } catch {
      setError('We could not start the repair. Nothing new was changed.')
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
      toast.notice(`${controlCopy[control].button} request saved.`)
      void loadJobs()
    } catch {
      setError('We could not save that request. Please try again.')
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

  const selectedEmployees = users.filter((user) => selected.has(user.user_key))
  const activeRepair = jobs.find(
    (row) => row.connector_id === deviceId && activeJobStates.has(row.status),
  )
  const jobIsComplete = job?.status === 'COMPLETED'
  const currentStep = job
    ? job.status === 'AWAITING_APPROVAL'
      ? 4
      : job.approved_at || ['QUEUED', 'RUNNING', 'WAITING_ORACLE', 'WAITING_DOWNSTREAM', 'COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status)
        ? 5
        : 3
    : candidates
      ? 3
      : deviceId
        ? 2
        : 1

  const startAnotherRepair = () => {
    setJob(null)
    setCandidates(null)
    setSelected(new Set())
    setAliasTokens(new Set())
    setReason('')
    setPassword('')
    setConfirmation('')
    setError('')
    const params = new URLSearchParams(window.location.search)
    params.delete('repair_job')
    params.set('tab', 'employee-repair')
    window.history.pushState(null, '', `${window.location.pathname}?${params}`)
  }

  const openRepair = async (jobId: string) => {
    setLoading(true)
    setError('')
    try {
      const detail = await api<AttendanceRepairJob>(
        `/api/v1/attendance-repairs/${jobId}`,
      )
      setJob(detail)
      setDeviceId(detail.connector_id)
      const params = new URLSearchParams(window.location.search)
      params.set('tab', 'employee-repair')
      params.set('repair_job', detail.job_id)
      params.set('device_id', detail.connector_id)
      window.history.pushState(null, '', `${window.location.pathname}?${params}`)
    } catch {
      setError('We could not open that repair. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      className="employee-repair-workspace"
      role="tabpanel"
      id="reconciliation-employee-repair-panel"
      aria-labelledby="reconciliation-employee-repair-tab"
    >
      <section className="employee-repair-intro" aria-labelledby="employee-repair-title">
        <span className="employee-repair-intro-icon"><Icon name="refresh" /></span>
        <div>
          <p className="eyebrow">FIX PAST ATTENDANCE</p>
          <h2 id="employee-repair-title">Fix past attendance for an employee</h2>
          <p>
            Use this when an employee’s name or CNIC was wrong, or when old punches did not
            reach the attendance database.
          </p>
        </div>
        <ul aria-label="What stays safe">
          <li><Icon name="shield" /><span><strong>Punch details stay safe</strong>Time, machine and punch type never change.</span></li>
          <li><Icon name="users" /><span><strong>People can keep punching</strong>The attendance machine stays available.</span></li>
          <li><Icon name="check" /><span><strong>You approve first</strong>No repair starts before the final check.</span></li>
        </ul>
      </section>

      <GuideSteps current={currentStep} complete={Boolean(jobIsComplete)} />

      {!job && (
        <section className="panel employee-repair-builder" aria-labelledby="employee-repair-builder-title">
          <header className="employee-repair-section-heading">
            <span>1</span>
            <div>
              <p className="eyebrow">FIRST, CHOOSE THE MACHINE</p>
              <h3 id="employee-repair-builder-title">Which machine has these punches?</h3>
              <p>Each repair is for one attendance machine. You can repair another machine afterward.</p>
            </div>
          </header>

          <div className="employee-repair-machine-row">
            <label className="employee-repair-terminal">
              Attendance machine
              <select value={deviceId} onChange={(event) => setDeviceId(event.target.value)}>
                <option value="">Choose a machine</option>
                {devices.map((device) => (
                  <option key={device.connector_id} value={device.connector_id}>
                    {device.display_name} · {device.zone_name}
                  </option>
                ))}
              </select>
              <small>{selectedDevice?.zkt?.serial ? `Machine serial ${selectedDevice.zkt.serial}` : 'Choose the machine the employee uses.'}</small>
            </label>
            {deviceId && (
              <button className="button secondary" type="button" disabled={loading} onClick={() => void loadTerminal()}>
                <Icon name="refresh" /> Check again
              </button>
            )}
          </div>

          {!deviceId && (
            <div className="employee-repair-empty-step">
              <Icon name="terminal" />
              <div><strong>Start by choosing a machine</strong><p>The employee list will appear here.</p></div>
            </div>
          )}

          {deviceId && loading && !preflight && (
            <div className="employee-repair-loading" role="status">
              <span /><span /><span /> Checking the machine…
            </div>
          )}

          {preflight && (
            <>
              <SourceDisclosure current={!preflight.requires_source_reconciliation} />
              <ReadinessSummary preflight={preflight} />
            </>
          )}

          {error && (
            <div className="employee-repair-friendly-error" role="alert">
              <Icon name="alert" />
              <div><strong>We could not continue</strong><p>{error}</p></div>
            </div>
          )}

          {activeRepair && !candidates && (
            <div className="employee-repair-active-reminder">
              <Icon name="refresh" />
              <div>
                <strong>A repair is already open for this machine</strong>
                <p>Open it to see the latest progress before starting another one.</p>
              </div>
              <button
                className="button primary"
                disabled={loading}
                onClick={() => void openRepair(activeRepair.job_id)}
              >
                {loading ? 'Opening…' : 'View progress'}
              </button>
            </div>
          )}

          {selectedDevice && users.length > 0 && !candidates && !activeRepair && (
            <form onSubmit={queryCandidates} className="employee-repair-selection">
              <header className="employee-repair-section-heading">
                <span>2</span>
                <div>
                  <p className="eyebrow">NEXT, CHOOSE THE EMPLOYEE</p>
                  <h3>Whose past attendance should be fixed?</h3>
                  <p>You can choose up to 500 employees from this machine.</p>
                </div>
              </header>

              <div className="employee-repair-selection-head">
                <label className="search-field">
                  <Icon name="search" />
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search by name, CNIC or employee ID"
                    aria-label="Search employees"
                  />
                </label>
                <span aria-live="polite">{plural(selected.size, 'employee')} selected</span>
              </div>

              <div className="employee-repair-user-list" role="list" aria-label="Employees">
                {shownUsers.map((user) => (
                  <label key={user.user_key} role="listitem">
                    <input
                      type="checkbox"
                      checked={selected.has(user.user_key)}
                      aria-label={`Select ${user.display_name}`}
                      onChange={() => toggleUser(user.user_key)}
                    />
                    <span className="employee-repair-avatar" aria-hidden="true">{user.display_name.trim().slice(0, 1).toUpperCase()}</span>
                    <span>
                      <strong>{user.display_name}</strong>
                      <small>{user.cnic_masked || 'CNIC unavailable'} · Employee ID {user.user_id}</small>
                    </span>
                    <span className="employee-repair-row-action">{selected.has(user.user_key) ? 'Selected' : 'Choose'}</span>
                  </label>
                ))}
                {!shownUsers.length && (
                  <div className="employee-repair-no-results"><Icon name="search" /><span><strong>No employee found</strong><small>Try a different name, CNIC or employee ID.</small></span></div>
                )}
              </div>

              {userCursor && (
                <button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadMoreUsers()}>
                  {loadingMore ? 'Loading employees…' : 'Show more employees'}
                </button>
              )}

              <fieldset className="employee-repair-date-scope">
                <legend>Which dates should we check?</legend>
                <label className={!dateScoped ? 'is-selected' : ''}>
                  <input type="radio" name="repair-date-scope" checked={!dateScoped} onChange={() => setDateScoped(false)} />
                  <span><strong>All available attendance</strong><small>Recommended when the wrong details were used for a long time.</small></span>
                </label>
                <label className={dateScoped ? 'is-selected' : ''}>
                  <input type="radio" name="repair-date-scope" checked={dateScoped} onChange={() => setDateScoped(true)} />
                  <span><strong>Only between selected dates</strong><small>Use this when you know exactly when the problem happened.</small></span>
                </label>
                {dateScoped && (
                  <div className="employee-repair-date-fields">
                    <label>Start date<input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
                    <label>End date<input type="date" value={dateTo} min={dateFrom} onChange={(event) => setDateTo(event.target.value)} /></label>
                    <small>Dates use Pakistan time and include the full end date.</small>
                  </div>
                )}
              </fieldset>

              <div className="employee-repair-selection-bar" aria-live="polite">
                <div>
                  <strong>{plural(selected.size, 'employee')} selected</strong>
                  <span>{selectedEmployees.slice(0, 3).map((user) => user.display_name).join(', ')}{selected.size > 3 ? ` and ${selected.size - 3} more` : ''}</span>
                </div>
                <button
                  className="button primary large"
                  type="submit"
                  disabled={!selected.size || loading || !preflight?.preview_enabled || Boolean(preflight?.hard_blockers.length)}
                >
                  <Icon name="search" /> {loading ? 'Checking past punches…' : 'Review past punches'}
                </button>
              </div>
            </form>
          )}

          {selectedDevice && !loading && preflight && !users.length && !activeRepair && (
            <div className="employee-repair-empty-step">
              <Icon name="users" />
              <div><strong>No eligible employees found</strong><p>Refresh the machine’s employee list, or resolve incomplete employee details first.</p></div>
            </div>
          )}

          {candidates && (
            <div className="employee-repair-review">
              <header className="employee-repair-section-heading">
                <span>3</span>
                <div>
                  <p className="eyebrow">CHECK BEFORE CONTINUING</p>
                  <h3>Review the past punches we found</h3>
                  <p>These punches will use each employee’s current name and CNIC.</p>
                </div>
                <strong className="employee-repair-found-count">{plural(selectedEventCount, 'punch', 'punches')}</strong>
              </header>

              {!candidates.source_current && <SourceDisclosure current={false} />}

              <div className="employee-repair-safety-note">
                <Icon name="shield" />
                <div><strong>Only the employee name and CNIC can be fixed</strong><p>Punch time, machine, punch type and original punch ID stay exactly the same.</p></div>
              </div>

              <div className="employee-repair-review-list">
                {candidates.targets.map((target) => {
                  const automatic = target.cohorts.filter((cohort) => cohort.evidence_classification === 'CURRENT_USER_LINEAGE')
                  const older = target.cohorts.filter((cohort) => cohort.evidence_classification !== 'CURRENT_USER_LINEAGE')
                  return (
                    <article key={target.user_key}>
                      <header>
                        <span className="employee-repair-avatar" aria-hidden="true">{target.display_name.trim().slice(0, 1).toUpperCase()}</span>
                        <div><strong>{target.display_name}</strong><small>Current CNIC {target.cnic_masked || 'unavailable'}</small></div>
                        <span className={`employee-repair-friendly-state is-${target.eligible ? 'complete' : 'attention'}`}>{target.eligible ? 'Ready' : 'Cannot include'}</span>
                      </header>

                      <div className="employee-repair-included-punches">
                        {automatic.map((cohort) => (
                          <div key={cohort.cohort_token}>
                            <Icon name="check" />
                            <span><strong>Included automatically</strong><small>{formatPunchDates(cohort.first_event_at, cohort.last_event_at)}</small></span>
                            <b>{plural(cohort.event_count, 'punch', 'punches')}</b>
                          </div>
                        ))}
                      </div>

                      {older.length > 0 && (
                        <details className="employee-repair-older-records">
                          <summary>Possible older employee records ({older.length})</summary>
                          <p>Select an older record only when you know it belongs to this employee. Unsafe records cannot be selected.</p>
                          {older.map((cohort) => (
                            <label key={cohort.cohort_token} className={!cohort.selectable ? 'is-disabled' : ''}>
                              <input
                                type="checkbox"
                                checked={aliasTokens.has(cohort.cohort_token)}
                                disabled={!cohort.selectable}
                                aria-label={`Include older record for ${target.display_name} with ${cohort.event_count} punches`}
                                onChange={() => setAliasTokens((selectedTokens) => {
                                  const next = new Set(selectedTokens)
                                  if (next.has(cohort.cohort_token)) next.delete(cohort.cohort_token)
                                  else next.add(cohort.cohort_token)
                                  return next
                                })}
                              />
                              <span>
                                <strong>{formatPunchDates(cohort.first_event_at, cohort.last_event_at)}</strong>
                                <small>
                                  Previous details {cohort.masked_identity.variants.map((identity) => `${identity.display_name_masked || 'name unavailable'} · ${identity.cnic_masked || 'CNIC unavailable'}`).join(' / ') || 'unavailable'}
                                </small>
                                {!cohort.selectable && <em>Not safe to include automatically</em>}
                              </span>
                              <b>{plural(cohort.event_count, 'punch', 'punches')}</b>
                            </label>
                          ))}
                        </details>
                      )}

                      <details className="employee-repair-proof">
                        <summary>Why ADD trusts these punches</summary>
                        <p>The machine record, saved punch ID and employee link all agree. Technical source: {target.cohorts.flatMap((cohort) => cohort.source_evidence.source_types).filter(Boolean).join(', ') || 'machine history'}.</p>
                      </details>
                    </article>
                  )
                })}
              </div>

              <div className="employee-repair-actions">
                <button className="button secondary" type="button" onClick={() => setCandidates(null)}>Back to employees</button>
                <button className="button primary large" type="button" disabled={!selectedEventCount || loading} onClick={() => void prepare()}>
                  <Icon name="shield" /> {loading ? 'Preparing final check…' : 'Prepare final check'}
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {job && (
        <section className="panel employee-repair-current-job" aria-labelledby="employee-repair-progress-title">
          <header className="panel-header employee-repair-job-header">
            <div>
              <p className="eyebrow">LIVE REPAIR STATUS</p>
              <h2 id="employee-repair-progress-title">{jobStatusCopy[job.status] || 'Repair progress'}</h2>
              <p>Started {relativeTime(job.created_at)}. Progress is saved automatically.</p>
            </div>
            <div className="employee-repair-job-actions">
              {['QUEUED', 'RUNNING', 'WAITING_ORACLE', 'WAITING_DOWNSTREAM'].includes(job.status) && (
                <button className="button secondary" onClick={() => setControl('pause')}><Icon name="pause" /> Pause</button>
              )}
              {job.status === 'PAUSED' && (
                <button className="button primary" onClick={() => setControl('resume')}><Icon name="refresh" /> Continue</button>
              )}
              {['NEEDS_ATTENTION', 'COMPLETED_WITH_ATTENTION'].includes(job.status) && (
                <button className="button primary" onClick={() => setControl('retry')}><Icon name="refresh" /> Try again</button>
              )}
              {!['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status) && (
                <button className="button destructive" onClick={() => setControl('cancel')}>Stop remaining work</button>
              )}
              <a className="button secondary" href={`/api/v1/attendance-repairs/${job.job_id}/evidence`}><Icon name="shield" /> Download proof</a>
              {['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status) && (
                <button className="button primary" onClick={startAnotherRepair}>Start another repair</button>
              )}
            </div>
          </header>
          {error && (
            <div className="employee-repair-friendly-error" role="alert">
              <Icon name="alert" />
              <div><strong>We could not continue</strong><p>{error}</p></div>
            </div>
          )}
          <JobProgress
            job={job}
            loadingMore={loadingMore}
            onLoadMore={job.items_next_cursor ? () => void loadMoreItems() : undefined}
          />
          {job.status === 'AWAITING_APPROVAL' && (
            <form className="employee-repair-approval" onSubmit={(event) => { event.preventDefault(); void approve() }}>
              <header>
                <span><Icon name="shield" /></span>
                <div>
                  <p className="eyebrow">FINAL CONFIRMATION</p>
                  <h3>Check once more, then start the repair</h3>
                  <p>This final list expires {dateTime(job.preview_expires_at)}.</p>
                </div>
              </header>
              <div className="employee-repair-approval-summary">
                <div><strong>{job.totals.employees.toLocaleString()}</strong><span>{job.totals.employees === 1 ? 'employee' : 'employees'}</span></div>
                <div><strong>{job.totals.events.toLocaleString()}</strong><span>{job.totals.events === 1 ? 'punch' : 'punches'}</span></div>
                <div><strong>{job.downstream_impact?.employee_days.toLocaleString() ?? '—'}</strong><span>attendance days</span></div>
              </div>
              <ul className="employee-repair-approval-safety">
                <li><Icon name="check" /> Punch times and punch types will not change.</li>
                <li><Icon name="check" /> Existing punch IDs will be kept.</li>
                <li><Icon name="check" /> ADD will check the final attendance reports before marking this complete.</li>
              </ul>
              <label>
                Why is this repair needed?
                <textarea
                  value={reason}
                  maxLength={500}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Example: The employee’s name and CNIC were corrected on the machine. Do not type the CNIC here."
                />
                <small>Write at least 10 characters. Do not include the employee’s CNIC.</small>
              </label>
              <label>
                Type this confirmation phrase
                <code className="employee-repair-confirmation-phrase">{job.typed_confirmation}</code>
                <input
                  value={confirmation}
                  autoComplete="off"
                  aria-label="Confirmation phrase"
                  onChange={(event) => setConfirmation(event.target.value)}
                />
                <small>This protects against starting the wrong repair by mistake.</small>
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
              <div className="employee-repair-approval-checks" aria-label="Confirmation progress" aria-live="polite">
                <span className={reason.trim().length >= 10 ? 'is-done' : ''}>
                  <Icon name={reason.trim().length >= 10 ? 'check' : 'clock'} /> Add a reason
                </span>
                <span className={confirmation === job.typed_confirmation ? 'is-done' : ''}>
                  <Icon name={confirmation === job.typed_confirmation ? 'check' : 'clock'} /> Match the phrase
                </span>
                <span className={password ? 'is-done' : ''}>
                  <Icon name={password ? 'check' : 'clock'} /> Enter your password
                </span>
              </div>
              {!preflight?.execution_enabled && (
                <p className="employee-repair-execution-wait"><Icon name="clock" /> Repairs are temporarily paused. This final check is saved.</p>
              )}
              <button
                className="button primary large"
                type="submit"
                disabled={
                  loading ||
                  reason.trim().length < 10 ||
                  !password ||
                  confirmation !== job.typed_confirmation ||
                  !preflight?.execution_enabled
                }
              >
                <Icon name="check" /> {loading ? 'Starting repair…' : 'Start the repair'}
              </button>
            </form>
          )}
        </section>
      )}

      <section className="panel employee-repair-history">
        <header className="panel-header">
          <div>
            <p className="eyebrow">SAVED AUTOMATICALLY</p>
            <h2>Past repairs</h2>
            <p>Open any repair to see its latest status or download proof.</p>
          </div>
          <button className="button secondary" onClick={() => void loadJobs()}>
            <Icon name="refresh" /> Refresh
          </button>
        </header>
        <div>
          {jobs.map((row) => (
            <button
              key={row.job_id}
              aria-label={`Open ${jobStatusCopy[row.status] || 'repair'} for machine ${row.device_id}`}
              disabled={loading}
              onClick={() => void openRepair(row.job_id)}
            >
              <span>
                <strong>{row.targets[0]?.display_name || `${plural(row.totals.employees, 'employee')}`}</strong>
                <small>{row.totals.employees > 1 ? `${plural(row.totals.employees, 'employee')} · ` : ''}{relativeTime(row.created_at)}</small>
              </span>
              <span className={`employee-repair-friendly-state is-${row.status === 'COMPLETED' ? 'complete' : ['NEEDS_ATTENTION', 'COMPLETED_WITH_ATTENTION'].includes(row.status) ? 'attention' : 'working'}`}>{jobStatusCopy[row.status] || humanize(row.status)}</span>
              <span>{plural(row.totals.events, 'punch', 'punches')}</span>
              <span>{row.totals.completed_events.toLocaleString()} finished</span>
              <Icon name="chevron" />
            </button>
          ))}
          {!jobs.length && (
            <div className="employee-repair-history-empty"><Icon name="clock" /><span><strong>No past repairs yet</strong><small>Your completed and active repairs will appear here.</small></span></div>
          )}
        </div>
      </section>

      {control && job && (
        <Dialog
          titleId="attendance-repair-control-title"
          title={controlCopy[control].title}
          description="Administrator confirmation required"
          onClose={() => setControl(null)}
        >
          <div className="dialog-body">
            <div className={`info-copy pattern-${control === 'cancel' ? 'blocked' : 'waiting'}`}>
              <Icon name="shield" />
              <div>
                <h3>{controlCopy[control].description}</h3>
                <p>Any attendance already fixed will stay fixed. Punch times and punch types never change.</p>
              </div>
            </div>
            <label>
              Reason for this action
              <textarea
                value={controlReason}
                maxLength={500}
                onChange={(event) => setControlReason(event.target.value)}
                placeholder="At least 10 characters. Do not include a CNIC."
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
                Go back
              </button>
              <button
                className={control === 'cancel' ? 'button destructive' : 'button primary'}
                disabled={loading || controlReason.trim().length < 10 || !controlPassword}
                onClick={() => void submitControl()}
              >
                {controlCopy[control].button}
              </button>
            </div>
            <details className="employee-repair-dialog-details"><summary>Technical repair ID</summary><code>{job.job_id}</code></details>
          </div>
        </Dialog>
      )}
    </div>
  )
}
