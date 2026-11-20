// Unreadable records (08-advanced 1.5, today "Source exceptions"). Opens on
// "Not seen yet"; every row names its real problem. Filters apply at once and
// live in the URL. The national totals sit under Technical details.
import { useEffect, useState } from 'react'
import { Button, DivisionTag, EmptyState, ErrorState, FilterBar, Skeleton, StatusChip, TechnicalDetails, type ActiveFilter } from '../../ui'
import { day } from '../../lib/time'
import { formatCount, plural } from '../../words/glossary'
import { EMPLOYEE_ID } from '../../words/people'
import {
  PROBLEM_CODES,
  PROBLEM_FILTER_WORDS,
  REVIEW_FILTER_WORDS,
  newestPunchSentence,
  problemCodeLabel,
  problemName,
  type ProblemFilter,
  type ReviewFilter,
} from '../../words/recover'
import type { Device, SourceException, SourceExceptionList, SourceExceptionTotals } from '../../types'
import type { Namer, Place } from './names'

export interface UnreadableFilters {
  review: ReviewFilter
  problem: ProblemFilter | ''
  device_id: string
  code: string
  record: string
  corrected: boolean
  job_id: string
}

const REVIEW_STATE: Record<string, ReviewFilter> = { OPEN: 'open', REVIEWED: 'reviewed' }
const DISPOSITION_PROBLEM: Record<string, ProblemFilter> = { INVALID_TIME: 'time', MALFORMED: 'damaged', IDENTITY_UNRESOLVED: 'unmatched' }

/** Filters from the URL (new keys first, the old keys still read). */
export function readUnreadableFilters(params: URLSearchParams): UnreadableFilters {
  const review = params.get('review')
  const problem = params.get('problem')
  return {
    review: review === 'open' || review === 'reviewed' || review === 'all' ? review : REVIEW_STATE[params.get('review_state') || ''] || 'open',
    problem: problem === 'time' || problem === 'damaged' || problem === 'unmatched' ? problem : DISPOSITION_PROBLEM[params.get('disposition') || ''] || '',
    device_id: params.get('device_id') || '',
    code: (params.get('code') || params.get('error_code') || '').toUpperCase().replace(/[^A-Z0-9_]/g, '').slice(0, 120),
    record: (params.get('record') || params.get('ordinal') || '').replace(/\D/g, '').slice(0, 10),
    corrected: params.get('corrected') === '1',
    job_id: params.get('job_id') || '',
  }
}

export interface UnreadableRecordsProps {
  filters: UnreadableFilters
  onFilters: (changes: Record<string, string | null>, options?: { replace?: boolean }) => void
  rows: SourceException[]
  totals: SourceExceptionTotals | null
  scope: SourceExceptionList['scope'] | null
  filteredTotal: number
  hasMore: boolean
  loading: boolean
  loadingMore: boolean
  error: unknown
  devices: Device[]
  namer: Namer
  /** "From the recovery of {device} on {date}": the job's start date, when the list knows it. */
  jobStartedAt?: string | null
  onRetry: () => unknown
  onLoadMore: () => unknown
  onOpen: (row: SourceException, trigger: HTMLButtonElement) => void
}

const scopeTitle = (state: string, open: number) => {
  switch (state) {
    case 'IDENTITY_EVIDENCE_REQUIRED': return "Some check-ins aren't matched to an employee"
    case 'NEWEST_PUNCH_UNRECEIVED': return 'Latest check-in not received yet'
    case 'REVIEW_REQUIRED': return `${plural(open, 'record')} to look at`
    case 'INTERPRETATION_REQUIRED': return "Some records can't be read yet"
    case 'SCOPE_MISMATCH': return "The numbers don't add up. Ask IT."
    default: return 'All records looked at'
  }
}

