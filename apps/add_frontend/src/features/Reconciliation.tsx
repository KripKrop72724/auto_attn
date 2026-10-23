import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from 'react'
import './Reconciliation.css'
import { api, queryString } from '../api'
import {
  Dialog,
  Metric,
  PageHeader,
  StatusBadge,
  dateTime,
  idempotency,
  relativeTime,
  statusPattern,
  useToast,
} from '../App'
import { Icon } from '../Icon'
import { AnchoredLayer } from '../AnchoredLayer'
import { AttendanceRecovery } from './AttendanceRecovery'
import type {
  Device,
  HikvisionHistoryScope,
  ReconciliationDivergenceDetail,
  ReconciliationDivergenceReveal,
  ReconciliationBulkResponse,
  ReconciliationBulkResult,
  ReconciliationClearHistoryResult,
  ReconciliationJob,
  ReconciliationJobTotals,
  ReconciliationListResponse,
  ReconciliationPreflight,
  ReconciliationScheduler,
  ReconciliationSection,
  ReconciliationStatusGroup,
  SourceException,
  SourceExceptionAssurance,
  SourceExceptionList,
  SourceExceptionReveal,
  SourceExceptionTotals,
} from '../types'

type Toast = ReturnType<typeof useToast>
type ExceptionFilters = {
  job_id: string
  device_id: string
  disposition: string
  review_state: string
  error_code: string
  ordinal: string
}

const emptyScheduler: ReconciliationScheduler = {
  policy: 'BOUNDED_PARALLEL_PER_DEVICE',
  device_concurrency: 1,
  active_scan_jobs: 0,
  waiting_scan_jobs: 0,
  available_scan_slots: 1,
}
const emptyJobTotals: ReconciliationJobTotals = {
  all: 0,
  active: 0,
  queued_waiting: 0,
  paused: 0,
  attention: 0,
  completed: 0,
  cancelled: 0,
}
const emptyExceptionTotals: SourceExceptionTotals = {
  all: 0,
  open: 0,
  reviewed: 0,
  invalid_time: 0,
  malformed: 0,
  affected_terminals: 0,
}
const emptySourceAssurance: SourceExceptionAssurance = {
  total: 0,
  reviewed: 0,
  open: 0,
  invalid_time: 0,
  malformed: 0,
  state: 'NONE',
  cohort_digest: null,
}
const defaultExceptionFilters: ExceptionFilters = {
  job_id: '',
  device_id: '',
  disposition: '',
  review_state: '',
  error_code: '',
  ordinal: '',
}
const terminalJobStates = new Set([
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'INVALIDATED',
])

const humanize = (value?: string | null) =>
  (value || 'Unknown')
    .replaceAll('_', ' ')
    .toLowerCase()
    .replace(/^./, (letter) => letter.toUpperCase())

const sourceEvidencePending = (state: SourceExceptionAssurance['state']) =>
  state === 'REVIEW_REQUIRED' || state === 'IDENTITY_EVIDENCE_REQUIRED' || state === 'INTERPRETATION_REQUIRED' ||
  state === 'NEWEST_PUNCH_UNRECEIVED'

const newestPunchCopy = (count: number) =>
  `${count.toLocaleString()} terminal newest-record slot${count === 1 ? ' holds' : 's hold'} a punch ADD has not received; a review cannot exclude ${count === 1 ? 'it' : 'them'}. Assurance resumes automatically if LIVE capture delivers the same punch. Otherwise cancel this job and run another complete reconcile after the next punch.`

const newestCondition = (row: SourceException) =>
  row.source_condition === 'TERMINAL_NEWEST_RECORD_UNSETTLED' ||
  row.source_condition === 'TERMINAL_NEWEST_RECORD_UNCONFIRMED'

const reconciliationEtaLabel = (job: ReconciliationJob) => {
  if (job.eta.high_seconds != null)
    return `${Math.ceil(job.eta.high_seconds / 60)} min`
  if (job.eta.unavailable_reason === 'COMPLETED') return 'Complete'
  if (
    ['CANCELLED', 'FAILED', 'INVALIDATED'].includes(
      job.eta.unavailable_reason || '',
    )
  )
    return 'Stopped'
  if (job.eta.unavailable_reason?.includes('REVIEW')) return 'Review needed'
  if (job.eta.unavailable_reason?.includes('ORACLE')) return 'Oracle verifying'
  if (job.eta.unavailable_reason === 'WAITING_FOR_DEVICE') return 'Device wait'
  return 'Measuring'
}

// Realtime revisions arrive on every chunk and punch. Refetch at most once per
// interval (trailing edge), so a running scan does not keep reloading the view.
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
const JOB_PAGE = 40
const BULK_REQUEST_LIMIT = 100

// A refresh re-reads the loaded window, newest first by job id. Rows older than
// the refreshed window stay as loaded, so "Load older" rows never vanish and
// nothing is reordered; rows missing inside the window were archived or deleted.
const mergeJobWindow = (
  current: ReconciliationJob[],
  fresh: ReconciliationJob[],
  windowTruncated: boolean,
) => {
  if (!windowTruncated || !fresh.length) return { rows: fresh, keptTail: false }
  const freshIds = new Set(fresh.map((row) => row.job_id))
  const boundary = current.findIndex(
    (row) => row.job_id === fresh[fresh.length - 1].job_id,
  )
  const tail =
    boundary < 0
      ? []
      : current.slice(boundary + 1).filter((row) => !freshIds.has(row.job_id))
  return { rows: [...fresh, ...tail], keptTail: tail.length > 0 }
}

const bulkOutcomeCopy: Record<ReconciliationBulkResult['outcome'], string> = {
  CREATED: 'Started',
  ALREADY_ACTIVE: 'Already running',
  BLOCKED: 'Blocked',
}

