// Old check-ins (07-people P12): check-ins from people with no CNIC that can't
// go to the HR system until someone adds the details. Small list: filtered here.
import type { HistoricalIdentityCandidate, HistoricalIdentityReport } from '../../types'
import { EmptyState, ErrorState, FilterBar, Skeleton, StatusChip, TechnicalDetails } from '../../ui'
import { day } from '../../lib/time'
import { OLD_CHECKINS } from '../../words/people'

export const oldCandidates = (report: HistoricalIdentityReport | null) => [...(report?.rows || []), ...(report?.unassigned_groups || [])]

const rangeOf = (candidate: HistoricalIdentityCandidate) =>
  candidate.first_event_at && candidate.last_event_at ? `${day(candidate.first_event_at)} to ${day(candidate.last_event_at)}` : ''

export function OldCheckinsTab({ report, loading, error, onRetry, fixable, onFixableChange, query, onQueryChange, onAction }: {
  report: HistoricalIdentityReport | null
  loading: boolean
  error: unknown
  onRetry: () => unknown
  fixable: boolean
  onFixableChange: (value: boolean) => void
  query: string
  onQueryChange: (value: string) => void
  onAction: (candidate: HistoricalIdentityCandidate) => void
}) {
  const all = oldCandidates(report)
  const canFix = all.filter((candidate) => candidate.operator_actionable)
  const other = all.filter((candidate) => !candidate.operator_actionable)
  const needle = query.trim().toLowerCase()
  const shown = (fixable ? canFix : other)
    .filter((candidate) => !needle || `${candidate.display_name} ${candidate.user_id}`.toLowerCase().includes(needle))
    .sort((left, right) => right.event_count - left.event_count)
  const total = report?.totals.unresolved_events ?? 0
  return (
    <section className="people-tab-panel" aria-labelledby="people-old-title">
      <header className="people-tab-header">
        <h2 id="people-old-title">{OLD_CHECKINS.title}</h2>
        {total > 0 && <p>{OLD_CHECKINS.intro(total)}</p>}
        <p className="people-muted">{OLD_CHECKINS.sendLater}</p>
      </header>
      <FilterBar
        search={{ value: query, onChange: onQueryChange, label: OLD_CHECKINS.searchLabel }}
        chips={[{ id: 'yes', label: OLD_CHECKINS.canFix, count: canFix.length }, { id: 'no', label: OLD_CHECKINS.otherStep, count: other.length }]}
        chipValue={fixable ? 'yes' : 'no'}
        chipsLabel={OLD_CHECKINS.canFix}
        onChipChange={(id) => onFixableChange(id === 'yes')}
      />
      {Boolean(error) && <ErrorState title={OLD_CHECKINS.loadFailed} error={error} onRetry={onRetry} compact />}
      {loading && !report && <Skeleton variant="block" count={2} label={OLD_CHECKINS.loading} />}
      {!loading && !error && !all.length && <EmptyState icon="check" title={OLD_CHECKINS.emptyTitle}>{OLD_CHECKINS.emptyBody}</EmptyState>}
      {!loading && !error && all.length > 0 && !shown.length && <EmptyState icon="list" title={OLD_CHECKINS.noneOther} />}
      <div className="people-card-grid">
        {shown.map((candidate) => {
          const path = candidate.resolution_path
          const gone = Boolean(candidate.deleted_at) || path === 'HR_DIRECTORY_EVENT_GROUP' || candidate.source_kind === 'EVENT_GROUP'
          const button = !candidate.operator_actionable ? null
            : path === 'ACTIVE_USER_ENRICHMENT' ? OLD_CHECKINS.addCnic
              : path === 'CURRENT_IDENTITY_EVIDENCE' ? OLD_CHECKINS.confirmSame
                : path === 'IDENTITY_CONFLICT_REVIEW' ? OLD_CHECKINS.openSameCnic
                  : path === 'IDENTITY_REUSE_REVIEW' ? null
                    : OLD_CHECKINS.enterHr
          return (
            <article key={candidate.source_user_key || candidate.group_token} className="people-old-card" data-pattern={candidate.operator_actionable ? 'waiting' : 'blocked'}>
              <header>
                <div>
                  <strong data-user-text>{candidate.display_name} — Employee ID {candidate.user_id}</strong>
                  {gone && <small>{OLD_CHECKINS.noLonger}</small>}
                </div>
                <StatusChip tone={candidate.operator_actionable ? 'attention' : 'neutral'} label={OLD_CHECKINS.paths[path] || OLD_CHECKINS.pathFallback} labelPrefix={null} size="sm" code={path} />
              </header>
              <p>{OLD_CHECKINS.facts(candidate.event_count, rangeOf(candidate))}</p>
              {candidate.operator_actionable && <p className="people-muted">{OLD_CHECKINS.addHere}</p>}
              {button && <button type="button" className="button secondary" onClick={() => onAction(candidate)}>{button}</button>}
              <TechnicalDetails>{[`UID ${candidate.uid || '—'}`, candidate.row_version == null ? null : `version ${candidate.row_version}`, candidate.source_kind, path, `blocked ${candidate.blocked_count}`, `quarantined ${candidate.quarantined_count}`].filter(Boolean).join(' · ')}</TechnicalDetails>
            </article>
          )
        })}
      </div>
    </section>
  )
}
