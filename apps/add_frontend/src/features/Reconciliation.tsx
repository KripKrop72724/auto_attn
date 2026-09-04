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
  type ReconciliationDialogState,
} from '../App'
import { Icon } from '../Icon'
import { AnchoredLayer } from '../AnchoredLayer'
import type {
  Device,
  ReconciliationDivergenceDetail,
  ReconciliationDivergenceReveal,
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

function WorkspaceTabs({
  section,
  activeJobs,
  openExceptions,
  onChange,
}: {
  section: ReconciliationSection
  activeJobs: number
  openExceptions: number
  onChange: (section: ReconciliationSection) => void
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const tabs: Array<{
    id: ReconciliationSection
    label: string
    count: number
  }> = [
    { id: 'jobs', label: 'Jobs', count: activeJobs },
    { id: 'exceptions', label: 'Source exceptions', count: openExceptions },
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
      className="reconciliation-workspace-tabs"
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
      className={`reconciliation-terminal-picker panel ${open ? 'is-open' : ''}`}
      aria-label="Terminal selection"
    >
      <div className="reconciliation-terminal-current">
        <span className="reconciliation-terminal-symbol">
          <Icon name="server" />
        </span>
        <div>
          <small>Terminal to reconcile</small>
          <strong>
            {selected?.display_name || 'Choose an authorized terminal'}
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
        <AnchoredLayer anchorRef={triggerRef} className="reconciliation-terminal-layer" matchAnchor mobileSheet preferredWidth={760} onDismiss={(reason) => close(reason === 'escape')}>
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
                <span className="reconciliation-terminal-symbol">
                  <Icon name="server" />
                </span>
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
                <Icon name="chevron" />
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

function ReconciliationCancelMenu({ onCancel }: { onCancel: () => void }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  return (
    <div className="reconciliation-action-menu">
      <button ref={triggerRef} className="button secondary" type="button" aria-label="More reconciliation actions" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((value) => !value)}><Icon name="menu" /> More</button>
      {open && <AnchoredLayer anchorRef={triggerRef} className="reconciliation-action-layer" mobileSheet preferredWidth={310} onDismiss={(reason) => { setOpen(false); if (reason === 'escape') triggerRef.current?.focus() }}>
        <div className="reconciliation-action-panel" role="group" aria-label="Reconciliation actions">
          <button type="button" className="danger-action" onClick={() => { setOpen(false); onCancel() }}><Icon name="x" /><span><strong>Cancel reconciliation</strong><small>Committed checkpoints and evidence remain preserved.</small></span></button>
        </div>
      </AnchoredLayer>}
    </div>
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
                <dd>{job.recovery?.source_epoch ?? 1}</dd>
              </div>
              <div>
                <dt>Committed next ordinal</dt>
                <dd>{job.checkpoint?.next_ordinal?.toLocaleString() ?? '—'}</dd>
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
                className={`info-copy pattern-${job.source_exception_assurance.state === 'REVIEW_REQUIRED' ? 'waiting' : job.source_exception_assurance.state === 'SCOPE_MISMATCH' ? 'blocked' : 'notice'}`}
              >
                <Icon name="shield" />
                <div>
                  <h3>
                    {job.source_exception_assurance.state === 'REVIEW_REQUIRED'
                      ? `${job.source_exception_assurance.open.toLocaleString()} source review${job.source_exception_assurance.open === 1 ? '' : 's'} remaining`
                      : job.source_exception_assurance.state === 'SCOPE_MISMATCH'
                        ? 'Source-exception evidence mismatch'
                        : 'Reviewed exclusions are preserved'}
                  </h3>
                  <p>
                    {job.source_exception_assurance.reviewed.toLocaleString()} of{' '}
                    {job.source_exception_assurance.total.toLocaleString()} certified
                    source exceptions reviewed. They remain immutable and excluded
                    from attendance and Oracle.
                  </p>
                  {job.source_exception_assurance.state === 'REVIEW_REQUIRED' && (
                    <button
                      className="button secondary"
                      onClick={() => onReviewExceptions(job)}
                    >
                      Review certified exceptions
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
          'Source review recorded. When the certified cohort is complete, assurance resumes automatically from its existing checkpoint.',
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
                <h3>Excluded from attendance and Oracle</h3>
                <p>
                  ADD preserved this terminal ordinal as immutable evidence.
                  Review never creates, edits, or deletes attendance. Completing
                  every review in a certified job automatically resumes assurance
                  for its valid records.
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
                <dd>{row.error_code || row.disposition}</dd>
              </div>
              <div>
                <dt>Observed identity</dt>
                <dd>
                  UID {row.observed_uid || '—'} · User{' '}
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
                    Review accepts this fail-closed exclusion; the preserved source
                    record itself never changes.
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
  const [dialog, setDialog] = useState<ReconciliationDialogState>(null)
  const [jobDrawer, setJobDrawer] = useState<ReconciliationJob | null>(null)
  const jobDrawerReturnFocusRef = useRef<HTMLButtonElement>(null)
  const [divergenceDrawerId, setDivergenceDrawerId] = useState<string | null>(
    null,
  )
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
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
    }: { cursor?: number; append?: boolean; quiet?: boolean } = {}) => {
      if (append) setJobLoadingMore(true)
      else if (!quiet) setJobLoading(true)
      setJobError('')
      jobAbortRef.current?.abort()
      const controller = new AbortController()
      jobAbortRef.current = controller
      try {
        const response = await api<ReconciliationListResponse>(
          `/api/v1/reconciliations${queryString({ q: jobQuery.trim() || undefined, zone_id: jobZone || undefined, status_group: jobStatus || undefined, cursor, limit: 40 })}`,
          { signal: controller.signal },
        )
        setRows((current) =>
          append
            ? [
                ...new Map(
                  [...current, ...response.rows].map((row) => [
                    row.job_id,
                    row,
                  ]),
                ).values(),
              ]
            : response.rows,
        )
        setEnabled(response.enabled)
        setScheduler(response.scheduler || emptyScheduler)
        setJobTotals(response.totals || emptyJobTotals)
        setJobCursor(response.next_cursor)
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
  useEffect(() => {
    if (section === 'jobs') void loadJobs({ quiet: true })
  }, [revision]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    void loadExceptions()
  }, [loadExceptions])
  useEffect(() => {
    if (section === 'exceptions') void loadExceptions({ quiet: true })
  }, [revision]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(
    () => () => {
      jobAbortRef.current?.abort()
      exceptionAbortRef.current?.abort()
    },
    [],
  )
  useEffect(() => {
    setDeviceDetail(null)
    setPreflight(null)
    setPreflightError('')
    if (!selectedId) return
    const controller = new AbortController()
    setPreflightLoading(true)
    Promise.allSettled([
      api<Device>(`/api/v1/devices/${selectedId}`, {
        signal: controller.signal,
      }),
      api<ReconciliationPreflight>(
        `/api/v1/devices/${selectedId}/reconciliations/preflight`,
        { signal: controller.signal },
      ),
    ])
      .then(([detail, check]) => {
        if (controller.signal.aborted) return
        if (detail.status === 'fulfilled') setDeviceDetail(detail.value)
        if (check.status === 'fulfilled') setPreflight(check.value)
        else
          setPreflightError(
            check.reason instanceof Error
              ? check.reason.message
              : 'Preflight could not be loaded.',
          )
      })
      .finally(() => {
        if (!controller.signal.aborted) setPreflightLoading(false)
      })
    return () => controller.abort()
  }, [selectedId, revision])

  const closeDialog = () => {
    setDialog(null)
    setReason('')
    setConfirmation('')
    setPassword('')
  }
  const start = async () => {
    if (!selected) return
    setBusy(true)
    try {
      await api(
        `/api/v1/devices/${selected.connector_id}/reconciliations/full-history`,
        {
          method: 'POST',
          body: JSON.stringify({
            reason: reason.trim(),
            confirmation,
            password,
            idempotency_key: idempotency('full-history'),
          }),
        },
      )
      closeDialog()
      toast.notice(
        'Durable start-of-time reconciliation queued. ADD now owns its checkpoint.',
      )
      await loadJobs({ quiet: true })
    } catch (error) {
      setPassword('')
      toast.error(
        error instanceof Error
          ? error.message
          : 'Could not start reconciliation.',
      )
    } finally {
      setBusy(false)
    }
  }
  const control = async (
    job: ReconciliationJob,
    action: 'pause' | 'resume' | 'cancel' | 'retry',
  ) => {
    setBusy(true)
    try {
      await api(`/api/v1/reconciliations/${job.job_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({
          reason: reason.trim(),
          password,
          idempotency_key: idempotency(`reconcile-${action}`),
        }),
      })
      closeDialog()
      toast.notice(`Reconciliation ${action} recorded in the audit trail.`)
      await loadJobs({ quiet: true })
    } catch (error) {
      setPassword('')
      toast.error(
        error instanceof Error
          ? error.message
          : 'Reconciliation control failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  const canOpenStart = Boolean(selected && enabled && preflight?.eligible)
  const canStart = Boolean(
    canOpenStart &&
      reason.trim().length >= 10 &&
      password &&
      confirmation === `RECONCILE ${selected?.device_id} FROM START`,
  )
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
        eyebrow="TERMINAL TRUTH & RECOVERY"
        title="Terminal truth & recovery"
        description="Recover complete terminal history through bounded, restart-safe capture and separately prove append-only Oracle membership. Every committed checkpoint and exception remains immutable."
        action={
          <div className="page-actions">
            <button
              className="button secondary"
              onClick={() =>
                void (section === 'jobs'
                  ? loadJobs({ quiet: true })
                  : loadExceptions({ quiet: true }))
              }
            >
              <Icon name="refresh" /> Refresh
            </button>
            {section === 'jobs' && (
              <button
                className="button primary"
                disabled={!canOpenStart}
                title={
                  !canOpenStart
                    ? preflight?.hard_blockers[0]?.message ||
                      'Select an eligible terminal first.'
                    : undefined
                }
                onClick={() => setDialog({ mode: 'start' })}
              >
                <Icon name="plus" />{' '}
                {preflight?.ready_now
                  ? 'Start complete reconcile'
                  : preflight?.eligible
                    ? 'Queue when safe'
                    : 'New complete reconcile'}
              </button>
            )}
          </div>
        }
      />
      <WorkspaceTabs
        section={section}
        activeJobs={activeJobCount}
        openExceptions={exceptionTotals.open}
        onChange={setWorkspaceSection}
      />
      {section === 'jobs' ? (
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
                  <p className="eyebrow">SELECTED TERMINAL CONTEXT</p>
                  <h2>{selected.display_name}</h2>
                  <p>
                    {selected.zone_name} · connector {selected.connector_id} ·
                    device {selected.device_id}
                  </p>
                </div>
                <StatusBadge
                  state={
                    preflightLoading
                      ? 'CHECKING'
                      : preflight?.ready_now
                        ? 'READY'
                        : preflight?.eligible
                          ? 'WAITING FOR SAFE WINDOW'
                          : 'BLOCKED'
                  }
                />
              </div>
              {preflightLoading && (
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
                      <strong>ZKT terminal</strong>
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
                        preflight.terminal?.range_resume_verified
                          ? 'check'
                          : 'alert'
                      }
                    />
                    <span>
                      <strong>Range-resume firmware</strong>
                      <small>
                        {preflight.terminal?.range_resume_verified
                          ? 'Verified for restart-safe reads'
                          : 'Not verified'}
                      </small>
                    </span>
                  </article>
                  <article>
                    <Icon
                      name={selected.zkt?.snapshot_complete ? 'check' : 'alert'}
                    />
                    <span>
                      <strong>Identity snapshot</strong>
                      <small>
                        {selected.zkt?.snapshot_complete
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
                <h2>Durable reconciliation ledger</h2>
                <p>
                  Attention-first operational history with independent capture
                  and Oracle assurance progress.
                </p>
              </div>
              <StatusBadge
                state={`${jobFilteredTotal.toLocaleString()} MATCHING`}
              />
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
                <span>Status</span>
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
                <span>Zone</span>
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
                  Clear {activeJobFilters}
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
            <div className="reconciliation-job-list" aria-live="polite">
              {rows.map((job) => {
                const cutoff = job.terminal.cutoff_count || 0
                const capturePercent = cutoff
                  ? Math.min(
                      100,
                      Math.round((job.progress.scanned / cutoff) * 100),
                    )
                  : job.capture_certified_at
                    ? 100
                    : 0
                const oracleTarget = job.progress.oracle_target || 0
                const oraclePercent = oracleTarget
                  ? Math.min(
                      100,
                      Math.round(
                        (job.progress.oracle_confirmed / oracleTarget) * 100,
                      ),
                    )
                  : job.oracle_certified_at
                    ? 100
                    : 0
                const queuePosition = queuePositions.get(job.job_id)
                const sourceAssurance =
                  job.source_exception_assurance || emptySourceAssurance
                const sourceGateActive =
                  sourceAssurance.state === 'REVIEW_REQUIRED' ||
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
                          <strong>Terminal capture</strong>
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
                          review
                        </p>
                      </section>
                    </div>
                    <dl className="reconciliation-job-facts">
                      <div>
                        <dt>Checkpoint</dt>
                        <dd>
                          {job.checkpoint?.next_ordinal?.toLocaleString() ??
                            job.progress.scanned.toLocaleString()}
                        </dd>
                      </div>
                      <div>
                        <dt>Source epoch</dt>
                        <dd>{job.recovery?.source_epoch ?? 1}</dd>
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
                        className={`reconciliation-source-assurance pattern-${sourceAssurance.state === 'REVIEW_REQUIRED' ? 'waiting' : sourceAssurance.state === 'SCOPE_MISMATCH' ? 'blocked' : 'notice'}`}
                      >
                        <Icon name="shield" />
                        <span>
                          <strong>
                            {sourceAssurance.state === 'REVIEW_REQUIRED'
                              ? `${sourceAssurance.open.toLocaleString()} source review${sourceAssurance.open === 1 ? '' : 's'} remaining`
                              : sourceAssurance.state === 'SCOPE_MISMATCH'
                                ? 'Source-exception evidence mismatch'
                                : 'Reviewed exclusions — assurance continuing'}
                          </strong>
                          {sourceAssurance.reviewed.toLocaleString()} of{' '}
                          {sourceAssurance.total.toLocaleString()} certified
                          exceptions reviewed; all remain excluded fail-closed.
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
                      {directAction && (
                        <button
                          className="button primary"
                          onClick={() =>
                            setDialog({
                              mode: 'control',
                              job,
                              action: directAction,
                            })
                          }
                        >
                          {humanize(directAction)}
                        </button>
                      )}
                      {controls.includes('cancel') && (
                        <ReconciliationCancelMenu onCancel={() => setDialog({ mode: 'control', job, action: 'cancel' })} />
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
                      ? 'No reconciliation jobs match these filters.'
                      : 'No complete reconciliation has been requested.'}
                  </h3>
                  <p>
                    {activeJobFilters
                      ? 'Clear one or more filters to broaden the durable ledger.'
                      : 'Select an eligible terminal, review preflight, and create the first source-coverage job.'}
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
              label={scopedAssurance ? 'Reviewed exclusions' : 'Invalid timestamps'}
              value={(
                scopedAssurance?.reviewed ?? exceptionTotals.invalid_time
              ).toLocaleString()}
              detail={
                scopedAssurance
                  ? 'Preserved and excluded fail-closed'
                  : 'Excluded fail-closed'
              }
              icon={scopedAssurance ? 'shield' : 'clock'}
              tone={scopedAssurance ? 'positive' : 'warning'}
            />
            <Metric
              label={scopedAssurance ? 'Open reviews' : 'Malformed rows'}
              value={(
                scopedAssurance?.open ?? exceptionTotals.malformed
              ).toLocaleString()}
              detail={
                scopedAssurance
                  ? 'Automatic continuation when zero'
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
          </section>
          <section className="panel source-exceptions-panel">
            <div className="panel-header">
              <div>
                <h2>Immutable source exception ledger</h2>
                <p>
                  Review never changes a preserved record or creates attendance.
                  Once every exception in a certified job is reviewed, ADD
                  automatically continues assurance for its valid records.
                </p>
              </div>
              <StatusBadge
                state={`${exceptionFilteredTotal.toLocaleString()} MATCHING`}
              />
            </div>
            {exceptionScope && scopedAssurance && (
              <div
                className={`info-copy pattern-${scopedAssurance.state === 'REVIEW_REQUIRED' ? 'waiting' : scopedAssurance.state === 'SCOPE_MISMATCH' ? 'blocked' : 'notice'}`}
                aria-live="polite"
              >
                <Icon name="shield" />
                <div>
                  <h3>
                    {scopedAssurance.state === 'REVIEW_REQUIRED'
                      ? `${scopedAssurance.open.toLocaleString()} certified review${scopedAssurance.open === 1 ? '' : 's'} remaining`
                      : scopedAssurance.state === 'SCOPE_MISMATCH'
                        ? 'Certified exception scope needs investigation'
                        : 'All certified exclusions reviewed'}
                  </h3>
                  <p>
                    Valid rows have already advanced through checkpoint{' '}
                    {(exceptionScope.cutoff_count ?? 0).toLocaleString()}. When
                    the open count reaches zero, final assurance resumes
                    automatically without a terminal rescan.
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
                  className={`reconciliation-exception-row pattern-${row.review_state === 'OPEN' ? 'waiting' : 'notice'}`}
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
                  </div>
                  <div data-label="Observed identity">
                    <strong>UID {row.observed_uid || '—'}</strong>
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
      {dialog && (
        <Dialog
          titleId="reconciliation-dialog-title"
          title={
            dialog.mode === 'start'
              ? preflight?.ready_now
                ? 'Start complete terminal reconciliation'
                : 'Queue reconciliation when safe'
              : `${humanize(dialog.action)} reconciliation`
          }
          description={
            dialog.mode === 'start'
              ? selected?.display_name
              : dialog.job.connector?.display_name
          }
          onClose={closeDialog}
        >
          <div className="dialog-body">
            <div
              className={`info-copy pattern-${dialog.mode === 'start' || dialog.action === 'cancel' ? 'blocked' : 'waiting'}`}
            >
              <Icon name="shield" />
              <div>
                <h3>
                  {dialog.mode === 'start'
                    ? `ADD runs up to ${scheduler.device_concurrency} isolated terminal scans in parallel.`
                    : 'ADD preserves every committed checkpoint and evidence record.'}
                </h3>
                <p>
                  Live punches retain priority. Cancellation stops future work
                  without deleting captured source evidence or Oracle membership
                  proof.
                </p>
              </div>
            </div>
            <label>
              Audited reason
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={500}
                placeholder="At least 10 characters"
              />
            </label>
            {dialog.mode === 'start' && selected && (
              <label>
                Type <code>{`RECONCILE ${selected.device_id} FROM START`}</code>
                <input
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  autoComplete="off"
                />
              </label>
            )}
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
              <button className="button secondary" onClick={closeDialog}>
                Keep current state
              </button>
              {dialog.mode === 'start' ? (
                <button
                  className="button primary"
                  disabled={!canStart || busy}
                  onClick={() => void start()}
                >
                  {busy
                    ? 'Verifying…'
                    : preflight?.ready_now
                      ? 'Start durable reconcile'
                      : 'Queue when safe'}
                </button>
              ) : (
                <button
                  className={
                    dialog.action === 'cancel'
                      ? 'button destructive'
                      : 'button primary'
                  }
                  disabled={reason.trim().length < 10 || !password || busy}
                  onClick={() => void control(dialog.job, dialog.action)}
                >
                  {busy ? 'Verifying…' : `Confirm ${dialog.action}`}
                </button>
              )}
            </div>
          </div>
        </Dialog>
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