function BulkReconcilePanel({
  devices,
  busy,
  result,
  onStart,
  onClose,
}: {
  devices: Device[]
  busy: boolean
  result: ReconciliationBulkResponse | null
  onStart: (connectorIds: string[]) => void
  onClose: () => void
}) {
  const [selection, setSelection] = useState<Set<string>>(() => new Set())
  const [query, setQuery] = useState('')
  const zones = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    const grouped = new Map<string, { name: string; devices: Device[] }>()
    devices
      .filter((device) =>
        `${device.display_name} ${device.zone_name} ${device.zone_id} ${device.connector_id} ${device.device_id} ${device.zkt?.serial || ''}`
          .toLowerCase()
          .includes(normalized),
      )
      .forEach((device) => {
        const key = device.zone_id || 'UNKNOWN'
        const group = grouped.get(key) || {
          name: device.zone_name || key,
          devices: [],
        }
        group.devices.push(device)
        grouped.set(key, group)
      })
    return [...grouped.entries()].sort(([left], [right]) =>
      left.localeCompare(right),
    )
  }, [devices, query])
  const shownIds = zones.flatMap(([, group]) =>
    group.devices.map((device) => device.connector_id),
  )
  const allShownSelected =
    shownIds.length > 0 && shownIds.every((id) => selection.has(id))
  const toggle = (ids: string[], checked: boolean) =>
    setSelection((current) => {
      const next = new Set(current)
      ids.forEach((id) => (checked ? next.add(id) : next.delete(id)))
      return next
    })
  const counts = result
    ? {
        CREATED: result.results.filter((row) => row.outcome === 'CREATED'),
        ALREADY_ACTIVE: result.results.filter(
          (row) => row.outcome === 'ALREADY_ACTIVE',
        ),
        BLOCKED: result.results.filter((row) => row.outcome === 'BLOCKED'),
      }
    : null
  return (
    <section
      className="panel reconciliation-bulk"
      aria-labelledby="reconciliation-bulk-title"
    >
      <div className="panel-header">
        <div>
          <h2 id="reconciliation-bulk-title">Reconcile several terminals</h2>
          <p>
            Tick terminals or whole zones and start them with one click. Each
            terminal gets its own full-history job; a blocked terminal is
            reported and never stops the others.
          </p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="Close bulk reconcile"
          onClick={onClose}
        >
          <Icon name="x" />
        </button>
      </div>
      <div className="reconciliation-bulk-toolbar">
        <label className="reconciliation-bulk-check">
          <input
            type="checkbox"
            checked={allShownSelected}
            disabled={!shownIds.length}
            onChange={(event) => toggle(shownIds, event.target.checked)}
          />
          <span>Select all ({shownIds.length.toLocaleString()})</span>
        </label>
        <label className="search-field">
          <span className="sr-only">Filter terminals for bulk reconcile</span>
          <Icon name="search" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter by name, zone, ID, or serial"
          />
        </label>
        <button
          className="button primary"
          type="button"
          disabled={!selection.size || busy}
          onClick={() => onStart([...selection])}
        >
          <Icon name="refresh" />{' '}
          {busy
            ? 'Starting…'
            : `Reconcile ${selection.size.toLocaleString()} selected`}
        </button>
      </div>
      <div className="reconciliation-bulk-zones">
        {zones.map(([zoneId, group]) => {
          const ids = group.devices.map((device) => device.connector_id)
          const zoneSelected = ids.every((id) => selection.has(id))
          return (
            <fieldset key={zoneId}>
              <legend>
                <label className="reconciliation-bulk-check">
                  <input
                    type="checkbox"
                    checked={zoneSelected}
                    onChange={(event) => toggle(ids, event.target.checked)}
                  />
                  <span>
                    <strong>{group.name}</strong>
                    <small>
                      {zoneId} · {ids.length} terminal{ids.length === 1 ? '' : 's'}
                    </small>
                  </span>
                </label>
              </legend>
              <div>
                {group.devices.map((device) => (
                  <label
                    key={device.connector_id}
                    className="reconciliation-bulk-check"
                  >
                    <input
                      type="checkbox"
                      checked={selection.has(device.connector_id)}
                      onChange={(event) =>
                        toggle([device.connector_id], event.target.checked)
                      }
                    />
                    <span>
                      <strong>{device.display_name}</strong>
                      <small>
                        {device.zkt?.serial || 'serial pending'} ·{' '}
                        {humanize(device.state)}
                      </small>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          )
        })}
        {!zones.length && (
          <div className="empty-state compact">
            <Icon name="search" />
            <p>No terminals match this filter.</p>
          </div>
        )}
      </div>
      {counts && (
        <div className="reconciliation-bulk-result" aria-label="Bulk reconcile result">
          <p>
            <strong>{counts.CREATED.length} started</strong> ·{' '}
            {counts.ALREADY_ACTIVE.length} already running ·{' '}
            {counts.BLOCKED.length} blocked
          </p>
          {[...counts.ALREADY_ACTIVE, ...counts.BLOCKED].length > 0 && (
            <ul>
              {[...counts.BLOCKED, ...counts.ALREADY_ACTIVE].map((row) => (
                <li key={row.connector_id} className={`pattern-${row.outcome === 'BLOCKED' ? 'blocked' : 'waiting'}`}>
                  <strong>{row.display_name || row.connector_id}</strong>
                  <span>
                    {bulkOutcomeCopy[row.outcome]}
                    {row.message ? ` — ${row.message}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

function WorkspaceTabs({
  section,
  activeJobs,
  sourceExceptions,
  recoveryCount,
  onChange,
}: {
  section: ReconciliationSection
  activeJobs: number
  sourceExceptions: number
  recoveryCount: number
  onChange: (section: ReconciliationSection) => void
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const tabs: Array<{
    id: ReconciliationSection
    label: string
    count: number
  }> = [
    { id: 'jobs', label: 'Jobs', count: activeJobs },
    { id: 'exceptions', label: 'Source exceptions', count: sourceExceptions },
    { id: 'recovery', label: 'Recovery', count: recoveryCount },
  ]
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft')
      next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault()
    onChange(tabs[next].id)
    refs.current[next]?.focus()
  }
  return (
    <div
      className="section-tabs reconciliation-workspace-tabs"
      role="tablist"
      aria-label="Reconciliation workspace"
    >
      {tabs.map((tab, index) => (
        <button
          key={tab.id}
          ref={(node) => {
            refs.current[index] = node
          }}
          type="button"
          role="tab"
          id={`reconciliation-${tab.id}-tab`}
          aria-controls={`reconciliation-${tab.id}-panel`}
          aria-selected={section === tab.id}
          tabIndex={section === tab.id ? 0 : -1}
          className={section === tab.id ? 'active' : ''}
          onKeyDown={(event) => move(event, index)}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          <strong>{tab.count.toLocaleString()}</strong>
        </button>
      ))}
    </div>
  )
}

function TerminalPicker({
  devices,
  selectedId,
  open,
  onOpenChange,
  onSelect,
}: {
  devices: Device[]
  selectedId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (id: string) => void
}) {
  const [query, setQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  const cardRef = useRef<HTMLElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const optionsRef = useRef<HTMLDivElement>(null)
  const selected =
    devices.find((device) => device.connector_id === selectedId) || null
  const shown = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    return devices.filter((device) =>
      `${device.display_name} ${device.zone_name} ${device.zone_id} ${device.connector_id} ${device.device_id} ${device.hardware_id} ${device.zkt?.serial || ''}`
        .toLowerCase()
        .includes(normalized),
    )
  }, [devices, query])
  useEffect(() => {
    if (open) window.setTimeout(() => searchRef.current?.focus(), 0)
    else setQuery('')
  }, [open])
  const close = useCallback((restoreFocus = true) => {
    onOpenChange(false)
    if (restoreFocus) window.setTimeout(() => triggerRef.current?.focus(), 0)
  }, [onOpenChange])
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const options = Array.from(
      optionsRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="option"]',
      ) || [],
    )
    let next = index
    if (event.key === 'ArrowDown')
      next = Math.min(options.length - 1, index + 1)
    else if (event.key === 'ArrowUp') next = Math.max(0, index - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = options.length - 1
    else if (event.key === 'Escape') {
      event.preventDefault()
      close()
      return
    } else return
    event.preventDefault()
    options[next]?.focus()
  }
  return (
    <section
      ref={cardRef}
      className={`reconciliation-terminal-picker panel ${open ? 'is-open' : ''}`}
      aria-label="Terminal selection"
    >
      <div className="reconciliation-terminal-current">
        <span className="reconciliation-terminal-symbol">
          <Icon name="server" />
        </span>
        <div>
          <strong>
            {selected?.display_name || 'Choose a terminal to reconcile'}
          </strong>
          <span>
            {selected
              ? `${selected.zone_name} · ${selected.zkt?.serial || 'serial pending'} · FW ${selected.firmware_version || 'unknown'}`
              : 'Search the national fleet by name, zone, connector, device, MAC, or serial'}
          </span>
        </div>
        {selected && (
          <StatusBadge state={selected.state} live={selected.connected} />
        )}
        <button
          ref={triggerRef}
          className="button secondary"
          type="button"
          aria-expanded={open}
          onClick={() => onOpenChange(!open)}
        >
          <Icon name="search" />{' '}
          {selected ? 'Change terminal' : 'Select terminal'}
        </button>
      </div>
      {open && (
        <AnchoredLayer anchorRef={cardRef} className="reconciliation-terminal-layer" matchAnchor mobileSheet preferredWidth={560} onDismiss={(reason) => close(reason === 'escape')}>
          <div className="reconciliation-terminal-popover">
          <label className="search-field">
            <span className="sr-only">Search authorized terminals</span>
            <Icon name="search" />
            <input
              ref={searchRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'ArrowDown') {
                  event.preventDefault()
                  optionsRef.current
                    ?.querySelector<HTMLButtonElement>('[role="option"]')
                    ?.focus()
                } else if (event.key === 'Escape') close()
              }}
              placeholder="Search name, zone, ID, MAC, or serial"
            />
          </label>
          <div
            ref={optionsRef}
            className="reconciliation-terminal-results"
            role="listbox"
            aria-label="Authorized terminals"
          >
            {shown.map((device, index) => (
              <button
                key={device.connector_id}
                type="button"
                role="option"
                aria-selected={device.connector_id === selectedId}
                onKeyDown={(event) => move(event, index)}
                onClick={() => {
                  onSelect(device.connector_id)
                  close(false)
                }}
              >
                <span>
                  <strong>{device.display_name}</strong>
                  <small>
                    {device.zone_name} ·{' '}
                    {device.zkt?.serial || 'serial pending'}
                  </small>
                </span>
                <span>
                  <StatusBadge state={device.state} />
                  <small>
                    {device.zkt?.attendance_count?.toLocaleString() ?? '—'}{' '}
                    records · {relativeTime(device.last_seen_at)}
                  </small>
                </span>
                <Icon name={device.connector_id === selectedId ? 'check' : 'chevron'} />
              </button>
            ))}
            {!shown.length && (
              <div className="empty-state compact">
                <Icon name="search" />
                <p>No authorized terminals match this search.</p>
              </div>
            )}
          </div>
          </div>
        </AnchoredLayer>
      )}
    </section>
  )
}

function JobDetailDrawer({
  seed,
  onClose,
  toast,
  onDivergence,
  onReviewExceptions,
  returnFocusRef,
}: {
  seed: ReconciliationJob
  onClose: () => void
  toast: Toast
  onDivergence: (id: string) => void
  onReviewExceptions: (job: ReconciliationJob) => void
  returnFocusRef: RefObject<HTMLButtonElement | null>
}) {
  const [job, setJob] = useState<ReconciliationJob | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [visibleEventCount, setVisibleEventCount] = useState(25)
  const [expandedEvent, setExpandedEvent] = useState<string | null>(null)
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setJob(await api<ReconciliationJob>(`/api/v1/reconciliations/${seed.job_id}`))
      setVisibleEventCount(25)
      setExpandedEvent(null)
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Reconciliation evidence could not be loaded.',
      )
    } finally {
      setLoading(false)
    }
  }, [seed.job_id])
  useEffect(() => {
    void load()
  }, [load])
  const orderedEvents = useMemo(
    () => (job?.events || []).slice().reverse(),
    [job?.events],
  )
  const shownEvents = orderedEvents.slice(0, visibleEventCount)
  return (
    <Dialog
      titleId="reconciliation-job-detail-title"
      title="Reconciliation evidence"
      description={`${seed.connector?.display_name || seed.job_id} · ${seed.job_id}`}
      onClose={onClose}
      className="device-drawer reconciliation-detail-drawer"
      returnFocusRef={returnFocusRef}
    >
      <div className="drawer-status">
        <StatusBadge
          state={job?.operator_state || seed.operator_state || seed.status}
        />
        <span>
          Terminal truth remains immutable; Oracle proof is append-only.
        </span>
      </div>
      <div className="drawer-content reconciliation-detail-content">
        {loading && (
          <div
            className="reconciliation-skeleton"
            aria-label="Loading reconciliation evidence"
          >
            <i />
            <i />
            <i />
          </div>
        )}
        {error && (
          <div className="inline-retry pattern-blocked" role="alert">
            <Icon name="alert" />
            <div>
              <strong>Evidence is temporarily unavailable</strong>
              <p>{error}</p>
            </div>
            <button className="button secondary" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}
        {job && (
          <>
            <article
              className={`reconciliation-assurance-card pattern-${statusPattern(job.operator_state || job.status)}`}
            >
              <Icon name="shield" />
              <div>
                <p className="eyebrow">CURRENT ASSURANCE</p>
                <h3>{humanize(job.operator_state || job.status)}</h3>
                <p>
                  {job.operator_message ||
                    'Durable source evidence is recorded for this job.'}
                </p>
              </div>
            </article>
            <dl className="reconciliation-detail-facts">
              <div>
                <dt>Terminal</dt>
                <dd>{job.terminal.serial || 'Awaiting identity'}</dd>
              </div>
              <div>
                <dt>Source epoch</dt>
                <dd>{job.mode === 'HIKVISION_SERIAL_HISTORY' ? job.recovery?.source_epoch_id || 'Pending' : job.recovery?.source_epoch ?? 1}</dd>
              </div>
              <div>
                <dt>{job.mode === 'HIKVISION_SERIAL_HISTORY' ? 'Committed source serial' : 'Committed next ordinal'}</dt>
                <dd>{(job.checkpoint?.source_serial ?? job.checkpoint?.next_ordinal)?.toLocaleString() ?? '—'}</dd>
              </div>
              <div>
                <dt>Chain digest</dt>
                <dd>
                  <code>{job.checkpoint?.chain_digest || 'Not available'}</code>
                </dd>
              </div>
              <div>
                <dt>Assignment</dt>
                <dd>
                  <code>
                    {job.assignment.assignment_id || 'No active assignment'}
                  </code>
                </dd>
              </div>
              <div>
                <dt>Heartbeat</dt>
                <dd>
                  {job.assignment.heartbeat_at
                    ? dateTime(job.assignment.heartbeat_at)
                    : 'Not active'}
                </dd>
              </div>
              <div>
                <dt>Capture certified</dt>
                <dd>
                  {job.capture_certified_at
                    ? dateTime(job.capture_certified_at)
                    : 'Pending'}
                </dd>
              </div>
              <div>
                <dt>Oracle certified</dt>
                <dd>
                  {job.oracle_certified_at
                    ? dateTime(job.oracle_certified_at)
                    : 'Pending'}
                </dd>
              </div>
            </dl>
            {(job.source_exception_assurance?.total ?? 0) > 0 && (
              <article
                className={`info-copy pattern-${sourceEvidencePending(job.source_exception_assurance.state) ? 'waiting' : job.source_exception_assurance.state === 'SCOPE_MISMATCH' ? 'blocked' : 'notice'}`}
              >
                <Icon name="shield" />
                <div>
                  <h3>
                    {job.source_exception_assurance.state === 'INTERPRETATION_REQUIRED'
                      ? 'Source interpretation pending'
                      : job.source_exception_assurance.state === 'IDENTITY_EVIDENCE_REQUIRED'
                      ? 'Historical identity evidence required'
                      : job.source_exception_assurance.state === 'NEWEST_PUNCH_UNRECEIVED'
                      ? 'Terminal punch not yet received'
                      : job.source_exception_assurance.state === 'REVIEW_REQUIRED'
                      ? `${job.source_exception_assurance.open.toLocaleString()} source review${job.source_exception_assurance.open === 1 ? '' : 's'} remaining`
                      : job.source_exception_assurance.state === 'SCOPE_MISMATCH'
                        ? 'Source-exception evidence mismatch'
                        : 'Reviewed exclusions are preserved'}
                  </h3>
                  <p>
                    {job.source_exception_assurance.state === 'INTERPRETATION_REQUIRED' ? (
                      'Raw source bytes are preserved. Qualified interpretation is still required before attendance and Oracle delivery can be determined.'
                    ) : <>
                    {job.source_exception_assurance.reviewed.toLocaleString()} of{' '}
                    {job.source_exception_assurance.total.toLocaleString()} certified
                    source exceptions reviewed. Original evidence remains immutable.
                    {job.source_exception_assurance.state === 'IDENTITY_EVIDENCE_REQUIRED' &&
                      ' Review notes cannot resolve missing identity or certify Oracle delivery.'}
                    {job.source_exception_assurance.state === 'NEWEST_PUNCH_UNRECEIVED' &&
                      ` ${newestPunchCopy(job.source_exception_assurance.newest_punch_unreceived ?? 1)}`}
                    </>}
                  </p>
                  {sourceEvidencePending(job.source_exception_assurance.state) && job.source_exception_assurance.state !== 'INTERPRETATION_REQUIRED' && (
                    <button
                      className="button secondary"
                      onClick={() => onReviewExceptions(job)}
                    >
                      Inspect certified exceptions
                    </button>
                  )}
                </div>
              </article>
            )}
            {job.recovery?.divergence && (
              <article className="info-copy pattern-waiting">
                <Icon name="shield" />
                <div>
                  <h3>Preserved terminal-history change</h3>
                  <p>
                    Ordinal {job.recovery.divergence.ordinal.toLocaleString()}{' '}
                    has {job.recovery.divergence.observation_count} independent
                    observation(s).
                  </p>
                  <button
                    className="button secondary"
                    onClick={() =>
                      onDivergence(
                        job.recovery?.divergence?.divergence_id || '',
                      )
                    }
                  >
                    Inspect source-change evidence
                  </button>
                </div>
              </article>
            )}
            <section className="reconciliation-certificate-grid">
              <details>
                <summary>Capture certificate</summary>
                <pre>
                  {JSON.stringify(
                    job.capture_certificate || { state: 'Pending' },
                    null,
                    2,
                  )}
                </pre>
              </details>
              <details>
                <summary>Oracle assurance certificate</summary>
                <pre>
                  {JSON.stringify(
                    job.oracle_certificate || { state: 'Pending' },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </section>
            <section className="reconciliation-event-ledger">
              <div className="panel-header">
                <div>
                  <h3>Durable event timeline</h3>
                  <p>
                    Newest state changes remain correlated to the same immutable
                    job.
                  </p>
                </div>
                <div className="reconciliation-ledger-count">
                  <StatusBadge state={`${orderedEvents.length} EVENTS`} />
                  {orderedEvents.length > 0 && <small>Showing {Math.min(visibleEventCount, orderedEvents.length)} newest</small>}
                </div>
              </div>
              {shownEvents.map((event, index) => {
                const eventKey = `${event.created_at}-${index}`
                const detailKeys = Object.keys(event.details || {})
                const expanded = expandedEvent === eventKey
                return (
                  <article key={eventKey}>
                    <button className="reconciliation-event-summary" type="button" aria-expanded={expanded} onClick={() => setExpandedEvent(expanded ? null : eventKey)}>
                      <span><StatusBadge state={event.state} /><small>{dateTime(event.created_at)}</small></span>
                      <span><strong>{humanize(event.state)}</strong><small>{detailKeys.length ? detailKeys.slice(0, 3).map(humanize).join(' · ') : 'No additional fields'}</small></span>
                      <Icon name="chevron" />
                    </button>
                    {expanded && <pre>{JSON.stringify(event.details, null, 2)}</pre>}
                  </article>
                )
              })}
              {!orderedEvents.length && (
                <p className="muted-copy">
                  No detailed event records are available yet.
                </p>
              )}
              {visibleEventCount < orderedEvents.length && <div className="reconciliation-ledger-more"><button className="button secondary" type="button" onClick={() => setVisibleEventCount((count) => Math.min(count + 25, orderedEvents.length))}>Load 25 older events</button><small>{(orderedEvents.length - visibleEventCount).toLocaleString()} older event{orderedEvents.length - visibleEventCount === 1 ? '' : 's'} remain</small></div>}
            </section>
            <a
              className="button secondary"
              href={`/api/v1/reconciliations/${job.job_id}/evidence`}
              target="_blank"
              rel="noreferrer"
            >
              <Icon name="shield" /> Open raw JSON evidence
            </a>
          </>
        )}
      </div>
    </Dialog>
  )
}

function SourceExceptionDrawer({
  seed,
  onClose,
  onChanged,
  toast,
}: {
  seed: SourceException
  onClose: () => void
  onChanged: () => Promise<void>
  toast: Toast
}) {
  const [row, setRow] = useState<SourceException | null>(null)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState<SourceExceptionReveal | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      setRow(await api<SourceException>(`/api/v1/source-exceptions/${seed.id}`))
    } catch (reason) {
      setLoadError(
        reason instanceof Error
          ? reason.message
          : 'Source evidence could not be loaded.',
      )
    } finally {
      setLoading(false)
    }
  }, [seed.id])
  useEffect(() => {
    void load()
  }, [load])
  useEffect(() => {
    if (!revealed) return
    const hide = () => setRevealed(null)
    const timeout = window.setTimeout(hide, 60_000)
    const hideOnBackground = () => {
      if (document.hidden) hide()
    }
    document.addEventListener('visibilitychange', hideOnBackground)
    return () => {
      window.clearTimeout(timeout)
      document.removeEventListener('visibilitychange', hideOnBackground)
    }
  }, [revealed])
  useEffect(() => () => setRevealed(null), [])
  const act = async (action: 'review' | 'reveal') => {
    if (!row) return
    setBusy(true)
    try {
      const body = JSON.stringify({
        reason: reason.trim(),
        password,
        idempotency_key: idempotency(`source-exception-${action}`),
      })
      if (action === 'review') {
        setRow(
          await api<SourceException>(
            `/api/v1/source-exceptions/${row.id}/review`,
            { method: 'POST', body },
          ),
        )
        toast.notice(
          newestCondition(row)
            ? 'Review recorded. It does not settle this terminal newest-record slot or read its punch.'
            : row.disposition === 'IDENTITY_UNRESOLVED' && row.source_condition !== 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER'
            ? 'Review recorded. The identity hold remains until verified correction evidence is available.'
            : 'Source review recorded. When the certified cohort is complete, assurance resumes automatically from its existing checkpoint.',
        )
        await onChanged()
      } else {
        setRevealed(
          await api<SourceExceptionReveal>(
            `/api/v1/source-exceptions/${row.id}/reveal`,
            { method: 'POST', body },
          ),
        )
        toast.notice('Protected source evidence revealed with an audit entry.')
      }
      setPassword('')
      setReason('')
    } catch (error) {
      setPassword('')
      toast.error(
        error instanceof Error
          ? error.message
          : 'Source exception action failed.',
      )
    } finally {
      setBusy(false)
    }
  }
  const canAct = reason.trim().length >= 10 && Boolean(password) && !busy
  return (
    <Dialog
      titleId="source-exception-title"
      title="Terminal source exception"
      description={`${seed.zone_id || 'Unknown zone'} · ordinal ${seed.ordinal.toLocaleString()}`}
      onClose={onClose}
      className="device-drawer source-exception-drawer"
    >
      <div className="drawer-status">
        <StatusBadge state={row?.disposition || seed.disposition} />
        <span>
          {(row || seed).cursor_advanced
            ? `Source cursor safely advanced to ${(row || seed).source_committed_cursor.toLocaleString()}`
            : 'Source cursor has not advanced beyond this row'}
        </span>
      </div>
      <div className="drawer-content source-exception-detail">
        {loading && (
          <div className="reconciliation-skeleton">
            <i />
            <i />
            <i />
          </div>
        )}
        {loadError && (
          <div className="inline-retry pattern-blocked" role="alert">
            <Icon name="alert" />
            <div>
              <strong>Evidence is temporarily unavailable</strong>
              <p>{loadError}</p>
            </div>
            <button className="button secondary" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}
        {row && (
          <>
            <article className="info-copy pattern-blocked">
              <Icon name="shield" />
              <div>
                <h3>{row.source_condition === 'TERMINAL_NEWEST_RECORD_CORRECTED'
                  ? 'Corrected from an unwritten newest record'
                  : row.source_condition === 'TERMINAL_NEWEST_RECORD_UNCONFIRMED'
                  ? 'Held: newest-record punch not confirmed'
                  : row.source_condition === 'TERMINAL_NEWEST_RECORD_UNSETTLED'
                  ? 'Terminal newest record not yet written'
                  : row.source_condition === 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER'
                  ? 'Excluded: newest-record bytes out of time order'
                  : row.disposition === 'IDENTITY_UNRESOLVED'
                  ? 'Held for verified historical identity'
                  : 'Excluded from attendance and Oracle'}</h3>
                <p>
                  ADD preserved this terminal ordinal as immutable evidence.
                  {row.source_condition === 'TERMINAL_NEWEST_RECORD_CORRECTED'
                    ? ` ${row.operator_reason || 'A source time correction was made from bytes that were not the punch in this slot. HR must verify the corrected punch.'}`
                    : row.source_condition === 'TERMINAL_NEWEST_RECORD_UNCONFIRMED'
                    ? ` ${row.operator_reason || 'These bytes decode as a punch no capture has delivered. ADD holds them until LIVE capture or a later reconcile confirms the punch.'}`
                    : row.source_condition === 'TERMINAL_NEWEST_RECORD_UNSETTLED'
                    ? ` ${row.operator_reason || 'These bytes were read before the terminal wrote this slot. They are not a punch.'}`
                    : row.source_condition === 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER'
                    ? ` ${row.operator_reason || 'The terminal holds these bytes, but their time does not lie between the punches around them. They may be stale bytes with another person\'s identity, so ADD did not ingest them. Review the row.'}`
                    : row.disposition === 'IDENTITY_UNRESOLVED'
                    ? ' Identity operations must establish an independently supported user reference. A historical attendance UID cannot identify the current enrollment owner. A review note does not resolve this hold or certify Oracle delivery.'
                    : ' Review never creates, edits, or deletes attendance. Completing every review in a certified job automatically resumes assurance for its valid records.'}
                </p>
              </div>
            </article>
            <dl className="reconciliation-detail-facts">
              <div>
                <dt>Device</dt>
                <dd>{row.display_name || row.connector_id || 'Unknown'}</dd>
              </div>
              <div>
                <dt>Terminal</dt>
                <dd>{row.terminal_serial}</dd>
              </div>
              <div>
                <dt>Generation / ordinal</dt>
                <dd>
                  {row.terminal_generation} / {row.ordinal.toLocaleString()}
                </dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd>{humanize(row.source_kind)}</dd>
              </div>
              <div>
                <dt>Error</dt>
                <dd>{row.error_code || humanize(row.disposition)}</dd>
              </div>
              <div>
                <dt>Observed identity</dt>
                <dd>
                  Attendance UID {row.observed_uid || '—'} · User{' '}
                  {row.observed_user_id || '—'}
                </dd>
              </div>
              <div className="wide">
                <dt>SHA-256 evidence digest</dt>
                <dd>
                  <code>{row.raw_record_digest}</code>
                </dd>
              </div>
              <div className="wide">
                <dt>Terminal record key</dt>
                <dd>
                  <code>{row.terminal_record_key}</code>
                </dd>
              </div>
            </dl>
            <section className="exception-review-section">
              <div className="panel-header">
                <div>
                  <h3>Review history</h3>
                  <p>
                    {newestCondition(row)
                      ? 'Review records that the row was seen. It does not settle this slot or read its punch; only a later complete reconcile does.'
                      : row.disposition === 'IDENTITY_UNRESOLVED'
                      ? 'Review records an investigation note; the identity hold and original source evidence remain.'
                      : 'Review accepts this fail-closed exclusion; the preserved source record itself never changes.'}
                  </p>
                </div>
                <StatusBadge state={row.review_state} />
              </div>
              {(row.reviews || []).map((review) => (
                <article key={review.review_id} className="exception-review">
                  <strong>{review.actor}</strong>
                  <span>{dateTime(review.created_at)}</span>
                  <p>{review.reason}</p>
                </article>
              ))}
              {!row.reviews?.length && (
                <p className="muted-copy">
                  No operator review has been recorded.
                </p>
              )}
            </section>
            {!!row.slot_receipts?.length && (
              <section className="exception-review-section" aria-label="Reads of this slot">
                <div className="panel-header">
                  <div>
                    <h3>Reads of this slot</h3>
                    <p>
                      Verified reads of this ordinal kept as evidence. None of them was sent to
                      Oracle from here.
                    </p>
                  </div>
                </div>
                {row.slot_receipts.map((receipt) => (
                  <article key={receipt.manifest_id} className="exception-review">
                    <strong>{humanize(receipt.source_kind)}</strong>
                    <span>{dateTime(receipt.observed_at)}</span>
                    <p>
                      {receipt.punch_received === false
                        ? 'Decodes as a punch ADD has not received.'
                        : receipt.punch_received
                        ? 'Decodes as a punch ADD has received from another capture.'
                        : receipt.decodes_as_punch
                        ? 'Its stored interpretation cannot be read; treated as a punch ADD has not received.'
                        : `No punch (${humanize(receipt.disposition)}).`}{' '}
                      <code>{receipt.raw_record_digest.slice(0, 16)}</code>
                    </p>
                  </article>
                ))}
              </section>
            )}
            {revealed && (
              <section className="revealed-evidence" aria-live="polite">
                <div className="panel-header">
                  <div>
                    <h3>Protected raw evidence</h3>
                    <p>
                      This audited no-store response hides automatically after
                      60 seconds.
                    </p>
                  </div>
                  <button
                    className="button secondary"
                    onClick={() => setRevealed(null)}
                  >
                    Hide now
                  </button>
                </div>
                <label>
                  Hex<code>{revealed.raw_record_hex}</code>
                </label>
                <label>
                  Base64<code>{revealed.raw_record_b64}</code>
                </label>
              </section>
            )}
            <section className="exception-actions">
              <label>
                Audited reason
                <textarea
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  maxLength={500}
                  placeholder="At least 10 characters"
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
              <div className="dialog-actions">
                <button
                  className="button secondary"
                  disabled={!canAct}
                  onClick={() => void act('reveal')}
                >
                  <Icon name="search" /> Reveal protected bytes
                </button>
                <button
                  className="button primary"
                  disabled={!canAct || row.review_state === 'REVIEWED'}
                  onClick={() => void act('review')}
                >
                  <Icon name="check" /> Mark reviewed
                </button>
              </div>
            </section>
          </>
        )}
      </div>
    </Dialog>
  )
}

function ReconciliationDivergenceDrawer({
  divergenceId,
  onClose,
  toast,
}: {
  divergenceId: string
  onClose: () => void
  toast: Toast
}) {
  const [row, setRow] = useState<ReconciliationDivergenceDetail | null>(null)
  const [loadError, setLoadError] = useState('')
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] =
    useState<ReconciliationDivergenceReveal | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    setLoadError('')
    try {
      setRow(
        await api<ReconciliationDivergenceDetail>(
          `/api/v1/reconciliation-divergences/${divergenceId}`,
        ),
      )
    } catch (reason) {
      setLoadError(
        reason instanceof Error
          ? reason.message
          : 'Source-change evidence could not be loaded.',
      )
    }
  }, [divergenceId])
  useEffect(() => {
    void load()
  }, [load])
  useEffect(() => {
    if (!revealed) return
    const hide = () => setRevealed(null)
    const timeout = window.setTimeout(hide, 60_000)
    const hideOnBackground = () => {
      if (document.hidden) hide()
    }
    document.addEventListener('visibilitychange', hideOnBackground)
    return () => {
      window.clearTimeout(timeout)
      document.removeEventListener('visibilitychange', hideOnBackground)
    }
  }, [revealed])
  const reveal = async () => {
    setBusy(true)
    try {
      setRevealed(
        await api<ReconciliationDivergenceReveal>(
          `/api/v1/reconciliation-divergences/${divergenceId}/reveal`,
          {
            method: 'POST',
            body: JSON.stringify({
              reason: reason.trim(),
              password,
              idempotency_key: idempotency('source-divergence-reveal'),
            }),
          },
        ),
      )
      setPassword('')
      setReason('')
      toast.notice(
        'Protected source-change evidence revealed with an audit entry.',
      )
    } catch (error) {
      setPassword('')
      toast.error(
        error instanceof Error
          ? error.message
          : 'Could not reveal source-change evidence.',
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId="source-divergence-title"
      title="Terminal-history change"
      description={
        row
          ? `Ordinal ${row.ordinal.toLocaleString()} · ${humanize(row.state)}`
          : 'Loading immutable evidence'
      }
      onClose={onClose}
      className="device-drawer source-exception-drawer"
    >
      <div className="drawer-status">
        <StatusBadge state={row?.state || 'LOADING'} />
        <span>
          Old evidence remains immutable; recovery never overwrites Oracle.
        </span>
      </div>
      <div className="drawer-content source-exception-detail">
        {loadError && (
          <div className="inline-retry pattern-blocked" role="alert">
            <Icon name="alert" />
            <div>
              <strong>Evidence is temporarily unavailable</strong>
              <p>{loadError}</p>
            </div>
            <button className="button secondary" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}
        {!row && !loadError && (
          <div className="reconciliation-skeleton">
            <i />
            <i />
            <i />
          </div>
        )}
        {row && (
          <>
            <article className="info-copy pattern-waiting">
              <Icon name="shield" />
              <div>
                <h3>Independent fresh-buffer verification</h3>
                <p>
                  ADD compares raw bytes, preserves stable changes as a new
                  source epoch, and never replaces prior evidence.
                </p>
              </div>
            </article>
            <dl className="reconciliation-detail-facts">
              <div>
                <dt>Job</dt>
                <dd>{row.job_id || 'Unknown'}</dd>
              </div>
              <div>
                <dt>Ordinal</dt>
                <dd>{row.ordinal.toLocaleString()}</dd>
              </div>
              <div>
                <dt>Original disposition</dt>
                <dd>{row.old_disposition || 'Unknown'}</dd>
              </div>
              <div>
                <dt>Observed disposition</dt>
                <dd>{row.new_disposition || 'Unknown'}</dd>
              </div>
              <div className="wide">
                <dt>Original SHA-256</dt>
                <dd>
                  <code>{row.old_raw_digest}</code>
                </dd>
              </div>
              <div className="wide">
                <dt>Observed SHA-256</dt>
                <dd>
                  <code>{row.new_raw_digest}</code>
                </dd>
              </div>
            </dl>
            <section className="exception-review-section">
              <div className="panel-header">
                <div>
                  <h3>Observation timeline</h3>
                  <p>
                    Each probe used an independently prepared terminal buffer.
                  </p>
                </div>
                <StatusBadge
                  state={`${row.observations.length} OBSERVATIONS`}
                />
              </div>
              {row.observations.map((item, index) => (
                <article
                  className="exception-review"
                  key={`${item.observed_at}-${index}`}
                >
                  <strong>{humanize(item.kind)}</strong>
                  <span>{dateTime(item.observed_at)}</span>
                  <p>{item.raw_record_digest}</p>
                </article>
              ))}
            </section>
            {revealed && (
              <section className="revealed-evidence" aria-live="polite">
                <div className="panel-header">
                  <div>
                    <h3>Protected raw evidence</h3>
                    <p>This no-store response hides automatically.</p>
                  </div>
                  <button
                    className="button secondary"
                    onClick={() => setRevealed(null)}
                  >
                    Hide now
                  </button>
                </div>
                <label>
                  Hex<code>{revealed.raw_record_hex}</code>
                </label>
                <label>
                  Base64<code>{revealed.raw_record_b64}</code>
                </label>
              </section>
            )}
            {row.evidence_available && (
              <section className="exception-actions">
                <label>
                  Audited reason
                  <textarea
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    maxLength={500}
                    placeholder="At least 10 characters"
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
                <div className="dialog-actions">
                  <button
                    className="button primary"
                    disabled={reason.trim().length < 10 || !password || busy}
                    onClick={() => void reveal()}
                  >
                    <Icon name="search" />{' '}
                    {busy ? 'Verifying…' : 'Reveal protected bytes'}
                  </button>
                </div>
              </section>
            )}
          </>
        )}
      </div>
    </Dialog>
  )
}

export function ReconciliationView({
  devices,
  revision,
  toast,
}: {
  devices: Device[]
  revision: number
  toast: Toast
}) {
  const initial = useMemo(() => new URLSearchParams(window.location.search), [])
  const [section, setSection] = useState<ReconciliationSection>(
    initial.get('tab') === 'source-exceptions'
      ? 'exceptions'
      : initial.get('tab') === 'recovery'
        ? 'recovery'
      : 'jobs',
  )
  const [rows, setRows] = useState<ReconciliationJob[]>([])
  const [scheduler, setScheduler] =
    useState<ReconciliationScheduler>(emptyScheduler)
  const [jobTotals, setJobTotals] =
    useState<ReconciliationJobTotals>(emptyJobTotals)
  const [jobCursor, setJobCursor] = useState<number | null>(null)
  const [jobFilteredTotal, setJobFilteredTotal] = useState(0)
  const [enabled, setEnabled] = useState(false)
  const [jobLoading, setJobLoading] = useState(true)
  const [jobLoadingMore, setJobLoadingMore] = useState(false)
  const [jobError, setJobError] = useState('')
  const [jobQuery, setJobQuery] = useState('')
  const [jobStatus, setJobStatus] = useState<ReconciliationStatusGroup>('')
  const [jobZone, setJobZone] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [pickerOpen, setPickerOpen] = useState(false)
  const [deviceDetail, setDeviceDetail] = useState<Device | null>(null)
  const [preflight, setPreflight] = useState<ReconciliationPreflight | null>(
    null,
  )
  const [preflightLoading, setPreflightLoading] = useState(false)
  const [preflightError, setPreflightError] = useState('')
  const [historyScope, setHistoryScope] = useState<HikvisionHistoryScope>('ALL_RECORDS')
  const [jobDrawer, setJobDrawer] = useState<ReconciliationJob | null>(null)
  const jobDrawerReturnFocusRef = useRef<HTMLButtonElement>(null)
  const [divergenceDrawerId, setDivergenceDrawerId] = useState<string | null>(
    null,
  )
  // One action at a time: 'start', 'bulk', 'clear', or `${job_id}:${action}`.
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkResult, setBulkResult] =
    useState<ReconciliationBulkResponse | null>(null)
  const [liveStatus, setLiveStatus] = useState('')
  const [exceptionRows, setExceptionRows] = useState<SourceException[]>([])
  const [exceptionTotals, setExceptionTotals] =
    useState<SourceExceptionTotals>(emptyExceptionTotals)
  const [exceptionScope, setExceptionScope] = useState<
    SourceExceptionList['scope'] | null
  >(null)
  const [exceptionFilteredTotal, setExceptionFilteredTotal] = useState(0)
  const [exceptionCursor, setExceptionCursor] = useState<number | null>(null)
  const [exceptionLoading, setExceptionLoading] = useState(true)
  const [exceptionLoadingMore, setExceptionLoadingMore] = useState(false)
  const [exceptionError, setExceptionError] = useState('')
  const [exceptionDrawer, setExceptionDrawer] =
    useState<SourceException | null>(null)
  const [recoveryCount, setRecoveryCount] = useState(0)
  const [exceptionDraft, setExceptionDraft] = useState<ExceptionFilters>({
    ...defaultExceptionFilters,
    job_id: initial.get('job_id') || '',
    device_id: initial.get('device_id') || '',
  })
  const [exceptionFilters, setExceptionFilters] = useState<ExceptionFilters>({
    ...defaultExceptionFilters,
    job_id: initial.get('job_id') || '',
    device_id: initial.get('device_id') || '',
  })
  const jobAbortRef = useRef<AbortController | null>(null)
  const exceptionAbortRef = useRef<AbortController | null>(null)
  const preflightAbortRef = useRef<AbortController | null>(null)
  const rowsRef = useRef<ReconciliationJob[]>([])
  const jobCursorRef = useRef<number | null>(null)
  useEffect(() => {
    rowsRef.current = rows
    jobCursorRef.current = jobCursor
  }, [rows, jobCursor])
  const refreshRevision = useThrottledValue(revision, REVISION_REFRESH_MS)
  const selected =
    devices.find((device) => device.connector_id === selectedId) || null
  const zones = useMemo(
    () =>
      [
        ...new Set(devices.map((device) => device.zone_id).filter(Boolean)),
      ].sort(),
    [devices],
  )

  useEffect(() => {
    if (initial.get('tab') !== 'employee-repair') return
    const params = new URLSearchParams({ view: 'needs-review' })
    const deviceId = initial.get('device_id')
    if (deviceId) params.set('device_id', deviceId)
    window.history.replaceState(null, '', `/attendance?${params}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, [initial])

  const setWorkspaceSection = (next: ReconciliationSection) => {
    setSection(next)
    const params = new URLSearchParams()
    if (next === 'exceptions') params.set('tab', 'source-exceptions')
    if (next === 'recovery') params.set('tab', 'recovery')
    const deviceId = next === 'exceptions' ? exceptionFilters.device_id : ''
    const jobId = next === 'exceptions' ? exceptionFilters.job_id : ''
    if (deviceId) params.set('device_id', deviceId)
    if (jobId) params.set('job_id', jobId)
    window.history.pushState(
      null,
      '',
      `${window.location.pathname}${params.size ? `?${params}` : ''}`,
    )
  }
  const reviewJobExceptions = (job: ReconciliationJob) => {
    const next = {
      ...defaultExceptionFilters,
      job_id: job.job_id,
      device_id: job.connector?.connector_id || '',
    }
    setJobDrawer(null)
    setExceptionDraft(next)
    setExceptionFilters(next)
    setSection('exceptions')
    const params = new URLSearchParams({
      tab: 'source-exceptions',
      job_id: job.job_id,
    })
    if (job.connector?.connector_id)
      params.set('device_id', job.connector.connector_id)
    window.history.pushState(
      null,
      '',
      `${window.location.pathname}?${params}`,
    )
  }
  useEffect(() => {
    const onPopState = () => {
      const params = new URLSearchParams(window.location.search)
      const next =
        params.get('tab') === 'source-exceptions'
          ? 'exceptions'
          : params.get('tab') === 'recovery'
            ? 'recovery'
            : 'jobs'
      setSection(next)
      if (next === 'exceptions') {
        const deviceId = params.get('device_id') || ''
        const jobId = params.get('job_id') || ''
        setExceptionDraft((current) => ({
          ...current,
          device_id: deviceId,
          job_id: jobId,
        }))
        setExceptionFilters((current) => ({
          ...current,
          device_id: deviceId,
          job_id: jobId,
        }))
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const loadJobs = useCallback(
    async ({
      cursor,
      append = false,
      quiet = false,
      refresh = false,
    }: {
      cursor?: number
      append?: boolean
      quiet?: boolean
      refresh?: boolean
    } = {}) => {
      if (append) setJobLoadingMore(true)
      else if (!quiet) setJobLoading(true)
      setJobError('')
      jobAbortRef.current?.abort()
      const controller = new AbortController()
      jobAbortRef.current = controller
      // A refresh re-reads the whole loaded window instead of page 1, so the
      // rows the operator paged in stay put.
      const limit = refresh
        ? Math.min(500, Math.max(JOB_PAGE, rowsRef.current.length))
        : JOB_PAGE
      try {
        const response = await api<ReconciliationListResponse>(
          `/api/v1/reconciliations${queryString({ q: jobQuery.trim() || undefined, zone_id: jobZone || undefined, status_group: jobStatus || undefined, cursor, limit })}`,
          { signal: controller.signal },
        )
        let nextCursor = response.next_cursor
        if (append) {
          setRows((current) => [
            ...new Map(
              [...current, ...response.rows].map((row) => [row.job_id, row]),
            ).values(),
          ])
        } else if (refresh) {
          const merged = mergeJobWindow(
            rowsRef.current,
            response.rows,
            response.next_cursor != null,
          )
          if (merged.keptTail) nextCursor = jobCursorRef.current
          rowsRef.current = merged.rows
          setRows(merged.rows)
        } else {
          setRows(response.rows)
        }
        setEnabled(response.enabled)
        setScheduler(response.scheduler || emptyScheduler)
        setJobTotals(response.totals || emptyJobTotals)
        setJobCursor(nextCursor)
        setJobFilteredTotal(response.filtered_total)
      } catch (reason) {
        if (!(reason instanceof DOMException && reason.name === 'AbortError'))
          setJobError(
            reason instanceof Error
              ? reason.message
              : 'Reconciliation jobs could not be loaded.',
          )
      } finally {
        if (jobAbortRef.current === controller) {
          setJobLoading(false)
          setJobLoadingMore(false)
        }
      }
    },
    [jobQuery, jobStatus, jobZone],
  )

  const loadExceptions = useCallback(
    async ({
      cursor,
      append = false,
      quiet = false,
    }: { cursor?: number; append?: boolean; quiet?: boolean } = {}) => {
      if (append) setExceptionLoadingMore(true)
      else if (!quiet) setExceptionLoading(true)
      setExceptionError('')
      exceptionAbortRef.current?.abort()
      const controller = new AbortController()
      exceptionAbortRef.current = controller
      try {
        const response = await api<SourceExceptionList>(
          `/api/v1/source-exceptions${queryString({ ...exceptionFilters, error_code: exceptionFilters.error_code || undefined, ordinal: exceptionFilters.ordinal || undefined, cursor, limit: 50 })}`,
          { signal: controller.signal },
        )
        setExceptionRows((current) =>
          append
            ? [
                ...new Map(
                  [...current, ...response.rows].map((row) => [row.id, row]),
                ).values(),
              ]
            : response.rows,
        )
        setExceptionTotals(response.totals)
        setExceptionScope(response.scope || null)
        setExceptionFilteredTotal(
          response.filtered_total ?? response.rows.length,
        )
        setExceptionCursor(response.next_cursor)
      } catch (reason) {
        if (!(reason instanceof DOMException && reason.name === 'AbortError'))
          setExceptionError(
            reason instanceof Error
              ? reason.message
              : 'Terminal source exceptions could not be loaded.',
          )
      } finally {
        if (exceptionAbortRef.current === controller) {
          setExceptionLoading(false)
          setExceptionLoadingMore(false)
        }
      }
    },
    [exceptionFilters],
  )

  useEffect(() => {
    const timer = window.setTimeout(() => void loadJobs(), 250)
    return () => window.clearTimeout(timer)
  }, [loadJobs])
  // Revisions refresh in place; the first load above is the mount's only load,
  // so a refresh never races it and replaces rows the operator paged in.
  const listRevisionRef = useRef(refreshRevision)
  useEffect(() => {
    if (listRevisionRef.current === refreshRevision) return
    listRevisionRef.current = refreshRevision
    if (section === 'jobs') void loadJobs({ quiet: true, refresh: true })
    else if (section === 'exceptions') void loadExceptions({ quiet: true })
  }, [refreshRevision]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    void loadExceptions()
  }, [loadExceptions])
  useEffect(() => {
    void api<{
      delivery: {
        safe_retryable: number
        identity_held: number
        permanent_review: number
      }
    }>('/api/v2/attendance-recovery/summary')
      .then((result) =>
        setRecoveryCount(
          result.delivery.safe_retryable +
            result.delivery.identity_held +
            result.delivery.permanent_review,
        ),
      )
      .catch(() => setRecoveryCount(0))
  }, [refreshRevision])
  useEffect(
    () => () => {
      jobAbortRef.current?.abort()
      exceptionAbortRef.current?.abort()
      preflightAbortRef.current?.abort()
    },
    [],
  )
  const loadPreflight = useCallback(async (id: string, quiet: boolean) => {
    preflightAbortRef.current?.abort()
    if (!id) return
    const controller = new AbortController()
    preflightAbortRef.current = controller
    if (!quiet) setPreflightLoading(true)
    const [detail, check] = await Promise.allSettled([
      api<Device>(`/api/v1/devices/${id}`, { signal: controller.signal }),
      api<ReconciliationPreflight>(
        `/api/v1/devices/${id}/reconciliations/preflight`,
        { signal: controller.signal },
      ),
    ])
    if (controller.signal.aborted) return
    // A quiet refresh swaps new data in and keeps the old panel on failure.
    if (detail.status === 'fulfilled') setDeviceDetail(detail.value)
    if (check.status === 'fulfilled') {
      setPreflight(check.value)
      setPreflightError('')
    } else if (!quiet)
      setPreflightError(
        check.reason instanceof Error
          ? check.reason.message
          : 'Preflight could not be loaded.',
      )
    setPreflightLoading(false)
  }, [])
  // Only a new terminal clears the readiness panel; revisions refresh it in place.
  useEffect(() => {
    setDeviceDetail(null)
    setPreflight(null)
    setPreflightError('')
    setPreflightLoading(false)
    void loadPreflight(selectedId, false)
  }, [selectedId, loadPreflight])
  const preflightRevisionRef = useRef(refreshRevision)
  useEffect(() => {
    if (preflightRevisionRef.current === refreshRevision) return
    preflightRevisionRef.current = refreshRevision
    if (selectedId) void loadPreflight(selectedId, true)
  }, [refreshRevision]) // eslint-disable-line react-hooks/exhaustive-deps

  const failure = (error: unknown, fallback: string) =>
    toast.error(error instanceof Error ? error.message : fallback)
  const refreshJobsAndReadiness = async () => {
    await Promise.all([
      loadJobs({ quiet: true, refresh: true }),
      selectedId ? loadPreflight(selectedId, true) : Promise.resolve(),
    ])
  }
  const start = async () => {
    if (!selected) return
    const scope =
      selected.firmware_family === 'hikvision' ? historyScope : 'ALL_RECORDS'
    setBusyKey('start')
    try {
      await api<ReconciliationJob>(
        `/api/v1/devices/${selected.connector_id}/reconciliations/full-history`,
        {
          method: 'POST',
          body: JSON.stringify({
            idempotency_key: idempotency('full-history'),
            scope,
          }),
        },
      )
      const message =
        scope === 'ACTIVE_USERS'
          ? 'Active-user history queued. ADD freezes the current user list and preserves resumable checkpoints.'
          : scope === 'RECENT_TAIL'
            ? 'Recent-tail recovery queued. ADD resumes from its last held serial and keeps following new punches.'
            : 'Full terminal reconciliation queued. ADD now owns its checkpoint.'
      toast.notice(message)
      setLiveStatus(`Reconcile queued for ${selected.display_name}.`)
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, 'Could not start reconciliation.')
    } finally {
      setBusyKey(null)
    }
  }
  const control = async (
    job: ReconciliationJob,
    action: 'pause' | 'resume' | 'cancel' | 'retry',
  ) => {
    setBusyKey(`${job.job_id}:${action}`)
    try {
      const updated = await api<ReconciliationJob>(
        `/api/v1/reconciliations/${job.job_id}/${action}`,
        {
          method: 'POST',
          body: JSON.stringify({
            idempotency_key: idempotency(`reconcile-${action}`),
          }),
        },
      )
      setRows((current) =>
        current.map((row) =>
          row.job_id === updated.job_id ? { ...row, ...updated } : row,
        ),
      )
      toast.notice(`Reconciliation ${action} recorded in the audit trail.`)
      setLiveStatus(
        `${humanize(action)} recorded for ${job.connector?.display_name || job.job_id}.`,
      )
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, 'Reconciliation control failed.')
    } finally {
      setBusyKey(null)
    }
  }
  const archive = async (job: ReconciliationJob, cancelFirst: boolean) => {
    const action = cancelFirst ? 'cancel-and-archive' : 'archive'
    setBusyKey(`${job.job_id}:${action}`)
    try {
      await api<ReconciliationJob>(
        `/api/v1/reconciliations/${job.job_id}/${action}`,
        {
          method: 'POST',
          body: JSON.stringify({
            idempotency_key: idempotency(`reconcile-${action}`),
          }),
        },
      )
      // Remove the card in place; the quiet refresh then fills the window.
      setRows((current) => {
        const next = current.filter((row) => row.job_id !== job.job_id)
        rowsRef.current = next
        return next
      })
      setJobFilteredTotal((total) => Math.max(0, total - 1))
      toast.notice(
        cancelFirst
          ? 'Reconciliation cancelled and archived.'
          : 'Reconciliation archived.',
      )
      setLiveStatus(
        `${job.connector?.display_name || job.job_id} ${cancelFirst ? 'cancelled and archived' : 'archived'}.`,
      )
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, 'Reconciliation could not be archived.')
    } finally {
      setBusyKey(null)
    }
  }
  const startBulk = async (connectorIds: string[]) => {
    setBusyKey('bulk')
    setBulkResult(null)
    try {
      const results: ReconciliationBulkResult[] = []
      for (let index = 0; index < connectorIds.length; index += BULK_REQUEST_LIMIT) {
        const response = await api<ReconciliationBulkResponse>(
          '/api/v1/reconciliations/bulk',
          {
            method: 'POST',
            body: JSON.stringify({
              connector_ids: connectorIds.slice(index, index + BULK_REQUEST_LIMIT),
              scope: 'ALL_RECORDS',
              idempotency_key: idempotency('reconcile-bulk'),
            }),
          },
        )
        results.push(...response.results)
      }
      const created = results.filter((row) => row.outcome === 'CREATED').length
      setBulkResult({ results, created })
      toast.notice(
        `${created} reconcile${created === 1 ? '' : 's'} started; ${results.length - created} not started.`,
      )
      setLiveStatus(`${created} of ${results.length} selected terminals started.`)
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, 'Bulk reconcile could not start.')
    } finally {
      setBusyKey(null)
    }
  }
  const clearHistory = async () => {
    setBusyKey('clear')
    try {
      const result = await api<ReconciliationClearHistoryResult>(
        '/api/v1/reconciliations/clear-history',
        {
          method: 'POST',
          body: JSON.stringify({
            idempotency_key: idempotency('reconcile-clear-history'),
          }),
        },
      )
      rowsRef.current = []
      setRows([])
      setJobCursor(null)
      const message = `Reconcile history cleared: ${result.cancelled} cancelled, ${result.deleted} deleted, ${result.archived} archived as evidence.`
      toast.notice(message)
      setLiveStatus(message)
      await refreshJobsAndReadiness()
    } catch (error) {
      failure(error, 'Reconcile history could not be cleared.')
    } finally {
      setBusyKey(null)
    }
  }

  const isHikvision = selected?.firmware_family === 'hikvision'
  const activeUsersOnly = isHikvision && historyScope === 'ACTIVE_USERS'
  const recentTail = isHikvision && historyScope === 'RECENT_TAIL'
  const canStart = Boolean(
    selected &&
      enabled &&
      preflight?.eligible &&
      (!activeUsersOnly || preflight?.active_user_history?.eligible !== false) &&
      (!recentTail || preflight?.recent_tail?.eligible === true),
  )
  const startBlockedReason = !selected
    ? 'Select a terminal first.'
    : !enabled
      ? 'Reconciliation is not enabled on this server.'
      : !preflight
        ? 'Checking readiness…'
        : preflight.hard_blockers[0]?.message ||
          (activeUsersOnly && preflight.active_user_history?.eligible === false
            ? preflight.active_user_history.reason || 'Active-user history is not available.'
            : recentTail && preflight.recent_tail?.eligible !== true
              ? preflight.recent_tail?.reason || 'Recent-tail recovery is not available.'
              : undefined)
  const activeJobCount =
    jobTotals.active +
    jobTotals.queued_waiting +
    jobTotals.paused +
    jobTotals.attention
  const activeJobFilters =
    Number(Boolean(jobQuery.trim())) +
    Number(Boolean(jobZone)) +
    Number(Boolean(jobStatus))
  const activeExceptionFilters =
    Object.values(exceptionFilters).filter(Boolean).length
  const scopedAssurance =
    exceptionScope?.source_exception_assurance || null
  const scanQueue = rows
    .filter(
      (job) =>
        !job.capture_certified_at && ['QUEUED', 'RUNNING'].includes(job.status),
    )
    .sort((left, right) => left.requested_at.localeCompare(right.requested_at))
  const queuePositions = new Map(
    scanQueue.map((job, index) => [job.job_id, index + 1]),
  )

  return (
    <div className="reconciliation-workspace">
      <PageHeader
        title="Reconciliation"
        description="Recover complete terminal history, prove Oracle delivery, and review source exceptions. Every checkpoint stays immutable."
        action={
          <div className="page-actions">
            <button
              className="button secondary"
              onClick={() =>
                void (section === 'jobs'
                  ? refreshJobsAndReadiness()
                  : section === 'exceptions'
                    ? loadExceptions({ quiet: true })
                    : api('/api/v2/attendance-recovery/summary').then(() => undefined))
              }
            >
              <Icon name="refresh" /> Refresh
            </button>
            {section === 'jobs' && (
              <>
                <button
                  className="button secondary"
                  type="button"
                  aria-expanded={bulkOpen}
                  onClick={() => setBulkOpen((open) => !open)}
                >
                  <Icon name="list" /> Bulk reconcile
                </button>
                <button
                  className="button destructive-outline"
                  type="button"
                  disabled={busyKey === 'clear'}
                  title="Cancels every open reconcile, deletes finished jobs that no proof depends on, and archives the rest."
                  onClick={() => void clearHistory()}
                >
                  <Icon name="trash" />{' '}
                  {busyKey === 'clear' ? 'Clearing…' : 'Clear reconcile history'}
                </button>
              </>
            )}
          </div>
        }
      />
      <WorkspaceTabs
        section={section}
        activeJobs={activeJobCount}
        sourceExceptions={exceptionTotals.all}
        recoveryCount={recoveryCount}
        onChange={setWorkspaceSection}
      />
      {section === 'recovery' ? (
        <div
          role="tabpanel"
          id="reconciliation-recovery-panel"
          aria-labelledby="reconciliation-recovery-tab"
        >
          <AttendanceRecovery revision={revision} toast={toast} />
        </div>
      ) : section === 'jobs' ? (
        <div
          role="tabpanel"
          id="reconciliation-jobs-panel"
          aria-labelledby="reconciliation-jobs-tab"
        >
          <section className="metric-grid reconciliation-metrics">
            <Metric
              label="Active terminal scans"
              value={scheduler.active_scan_jobs}
              detail="Capture slots currently working"
              icon="refresh"
              tone={scheduler.active_scan_jobs ? 'positive' : 'neutral'}
            />
            <Metric
              label="Queued and waiting"
              value={scheduler.waiting_scan_jobs}
              detail="Safe-window and capacity waits"
              icon="clock"
              tone={scheduler.waiting_scan_jobs ? 'warning' : 'neutral'}
            />
            <Metric
              label="Available scan slots"
              value={`${scheduler.available_scan_slots}/${scheduler.device_concurrency}`}
              detail="Isolated one-job-per-terminal capacity"
              icon="server"
              tone="positive"
            />
            <Metric
              label="Oracle history backlog"
              value={(scheduler.history_backlog ?? 0).toLocaleString()}
              detail={`Safe limit ${(scheduler.history_backlog_limit ?? 0).toLocaleString()}`}
              icon="shield"
              tone={
                (scheduler.history_backlog ?? 0) >=
                (scheduler.history_backlog_limit ?? Number.MAX_SAFE_INTEGER)
                  ? 'warning'
                  : 'neutral'
              }
            />
          </section>
          {bulkOpen && (
            <BulkReconcilePanel
              devices={devices}
              busy={busyKey === 'bulk'}
              result={bulkResult}
              onStart={(ids) => void startBulk(ids)}
              onClose={() => {
                setBulkOpen(false)
                setBulkResult(null)
              }}
            />
          )}
          <TerminalPicker
            devices={devices}
            selectedId={selectedId}
            open={pickerOpen}
            onOpenChange={setPickerOpen}
            onSelect={setSelectedId}
          />
          {selected && (
            <section className="reconciliation-readiness panel">
              <div className="panel-header">
                <div>
                  <h2>Readiness for {selected.display_name}</h2>
                  <p>
                    {selected.zone_name} · connector {selected.connector_id} ·
                    device {selected.device_id}
                  </p>
                </div>
                <div className="reconciliation-start-actions">
                  <StatusBadge
                    state={
                      preflightLoading && !preflight
                        ? 'CHECKING'
                        : preflight?.ready_now
                          ? 'READY'
                          : preflight?.eligible
                            ? 'WAITING FOR SAFE WINDOW'
                            : 'BLOCKED'
                    }
                  />
                  {isHikvision && (
                    <label className="reconciliation-scope-select">
                      <span className="sr-only">History scope</span>
                      <select
                        aria-label="History scope"
                        value={historyScope}
                        onChange={(event) =>
                          setHistoryScope(
                            event.target.value as HikvisionHistoryScope,
                          )
                        }
                      >
                        <option value="ALL_RECORDS">Full history</option>
                        <option value="ACTIVE_USERS">Active users</option>
                        <option value="RECENT_TAIL">Recent tail</option>
                      </select>
                    </label>
                  )}
                  <button
                    className="button primary"
                    type="button"
                    disabled={!canStart || busyKey === 'start'}
                    title={
                      startBlockedReason ||
                      (preflight?.ready_now
                        ? 'Starts now.'
                        : 'Queues now and starts when the terminal is safe to read.')
                    }
                    onClick={() => void start()}
                  >
                    <Icon name="refresh" />{' '}
                    {busyKey === 'start' ? 'Starting…' : 'Reconcile now'}
                  </button>
                </div>
              </div>
              {isHikvision && (
                <p className="reconciliation-scope-note">
                  {activeUsersOnly
                    ? preflight?.active_user_history
                      ? preflight.active_user_history.eligible
                        ? `${preflight.active_user_history.user_count} active users in the verified snapshot. Searches only those employees’ retained history; former users are excluded.`
                        : preflight.active_user_history.reason
                      : 'Searches only current employees’ retained history; former users are excluded.'
                    : recentTail
                      ? preflight?.recent_tail
                        ? preflight.recent_tail.eligible
                          ? `Resumes at terminal serial ${preflight.recent_tail.begin_serial} (re-reads up to serial ${preflight.recent_tail.anchor_serial}, the last serial before the newest break in ADD's ESP-poll records) and keeps following new punches until cancelled. It never certifies coverage.${preflight.recent_tail.earlier_breaks?.length ? ` ADD's poll records also break after serial${preflight.recent_tail.earlier_breaks.length === 1 ? '' : 's'} ${preflight.recent_tail.earlier_breaks.join(', ')}; only a full history read covers those.` : ''}`
                          : preflight.recent_tail.reason
                        : 'Reads every record after the last held punch and keeps following new punches.'
                      : 'Scans all retained terminal records, including former users and non-attendance events.'}
                </p>
              )}
              {preflightLoading && !preflight && (
                <div className="reconciliation-skeleton compact">
                  <i />
                  <i />
                </div>
              )}
              {preflightError && (
                <div className="inline-retry pattern-blocked" role="alert">
                  <Icon name="alert" />
                  <div>
                    <strong>Preflight is unavailable</strong>
                    <p>{preflightError}</p>
                  </div>
                </div>
              )}
              {preflight && (
                <div className="reconciliation-checklist">
                  <article>
                    <Icon name={selected.connected ? 'check' : 'alert'} />
                    <span>
                      <strong>ESP connection</strong>
                      <small>
                        {selected.connected
                          ? 'Online and authenticated'
                          : 'Offline; job will wait safely'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={
                        preflight.terminal?.connection_state === 'ONLINE'
                          ? 'check'
                          : 'clock'
                      }
                    />
                    <span>
                      <strong>{selected.firmware_family === 'hikvision' ? 'Hikvision terminal' : 'ZKT terminal'}</strong>
                      <small>
                        {preflight.terminal?.connection_state ||
                          'No terminal evidence'}{' '}
                        · {preflight.terminal?.serial || 'serial pending'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={
                        (preflight.source_protocol === 'hikvision-isapi-v1' ? preflight.terminal?.serial_search_enabled : preflight.terminal?.range_resume_verified)
                          ? 'check'
                          : 'alert'
                      }
                    />
                    <span>
                      <strong>{preflight.source_protocol === 'hikvision-isapi-v1' ? 'Serial-source search' : 'Range-resume firmware'}</strong>
                      <small>
                        {(preflight.source_protocol === 'hikvision-isapi-v1' ? preflight.terminal?.serial_search_enabled : preflight.terminal?.range_resume_verified)
                          ? (preflight.source_protocol === 'hikvision-isapi-v1' ? 'Enabled; coverage verified per job' : 'Verified for restart-safe reads')
                          : 'Not verified'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={selected.firmware_family === 'hikvision' || selected.zkt?.snapshot_complete ? 'check' : 'alert'}
                    />
                    <span>
                      <strong>{selected.firmware_family === 'hikvision' ? 'Name-CNIC identity' : 'Identity snapshot'}</strong>
                      <small>
                        {selected.firmware_family === 'hikvision' ? 'Valid name-CNIC records may proceed; missing identities are held' : selected.zkt?.snapshot_complete
                          ? 'Complete snapshot available'
                          : 'Incomplete; identity safety may block'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={deviceDetail?.active_command ? 'clock' : 'check'}
                    />
                    <span>
                      <strong>Conflicting command</strong>
                      <small>
                        {deviceDetail?.active_command
                          ? humanize(deviceDetail.active_command.status)
                          : 'None active'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={deviceDetail?.active_lease ? 'clock' : 'check'}
                    />
                    <span>
                      <strong>Enrollment lease</strong>
                      <small>
                        {deviceDetail?.active_lease
                          ? `${humanize(deviceDetail.active_lease.state)} · ${relativeTime(deviceDetail.active_lease.expires_at)}`
                          : 'No active lease'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={
                        preflight.coverage?.chain_continuous === false
                          ? 'alert'
                          : 'shield'
                      }
                    />
                    <span>
                      <strong>Source coverage</strong>
                      <small>
                        {preflight.coverage
                          ? `${humanize(preflight.coverage.capture_state)} · cursor ${preflight.coverage.source_committed_cursor.toLocaleString()}`
                          : 'No prior coverage certificate'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={
                        preflight.coverage?.oracle_certified_at
                          ? 'check'
                          : 'clock'
                      }
                    />
                    <span>
                      <strong>Oracle assurance</strong>
                      <small>
                        {preflight.coverage
                          ? humanize(preflight.coverage.oracle_state)
                          : 'No prior certificate'}
                      </small>
                    </span>
                  </article>
                </div>
              )}
              {preflight &&
                (preflight.hard_blockers.length > 0 ||
                  preflight.waitable_blockers.length > 0) && (
                  <div className="reconciliation-blockers">
                    {preflight.hard_blockers.length > 0 && (
                      <section className="pattern-blocked">
                        <h3>
                          <Icon name="alert" /> Hard blockers
                        </h3>
                        {preflight.hard_blockers.map((item) => (
                          <p key={item.code}>
                            <strong>{humanize(item.code)}</strong>
                            {item.message}
                          </p>
                        ))}
                      </section>
                    )}
                    {preflight.waitable_blockers.length > 0 && (
                      <section className="pattern-waiting">
                        <h3>
                          <Icon name="clock" /> Safe-window waits
                        </h3>
                        {preflight.waitable_blockers.map((item) => (
                          <p key={item.code}>
                            <strong>{humanize(item.code)}</strong>
                            {item.message}
                          </p>
                        ))}
                      </section>
                    )}
                  </div>
                )}
            </section>
          )}
          <section className="panel reconciliation-ledger">
            <div className="panel-header">
              <div>
                <h2>Reconciliation jobs</h2>
                <p>
                  Newest first, with capture and Oracle assurance progress for
                  each terminal. Archived jobs are hidden.
                </p>
              </div>
              <span className="badge">
                {jobFilteredTotal.toLocaleString()} matching
              </span>
            </div>
            <div className="reconciliation-toolbar">
              <label className="search-field">
                <span className="sr-only">Search reconciliation jobs</span>
                <Icon name="search" />
                <input
                  value={jobQuery}
                  onChange={(event) => setJobQuery(event.target.value)}
                  placeholder="Search job, terminal, connector, device, or zone"
                />
              </label>
              <label>
                <span className="sr-only">Status</span>
                <select
                  value={jobStatus}
                  onChange={(event) =>
                    setJobStatus(
                      event.target.value as ReconciliationStatusGroup,
                    )
                  }
                >
                  <option value="">All statuses</option>
                  <option value="ACTIVE">Active</option>
                  <option value="QUEUED_WAITING">Queued / waiting</option>
                  <option value="PAUSED">Paused</option>
                  <option value="ATTENTION">Needs attention</option>
                  <option value="COMPLETED">Completed</option>
                  <option value="CANCELLED">Cancelled</option>
                </select>
              </label>
              <label>
                <span className="sr-only">Zone</span>
                <select
                  value={jobZone}
                  onChange={(event) => setJobZone(event.target.value)}
                >
                  <option value="">All zones</option>
                  {zones.map((zone) => (
                    <option key={zone}>{zone}</option>
                  ))}
                </select>
              </label>
              {activeJobFilters > 0 && (
                <button
                  className="button secondary"
                  onClick={() => {
                    setJobQuery('')
                    setJobStatus('')
                    setJobZone('')
                  }}
                >
                  Clear filters
                </button>
              )}
            </div>
            {jobError && (
              <div className="inline-retry pattern-blocked" role="alert">
                <Icon name="alert" />
                <div>
                  <strong>Latest job data could not be loaded</strong>
                  <p>
                    {jobError}
                    {rows.length ? ' Existing rows remain visible.' : ''}
                  </p>
                </div>
                <button
                  className="button secondary"
                  onClick={() => void loadJobs()}
                >
                  Retry
                </button>
              </div>
            )}
            {jobLoading && !rows.length && (
              <div className="reconciliation-skeleton">
                <i />
                <i />
                <i />
              </div>
            )}
            <p className="reconciliation-live-status" aria-live="polite">
              {liveStatus}
            </p>
            <div className="reconciliation-job-list">
              {rows.map((job) => {
                const cutoff = job.terminal.cutoff_count || 0
                const capturePercent = job.scope === 'RECENT_TAIL'
                  ? job.recent_tail?.caught_up ? 100 : 0
                  : job.scope === 'ACTIVE_USERS' && job.active_user_scope?.user_count
                  ? job.capture_certified_at ? 100 : Math.min(99, Math.round(100 * job.active_user_scope.completed_users / job.active_user_scope.user_count))
                  : cutoff ? Math.min(
                      100,
                      Math.round((job.progress.scanned / cutoff) * 100),
                    )
                  : job.capture_certified_at
                    ? 100
                    : 0
                const oracleTarget = Math.max(job.progress.oracle_target || 0, job.progress.oracle_confirmed + job.progress.oracle_pending + (job.progress.oracle_review_required || 0) + (job.progress.blocked_identity || 0))
                const rawPending = job.source_exception_assurance?.raw_interpretation_open ?? 0
                const oraclePercent = oracleTarget
                  ? Math.min(
                      rawPending ? 99 : 100,
                      job.progress.oracle_confirmed < oracleTarget ? Math.min(99, Math.floor((job.progress.oracle_confirmed / oracleTarget) * 100)) : 100,
                    )
                  : job.oracle_certified_at
                    ? 100
                    : 0
                const queuePosition = queuePositions.get(job.job_id)
                const sourceAssurance =
                  job.source_exception_assurance || emptySourceAssurance
                const sourceGateActive =
                  sourceAssurance.state === 'REVIEW_REQUIRED' ||
                  sourceAssurance.state === 'IDENTITY_EVIDENCE_REQUIRED' ||
                  sourceAssurance.state === 'INTERPRETATION_REQUIRED' ||
                  sourceAssurance.state === 'NEWEST_PUNCH_UNRECEIVED' ||
                  sourceAssurance.state === 'SCOPE_MISMATCH'
                const controls: Array<'pause' | 'resume' | 'cancel' | 'retry'> =
                  []
                if (
                  ['QUEUED', 'RUNNING', 'PAUSE_REQUESTED'].includes(job.status)
                )
                  controls.push('pause')
                if (job.status === 'PAUSED') controls.push('resume')
                if (job.status === 'NEEDS_ATTENTION' && !sourceGateActive)
                  controls.push('retry')
                if (!terminalJobStates.has(job.status)) controls.push('cancel')
                const directAction = controls.find(
                  (action) => action !== 'cancel',
                )
                const queueCopy =
                  job.operator_message ||
                  (queuePosition
                    ? `Queue position ${queuePosition}`
                    : humanize(job.wait_reason || job.phase))
                return (
                  <article
                    key={job.job_id}
                    className={`reconciliation-job pattern-${statusPattern(job.operator_state || job.status)}`}
                  >
                    <header>
                      <div>
                        <p className="eyebrow">
                          {job.connector?.zone_id || 'UNKNOWN ZONE'}
                        </p>
                        <h3>{job.connector?.display_name || job.job_id}</h3>
                        {job.mode === 'HIKVISION_SERIAL_HISTORY' && <p>{job.scope === 'ACTIVE_USERS'
                          ? `Active-user history · ${job.active_user_scope?.completed_users ?? 0}/${job.active_user_scope?.user_count ?? 0} users verified · excludes former users`
                          : job.scope === 'RECENT_TAIL'
                            ? `Recent tail · from serial ${job.recent_tail?.begin_serial ?? '?'} · last ${job.recent_tail?.last_serial ?? 'pending'} · ${job.recent_tail?.records ?? 0} new records after serial ${job.recent_tail?.anchor_serial ?? '?'} · ${job.recent_tail?.overlap_records ?? 0} held records re-read${job.recent_tail?.fact_conflicts ? ` · ${job.recent_tail.fact_conflicts} changed serials held for review` : ''} · no coverage certificate`
                            : 'Full terminal audit · all records'}</p>}
                        <small>
                          {job.terminal.serial || 'serial pending'} · requested{' '}
                          {dateTime(job.requested_at)}
                        </small>
                      </div>
                      <StatusBadge
                        state={job.operator_state || job.status}
                        live={job.status === 'RUNNING'}
                      />
                    </header>
                    <div className="reconciliation-job-message">
                      <strong>
                        {humanize(job.operator_state || job.phase)}
                      </strong>
                      <span>{queueCopy}</span>
                      <small>
                        Updated {relativeTime(job.updated_at)} · ETA{' '}
                        {reconciliationEtaLabel(job)} (
                        {humanize(job.eta.confidence)})
                      </small>
                    </div>
                    <div className="reconciliation-progress-grid">
                      <section>
                        <div>
                          <strong>{job.scope === 'ACTIVE_USERS' ? 'User history verified' : 'Terminal capture'}</strong>
                          <span>{capturePercent}%</span>
                        </div>
                        <div
                          className="reconciliation-progress"
                          role="progressbar"
                          aria-label="Terminal capture progress"
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={capturePercent}
                        >
                          <i style={{ width: `${capturePercent}%` }} />
                        </div>
                        <p>
                          {job.progress.scanned.toLocaleString()} scanned ·{' '}
                          {job.progress.add_durable.toLocaleString()} durable ·{' '}
                          {job.progress.blocked_identity.toLocaleString()}{' '}
                          identity held ·{' '}
                          {job.progress.quarantined.toLocaleString()}{' '}
                          quarantined
                          {!!job.progress.raw_preserved && ` · ${job.progress.raw_preserved.toLocaleString()} awaiting interpretation`}
                        </p>
                      </section>
                      <section>
                        <div>
                          <strong>Oracle assurance</strong>
                          <span>{oraclePercent}%</span>
                        </div>
                        <div
                          className="reconciliation-progress oracle"
                          role="progressbar"
                          aria-label="Oracle assurance progress"
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={oraclePercent}
                        >
                          <i style={{ width: `${oraclePercent}%` }} />
                        </div>
                        <p>
                          {job.progress.oracle_confirmed.toLocaleString()}{' '}
                          confirmed ·{' '}
                          {job.progress.oracle_pending.toLocaleString()} pending
                          ·{' '}
                          {(
                            job.progress.oracle_review_required ?? 0
                          ).toLocaleString()}{' '}
                          review · {(job.progress.blocked_identity || 0).toLocaleString()} waiting for identity evidence
                        </p>
                        <p className="muted">For this reconciliation’s records only. <a href="/attendance?view=needs-review">Check all saved attendance</a> for other held punches.</p>
                      </section>
                    </div>
                    <dl className="reconciliation-job-facts">
                      <div>
                        <dt>Checkpoint</dt>
                        <dd>
                          {(job.checkpoint?.source_serial ?? job.checkpoint?.next_ordinal)?.toLocaleString() ??
                            job.progress.scanned.toLocaleString()}
                        </dd>
                      </div>
                      <div>
                        <dt>Source epoch</dt>
                        <dd>{job.mode === 'HIKVISION_SERIAL_HISTORY' ? job.recovery?.source_epoch_id || 'Pending' : job.recovery?.source_epoch ?? 1}</dd>
                      </div>
                      <div>
                        <dt>Assignment</dt>
                        <dd>
                          {job.assignment.assignment_id ? 'Active' : 'None'}
                        </dd>
                      </div>
                      <div>
                        <dt>Recovery attempts</dt>
                        <dd>{job.progress.auto_retry_count ?? 0}</dd>
                      </div>
                    </dl>
                    {sourceAssurance.total > 0 && (
                      <div
                        className={`reconciliation-source-assurance pattern-${sourceEvidencePending(sourceAssurance.state) ? 'waiting' : sourceAssurance.state === 'SCOPE_MISMATCH' ? 'blocked' : 'notice'}`}
                      >
                        <Icon name="shield" />
                        <span>
                          <strong>
                            {job.status === 'CANCELLED'
                              ? 'Cancelled — source exceptions remain preserved'
                              : sourceAssurance.state === 'INTERPRETATION_REQUIRED'
                              ? 'Source interpretation pending'
                              : sourceAssurance.state === 'IDENTITY_EVIDENCE_REQUIRED'
                              ? 'Historical identity evidence required'
                              : sourceAssurance.state === 'NEWEST_PUNCH_UNRECEIVED'
                              ? 'Terminal punch not yet received'
                              : sourceAssurance.state === 'REVIEW_REQUIRED'
                              ? `${sourceAssurance.open.toLocaleString()} source review${sourceAssurance.open === 1 ? '' : 's'} remaining`
                              : sourceAssurance.state === 'SCOPE_MISMATCH'
                                ? 'Source-exception evidence mismatch'
                                : job.status === 'COMPLETED'
                                  ? 'Completed with reviewed exclusions'
                                  : terminalJobStates.has(job.status)
                                    ? 'Reviewed exclusions — job ended'
                                    : 'Reviewed exclusions — assurance continuing'}
                          </strong>
                          {sourceAssurance.state === 'INTERPRETATION_REQUIRED' ? (
                            `${sourceAssurance.raw_interpretation_open ?? 0} raw source records preserved; attendance and Oracle completion remain unproven.`
                          ) : <>
                          {sourceAssurance.reviewed.toLocaleString()} of{' '}
                          {sourceAssurance.total.toLocaleString()} certified
                          exceptions reviewed; original evidence remains preserved.
                          {sourceAssurance.state === 'IDENTITY_EVIDENCE_REQUIRED' &&
                            ' Review notes cannot resolve the identity hold.'}
                          {sourceAssurance.state === 'NEWEST_PUNCH_UNRECEIVED' &&
                            ` ${newestPunchCopy(sourceAssurance.newest_punch_unreceived ?? 1)}`}
                          </>}
                        </span>
                      </div>
                    )}
                    {job.error_message && (
                      <div className="reconciliation-row-alert">
                        <Icon name="alert" />
                        <span>
                          <strong>{humanize(job.error_code)}</strong>
                          {job.error_message}
                        </span>
                      </div>
                    )}
                    <footer>
                      <button
                        className="button secondary"
                        onClick={(event) => {
                          jobDrawerReturnFocusRef.current = event.currentTarget
                          setJobDrawer(job)
                        }}
                      >
                        <Icon name="shield" /> Inspect evidence
                      </button>
                      {sourceAssurance.state === 'REVIEW_REQUIRED' && (
                        <button
                          className="button primary"
                          onClick={() => reviewJobExceptions(job)}
                        >
                          Review {sourceAssurance.open.toLocaleString()}{' '}
                          exception{sourceAssurance.open === 1 ? '' : 's'}
                        </button>
                      )}
                      {(sourceAssurance.state === 'IDENTITY_EVIDENCE_REQUIRED' ||
                        sourceAssurance.state === 'NEWEST_PUNCH_UNRECEIVED') && (
                        <button className="button primary" onClick={() => reviewJobExceptions(job)}>
                          Inspect held source records
                        </button>
                      )}
                      {directAction && (
                        <button
                          className={`button ${sourceAssurance.state === 'REVIEW_REQUIRED' ? 'secondary' : 'primary'}`}
                          disabled={busyKey !== null}
                          onClick={() => void control(job, directAction)}
                        >
                          {humanize(directAction)}
                        </button>
                      )}
                      {controls.includes('cancel') ? (
                        <>
                          <button
                            className="button destructive-outline"
                            disabled={busyKey !== null}
                            onClick={() => void control(job, 'cancel')}
                          >
                            {busyKey === `${job.job_id}:cancel` ? 'Cancelling…' : 'Cancel'}
                          </button>
                          <button
                            className="button secondary"
                            disabled={busyKey !== null}
                            onClick={() => void archive(job, true)}
                          >
                            {busyKey === `${job.job_id}:cancel-and-archive` ? 'Archiving…' : 'Cancel & archive'}
                          </button>
                        </>
                      ) : (
                        <button
                          className="button secondary"
                          disabled={busyKey !== null}
                          onClick={() => void archive(job, false)}
                        >
                          {busyKey === `${job.job_id}:archive` ? 'Archiving…' : 'Archive'}
                        </button>
                      )}
                    </footer>
                  </article>
                )
              })}
              {!jobLoading && !rows.length && (
                <div className="empty-state">
                  <Icon name="refresh" />
                  <h3>
                    {activeJobFilters
                      ? 'No reconciliation jobs match these filters'
                      : 'No open or recent reconciliation jobs'}
                  </h3>
                  <p>
                    {activeJobFilters
                      ? 'Clear one or more filters to broaden the durable ledger.'
                      : 'Select a terminal and press Reconcile now, or reconcile several at once.'}
                  </p>
                </div>
              )}
            </div>
            {jobCursor && (
              <div className="load-more">
                <button
                  className="button secondary"
                  disabled={jobLoadingMore}
                  onClick={() =>
                    void loadJobs({
                      cursor: jobCursor,
                      append: true,
                      quiet: true,
                    })
                  }
                >
                  {jobLoadingMore ? 'Loading older jobs…' : 'Load older jobs'}
                </button>
                <small>
                  {rows.length.toLocaleString()} of{' '}
                  {jobFilteredTotal.toLocaleString()} matching jobs loaded
                </small>
              </div>
            )}
          </section>
        </div>
      ) : (
        <div
          role="tabpanel"
          id="reconciliation-exceptions-panel"
          aria-labelledby="reconciliation-exceptions-tab"
        >
          <p className="reconciliation-metric-caption">
            {exceptionScope
              ? `Certified job cohort · ${exceptionScope.terminal_serial || 'terminal pending'} · cutoff ${(exceptionScope.cutoff_count ?? 0).toLocaleString()}`
              : 'National source-exception totals'}
          </p>
          <section className="metric-grid reconciliation-metrics">
            <Metric
              label={scopedAssurance ? 'Certified exceptions' : 'Open exceptions'}
              value={(
                scopedAssurance?.total ?? exceptionTotals.open
              ).toLocaleString()}
              detail={
                scopedAssurance
                  ? 'Exact immutable job cohort'
                  : 'Awaiting operator review'
              }
              icon="alert"
              tone={
                (scopedAssurance?.open ?? exceptionTotals.open)
                  ? 'warning'
                  : 'positive'
              }
            />
            <Metric
              label={scopedAssurance ? 'Recorded reviews' : 'Invalid timestamps'}
              value={(
                scopedAssurance?.reviewed ?? exceptionTotals.invalid_time
              ).toLocaleString()}
              detail={
                scopedAssurance
                  ? 'Review alone cannot resolve missing identity'
                  : 'Excluded fail-closed'
              }
              icon={scopedAssurance ? 'shield' : 'clock'}
              tone={scopedAssurance ? 'positive' : 'warning'}
            />
            <Metric
              label={scopedAssurance ? 'Unresolved exceptions' : 'Malformed rows'}
              value={(
                scopedAssurance?.open ?? exceptionTotals.malformed
              ).toLocaleString()}
              detail={
                scopedAssurance
                  ? 'Required evidence remains outstanding'
                  : 'Raw evidence preserved'
              }
              icon={scopedAssurance ? 'alert' : 'terminal'}
              tone={scopedAssurance?.open ? 'warning' : 'positive'}
            />
            <Metric
              label={scopedAssurance ? 'Assurance state' : 'Affected terminals'}
              value={
                scopedAssurance
                  ? humanize(scopedAssurance.state)
                  : exceptionTotals.affected_terminals.toLocaleString()
              }
              detail={
                scopedAssurance
                  ? 'Checkpoint and source chain unchanged'
                  : 'Subsequent valid punches continue'
              }
              icon="server"
            />
            <Metric
              label="Missing identity references"
              value={(scopedAssurance ? scopedAssurance.identity_unresolved : exceptionTotals.identity_unresolved)?.toLocaleString() ?? 'Unknown'}
              detail="Historical source count; review does not prove an employee"
              icon="shield"
              tone="warning"
            />
          </section>
          <section className="panel source-exceptions-panel">
            <div className="panel-header">
              <div>
                <h2>Immutable source exception ledger</h2>
                <p>
                  Review never changes a preserved record or creates attendance.
                  Invalid source exclusions can be reviewed. Missing identity
                  references require verified correction evidence before delivery
                  can be certified.
                </p>
              </div>
              <span className="badge">
                {exceptionFilteredTotal.toLocaleString()} matching
              </span>
            </div>
            {exceptionScope && scopedAssurance && (
              <div
                className={`info-copy pattern-${sourceEvidencePending(scopedAssurance.state) ? 'waiting' : scopedAssurance.state === 'SCOPE_MISMATCH' ? 'blocked' : 'notice'}`}
                aria-live="polite"
              >
                <Icon name="shield" />
                <div>
                  <h3>
                    {scopedAssurance.state === 'IDENTITY_EVIDENCE_REQUIRED'
                      ? 'Historical identity evidence required'
                      : scopedAssurance.state === 'NEWEST_PUNCH_UNRECEIVED'
                      ? 'Terminal punch not yet received'
                      : scopedAssurance.state === 'REVIEW_REQUIRED'

                      ? `${scopedAssurance.open.toLocaleString()} certified review${scopedAssurance.open === 1 ? '' : 's'} remaining`
                      : scopedAssurance.state === 'SCOPE_MISMATCH'
                        ? 'Certified exception scope needs investigation'
                        : 'All certified exclusions reviewed'}
                  </h3>
                  <p>
                    {scopedAssurance.state === 'NEWEST_PUNCH_UNRECEIVED' ? (
                      newestPunchCopy(scopedAssurance.newest_punch_unreceived ?? 1)
                    ) : <>
                    Source evidence has advanced through checkpoint{' '}
                    {(exceptionScope.cutoff_count ?? 0).toLocaleString()}. When
                    the open count reaches zero, final assurance resumes
                    automatically without a terminal rescan.
                    </>}
                  </p>

                </div>
              </div>
            )}
            <form
              className="reconciliation-exception-filters"
              onSubmit={(event) => {
                event.preventDefault()
                setExceptionFilters(exceptionDraft)
                const params = new URLSearchParams({
                  tab: 'source-exceptions',
                })
                if (exceptionDraft.job_id)
                  params.set('job_id', exceptionDraft.job_id)
                if (exceptionDraft.device_id)
                  params.set('device_id', exceptionDraft.device_id)
                window.history.pushState(
                  null,
                  '',
                  `${window.location.pathname}?${params}`,
                )
              }}
            >
              <label>
                <span>Device</span>
                <select
                  value={exceptionDraft.device_id}
                  onChange={(event) =>
                    setExceptionDraft({
                      ...exceptionDraft,
                      job_id: '',
                      device_id: event.target.value,
                    })
                  }
                >
                  <option value="">All devices</option>
                  {devices.map((device) => (
                    <option
                      key={device.connector_id}
                      value={device.connector_id}
                    >
                      {device.display_name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Review state</span>
                <select
                  value={exceptionDraft.review_state}
                  onChange={(event) =>
                    setExceptionDraft({
                      ...exceptionDraft,
                      review_state: event.target.value,
                    })
                  }
                >
                  <option value="">Open and reviewed</option>
                  <option value="OPEN">Open</option>
                  <option value="REVIEWED">Reviewed</option>
                </select>
              </label>
              <label>
                <span>Disposition</span>
                <select
                  value={exceptionDraft.disposition}
                  onChange={(event) =>
                    setExceptionDraft({
                      ...exceptionDraft,
                      disposition: event.target.value,
                    })
                  }
                >
                  <option value="">All exceptions</option>
                  <option value="INVALID_TIME">Invalid timestamp</option>
                  <option value="MALFORMED">Malformed record</option>
                  <option value="IDENTITY_UNRESOLVED">Missing identity reference</option>
                </select>
              </label>
              <details className="reconciliation-advanced-filters">
                <summary>
                  <Icon name="grid" /> Advanced{' '}
                  <strong>
                    {Number(Boolean(exceptionDraft.error_code)) +
                      Number(Boolean(exceptionDraft.ordinal))}
                  </strong>
                </summary>
                <div>
                  <label>
                    <span>Exact error code</span>
                    <input
                      value={exceptionDraft.error_code}
                      onChange={(event) =>
                        setExceptionDraft({
                          ...exceptionDraft,
                          error_code: event.target.value
                            .toUpperCase()
                            .replace(/[^A-Z0-9_]/g, '')
                            .slice(0, 120),
                        })
                      }
                      placeholder="IMPLAUSIBLE_TERMINAL_TIME"
                    />
                  </label>
                  <label>
                    <span>Exact ordinal</span>
                    <input
                      inputMode="numeric"
                      value={exceptionDraft.ordinal}
                      onChange={(event) =>
                        setExceptionDraft({
                          ...exceptionDraft,
                          ordinal: event.target.value
                            .replace(/\D/g, '')
                            .slice(0, 10),
                        })
                      }
                    />
                  </label>
                </div>
              </details>
              <button className="button primary" type="submit">
                Apply filters
              </button>
              {activeExceptionFilters > 0 && (
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => {
                    setExceptionDraft(defaultExceptionFilters)
                    setExceptionFilters(defaultExceptionFilters)
                    window.history.pushState(
                      null,
                      '',
                      `${window.location.pathname}?tab=source-exceptions`,
                    )
                  }}
                >
                  Clear all
                </button>
              )}
            </form>
            {activeExceptionFilters > 0 && (
              <div
                className="reconciliation-filter-chips"
                aria-label="Active source exception filters"
              >
                {Object.entries(exceptionFilters)
                  .filter(([, value]) => value)
                  .map(([key, value]) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => {
                        const next = { ...exceptionFilters, [key]: '' }
                        setExceptionFilters(next)
                        setExceptionDraft(next)
                      }}
                    >
                      {key === 'job_id' ? 'Certified job' : humanize(key)}:{' '}
                      {key === 'job_id' ? value.slice(0, 8) : humanize(value)}{' '}
                      <Icon name="x" />
                    </button>
                  ))}
              </div>
            )}
            {rows.some((job) => job.recovery?.divergence) && (
              <div className="info-copy pattern-waiting">
                <Icon name="shield" />
                <div>
                  <h3>Preserved terminal-history changes</h3>
                  <p>
                    These independent observations remain attached to their
                    source epochs.
                  </p>
                  {rows
                    .filter((job) => job.recovery?.divergence)
                    .map((job) => (
                      <button
                        key={job.job_id}
                        className="button text-button"
                        onClick={() =>
                          setDivergenceDrawerId(
                            job.recovery?.divergence?.divergence_id || null,
                          )
                        }
                      >
                        {job.connector?.display_name || job.job_id} · ordinal{' '}
                        {job.recovery?.divergence?.ordinal.toLocaleString()}
                      </button>
                    ))}
                </div>
              </div>
            )}
            {exceptionError && (
              <div className="inline-retry pattern-blocked" role="alert">
                <Icon name="alert" />
                <div>
                  <strong>Latest source evidence could not be loaded</strong>
                  <p>
                    {exceptionError}
                    {exceptionRows.length
                      ? ' Existing rows remain visible.'
                      : ''}
                  </p>
                </div>
                <button
                  className="button secondary"
                  onClick={() => void loadExceptions()}
                >
                  Retry
                </button>
              </div>
            )}
            {exceptionLoading && !exceptionRows.length && (
              <div className="reconciliation-skeleton">
                <i />
                <i />
                <i />
              </div>
            )}
            <div
              className="reconciliation-exception-list"
              role="list"
              aria-label="Terminal source exceptions"
            >
              <div className="reconciliation-exception-head" aria-hidden="true">
                <span>Terminal source</span>
                <span>Exception</span>
                <span>Observed identity</span>
                <span>Assurance</span>
                <span>Review</span>
                <span />
              </div>
              {exceptionRows.map((row) => (
                <article
                  key={row.id}
                  role="listitem"
                  className={`reconciliation-exception-row pattern-${row.review_state === 'OPEN' || row.disposition === 'IDENTITY_UNRESOLVED' ? 'waiting' : 'notice'}`}
                >
                  <div data-label="Terminal source">
                    <strong>{row.display_name || row.terminal_serial}</strong>
                    <small>
                      {row.zone_id || 'Unknown zone'} · generation{' '}
                      {row.terminal_generation} · ordinal{' '}
                      {row.ordinal.toLocaleString()}
                    </small>
                  </div>
                  <div data-label="Exception">
                    <StatusBadge state={row.disposition} />
                    <small>{row.error_code || 'No error code'}</small>
                    {row.source_condition === 'TERMINAL_NEWEST_RECORD_UNSETTLED' && (
                      <small>Newest record read before the terminal wrote it; not a punch</small>
                    )}
                    {row.source_condition === 'TERMINAL_NEWEST_RECORD_UNCONFIRMED' && (
                      <small>Newest-record punch held until LIVE capture or a reconcile confirms it</small>
                    )}
                    {row.source_condition === 'TERMINAL_NEWEST_RECORD_CORRECTED' && (
                      <small>Corrected from an unwritten newest record; HR must verify the punch</small>
                    )}
                    {row.source_condition === 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER' && (
                      <small>Newest-record bytes out of time order; excluded for review</small>
                    )}
                  </div>
                  <div data-label="Observed identity">
                    <strong>Attendance UID {row.observed_uid || '—'}</strong>
                    <small>
                      User {row.observed_user_id || '—'} · timestamp{' '}
                      {row.raw_timestamp ?? 'invalid'}
                    </small>
                  </div>
                  <div data-label="Assurance">
                    <StatusBadge
                      state={row.cursor_advanced ? 'CURSOR ADVANCED' : 'HELD'}
                    />
                    <small>
                      {row.cursor_advanced
                        ? `Committed through ${row.source_committed_cursor.toLocaleString()}`
                        : 'Awaiting durable commit'}
                    </small>
                    {row.disposition === 'IDENTITY_UNRESOLVED' && <small>Oracle delivery held for identity evidence</small>}
                  </div>
                  <div data-label="Review">
                    <StatusBadge state={row.review_state} />
                    <small>
                      {row.reviewed_at
                        ? dateTime(row.reviewed_at)
                        : 'Open for review'}
                    </small>
                  </div>
                  <button
                    className="button secondary"
                    onClick={() => setExceptionDrawer(row)}
                  >
                    Inspect
                  </button>
                </article>
              ))}
              {!exceptionLoading && !exceptionRows.length && (
                <div className="empty-state">
                  <Icon name="shield" />
                  <h3>
                    {activeExceptionFilters
                      ? 'No source exceptions match these filters.'
                      : 'No source exceptions have been recorded.'}
                  </h3>
                  <p>
                    Valid terminal rows remain in the immutable attendance
                    ledger.
                  </p>
                </div>
              )}
            </div>
            {exceptionCursor && (
              <div className="load-more">
                <button
                  className="button secondary"
                  disabled={exceptionLoadingMore}
                  onClick={() =>
                    void loadExceptions({
                      cursor: exceptionCursor,
                      append: true,
                      quiet: true,
                    })
                  }
                >
                  {exceptionLoadingMore
                    ? 'Loading older exceptions…'
                    : 'Load older exceptions'}
                </button>
                <small>
                  {exceptionRows.length.toLocaleString()} of{' '}
                  {exceptionFilteredTotal.toLocaleString()} matching exceptions
                  loaded
                </small>
              </div>
            )}
          </section>
        </div>
      )}
      {jobDrawer && (
        <JobDetailDrawer
          seed={jobDrawer}
          onClose={() => setJobDrawer(null)}
          toast={toast}
          returnFocusRef={jobDrawerReturnFocusRef}
          onReviewExceptions={reviewJobExceptions}
          onDivergence={(id) => {
            setJobDrawer(null)
            setDivergenceDrawerId(id)
          }}
        />
      )}
      {exceptionDrawer && (
        <SourceExceptionDrawer
          seed={exceptionDrawer}
          onClose={() => setExceptionDrawer(null)}
          onChanged={async () => {
            await Promise.all([
              loadExceptions({ quiet: true }),
              loadJobs({ quiet: true }),
            ])
          }}
          toast={toast}
        />
      )}
      {divergenceDrawerId && (
        <ReconciliationDivergenceDrawer
          divergenceId={divergenceDrawerId}
          onClose={() => setDivergenceDrawerId(null)}
          toast={toast}
        />
      )}
    </div>
  )
}