export function UnreadableRecords(props: UnreadableRecordsProps) {
  const { filters, onFilters, rows, totals, scope, filteredTotal, hasMore, loading, loadingMore, error, devices, namer, jobStartedAt, onRetry, onLoadMore, onOpen } = props
  const [record, setRecord] = useState(filters.record)
  useEffect(() => setRecord(filters.record), [filters.record])
  useEffect(() => {
    if (record === filters.record) return
    const timer = window.setTimeout(() => onFilters({ record: record || null, ordinal: null }, { replace: true }), 250)
    return () => window.clearTimeout(timer)
  }, [filters.record, onFilters, record])

  const scoped = scope?.source_exception_assurance || null
  const jobDevice = scope?.device_id ? devices.find((device) => device.connector_id === scope.device_id) : null
  const jobPlace: Place | null = scope ? namer(jobDevice || { connector_id: scope.device_id, display_name: rows[0]?.display_name, zone_id: rows[0]?.zone_id }) : null
  const deviceFilter = filters.device_id ? devices.find((device) => device.connector_id === filters.device_id) : null
  const active: ActiveFilter[] = []
  if (filters.job_id) active.push({ id: 'job', label: `From the recovery${jobStartedAt ? ` of ${day(jobStartedAt)}` : ''}`, onRemove: () => onFilters({ job_id: null }) })
  if (filters.device_id) active.push({ id: 'device', label: `Device: ${deviceFilter ? namer(deviceFilter).name : 'chosen device'}`, onRemove: () => onFilters({ device_id: null, job_id: null }) })
  if (filters.review !== 'open') active.push({ id: 'review', label: REVIEW_FILTER_WORDS[filters.review], onRemove: () => onFilters({ review: null, review_state: null }) })
  if (filters.problem) active.push({ id: 'problem', label: `Problem: ${PROBLEM_FILTER_WORDS[filters.problem]}`, onRemove: () => onFilters({ problem: null, disposition: null }) })
  if (filters.code) active.push({ id: 'code', label: `Problem: ${problemCodeLabel(filters.code)}`, onRemove: () => onFilters({ code: null, error_code: null }) })
  if (filters.record) active.push({ id: 'record', label: `Record ${formatCount(Number(filters.record))}`, onRemove: () => { setRecord(''); onFilters({ record: null, ordinal: null }) } })
  if (filters.corrected) active.push({ id: 'corrected', label: 'Time corrected', onRemove: () => onFilters({ corrected: null }) })
  const anyFilter = active.length > 0
  const clearAll = () => { setRecord(''); onFilters({ review: null, review_state: null, problem: null, disposition: null, device_id: null, code: null, error_code: null, record: null, ordinal: null, corrected: null, job_id: null }) }
  const moreCount = Number(Boolean(filters.device_id)) + Number(Boolean(filters.code)) + Number(filters.corrected)

  return (
    <section className="recover-unreadable" aria-labelledby="recover-unreadable-title">
      <div className="recover-section-head">
        <h2 id="recover-unreadable-title">Unreadable records</h2>
        <p>Records the machine saved that were not sent to the HR system (Oracle). Most need nothing from you. Records that are not matched to an employee are sent only after the employee is fixed.</p>
      </div>
      {scope && jobPlace && (
        <div className="recover-scope-chip">
          <span className="recover-chip-pill">
            {`From the recovery of ${jobPlace.name}${jobStartedAt ? ` on ${day(jobStartedAt)}` : ''}`}
            <button type="button" className="recover-chip-x" aria-label="Show records from every recovery" onClick={() => onFilters({ job_id: null })}>✕</button>
          </span>
          {scoped && (
            <div className="recover-scope-state" aria-live="polite">
              <strong>{scopeTitle(scoped.state, scoped.open)}</strong>
              <p>{scoped.state === 'NEWEST_PUNCH_UNRECEIVED' ? newestPunchSentence() : 'When every record here is marked as seen, the recovery finishes by itself. No need to read the machine again.'}</p>
            </div>
          )}
        </div>
      )}
      <FilterBar
        search={{ value: record, onChange: (value) => setRecord(value.replace(/\D/g, '').slice(0, 10)), label: 'Search record number', placeholder: 'Search record number' }}
        chips={(['open', 'reviewed', 'all'] as const).map((id) => ({ id, label: REVIEW_FILTER_WORDS[id] }))}
        chipValue={filters.review}
        chipsLabel="Seen or not"
        onChipChange={(id) => onFilters({ review: id === 'open' ? null : id, review_state: null })}
        actions={(
          <div className="recover-problem-chips" role="group" aria-label="Problem">
            <span className="recover-muted">Problem:</span>
            {(['time', 'damaged', 'unmatched'] as const).map((id) => (
              <button key={id} type="button" className="recover-toggle-chip" aria-pressed={filters.problem === id} onClick={() => onFilters({ problem: filters.problem === id ? null : id, disposition: null })}>
                {PROBLEM_FILTER_WORDS[id]}
              </button>
            ))}
          </div>
        )}
        moreFilters={(
          <div className="recover-more-filters">
            <label>
              <span>Device</span>
              <select value={filters.device_id} onChange={(event) => onFilters({ device_id: event.target.value || null, job_id: null })}>
                <option value="">All devices</option>
                {devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{namer(device).name}</option>)}
              </select>
            </label>
            <label>
              <span>Problem code</span>
              <select value={filters.code} onChange={(event) => onFilters({ code: event.target.value || null, error_code: null })}>
                <option value="">Any problem</option>
                {PROBLEM_CODES.map((item) => <option key={item.code} value={item.code}>{item.label}</option>)}
              </select>
            </label>
            <label className="recover-switch">
              <input type="checkbox" checked={filters.corrected} onChange={(event) => onFilters({ corrected: event.target.checked ? '1' : null })} />
              <span>Time corrected</span>
            </label>
          </div>
        )}
        moreFiltersCount={moreCount}
        active={active}
        onClearAll={clearAll}
        result={{ shown: rows.length, total: filteredTotal }}
      />
      {Boolean(error) && <ErrorState compact title="Couldn't refresh the list." message={rows.length ? 'Showing the last list we had.' : undefined} error={error} onRetry={onRetry} />}
      {loading && !rows.length && <Skeleton variant="row" count={3} label="Loading unreadable records" />}
      <div className="recover-records" role="list" aria-label="Unreadable records" data-loading={loading || undefined}>
        <div className="recover-records-head" aria-hidden="true">
          <span>Device</span><span>Problem</span><span>Person</span><span>Effect</span><span>Status</span><span />
        </div>
        {rows.map((row) => {
          const place = namer({ connector_id: row.connector_id, display_name: row.display_name, zone_id: row.zone_id })
          const seen = row.review_state === 'REVIEWED'
          return (
            <article key={row.id} role="listitem" className="recover-record-row" data-seen={seen ? '' : undefined}>
              <div className="recover-record-device">
                <strong>{place.name}</strong> <DivisionTag division={place.division} />
              </div>
              <div className="recover-record-problem">
                <span>{problemName(row.disposition, row.source_condition)}</span>
                <small>{`Record ${row.ordinal.toLocaleString()}`}</small>
              </div>
              <div className="recover-record-person">
                <span>{row.observed_user_id ? `${EMPLOYEE_ID} ${row.observed_user_id}` : `${EMPLOYEE_ID} not known`}</span>
                {row.disposition === 'INVALID_TIME' && <small>Time: not readable</small>}
              </div>
              <div className="recover-record-effect">
                <span>{row.cursor_advanced ? 'Other check-ins not affected' : 'Waiting'}</span>
                {row.disposition === 'IDENTITY_UNRESOLVED' && <small>Not sent: not matched to an employee</small>}
              </div>
              <div className="recover-record-status">
                <StatusChip size="sm" tone={seen ? 'working' : 'attention'} label={seen ? `Seen${row.reviewed_at ? ` ${day(row.reviewed_at)}` : ''}` : 'Not seen yet'} code={row.review_state} />
              </div>
              <Button size="sm" aria-label={`View record ${row.ordinal.toLocaleString()}, ${place.name}`} onClick={(event) => onOpen(row, event.currentTarget)}>View</Button>
            </article>
          )
        })}
        {!loading && !rows.length && !error && (
          anyFilter
            ? <EmptyState icon="search" title="No records match your filters." action={<Button onClick={clearAll}>Clear all</Button>} />
            : totals && totals.all === 0
              ? <EmptyState icon="check" title="No unreadable records.">All other check-ins were saved normally.</EmptyState>
              : <EmptyState icon="check" title="Nothing waiting.">Every set-aside record has been seen.</EmptyState>
        )}
      </div>
      {hasMore && (
        <div className="recover-more">
          <Button busy={loadingMore} busyLabel="Loading…" onClick={() => onLoadMore()}>Show older records</Button>
          <small>{`Showing ${formatCount(rows.length)} of ${formatCount(filteredTotal)}`}</small>
        </div>
      )}
      {totals && (
        <TechnicalDetails summary="Technical details: all devices, all time">
          <dl className="recover-tech">
            <div><dt>All records</dt><dd>{formatCount(totals.all)}</dd></div>
            <div><dt>Not seen yet</dt><dd>{formatCount(totals.open)}</dd></div>
            <div><dt>Impossible time</dt><dd>{formatCount(totals.invalid_time)}</dd></div>
            <div><dt>Damaged record</dt><dd>{formatCount(totals.malformed)}</dd></div>
            <div><dt>Machines affected</dt><dd>{formatCount(totals.affected_terminals)}</dd></div>
            <div><dt>Not matched to an employee</dt><dd>{totals.identity_unresolved != null ? formatCount(totals.identity_unresolved) : 'Unknown'}</dd></div>
            {scoped && <>
              <div><dt>Records in this recovery</dt><dd>{formatCount(scoped.total)}</dd></div>
              <div><dt>Seen</dt><dd>{formatCount(scoped.reviewed)}</dd></div>
              <div><dt>Still to look at</dt><dd>{formatCount(scoped.open)}</dd></div>
              <div><dt>State</dt><dd>{scoped.state} · {scope?.terminal_serial || 'terminal pending'} · cutoff {(scope?.cutoff_count ?? 0).toLocaleString()}</dd></div>
            </>}
          </dl>
        </TechnicalDetails>
      )}
    </section>
  )
}
