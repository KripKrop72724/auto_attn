// Same CNIC (07-people P11): groups of people on one machine with the same
// CNIC. Needs a decision first, then most check-ins. Small list: filtered here.
import { useEffect, useRef } from 'react'
import type { IdentityConflictGroup, IdentityConflictReport } from '../../types'
import { EmptyState, ErrorState, Skeleton, StatusChip, TechnicalDetails } from '../../ui'
import { day } from '../../lib/time'
import { personInitials, SAME_CNIC } from '../../words/people'

const checkins = (group: IdentityConflictGroup) => group.members.reduce((sum, member) => sum + (member.punch_evidence?.captured_count ?? 0), 0)

export function SameCnicTab({ report, loading, error, onRetry, decision, onDecisionChange, highlightUserKey, onConfirm, onUndo, onEditMember }: {
  report: IdentityConflictReport | null
  loading: boolean
  error: unknown
  onRetry: () => unknown
  decision: '' | 'done'
  onDecisionChange: (value: '' | 'done') => void
  highlightUserKey?: string
  onConfirm: (group: IdentityConflictGroup) => void
  onUndo: (group: IdentityConflictGroup) => void
  onEditMember: (userKey: string) => void
}) {
  const highlightRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (highlightUserKey && typeof highlightRef.current?.scrollIntoView === 'function') highlightRef.current.scrollIntoView({ block: 'center' })
  }, [highlightUserKey, report])
  const groups = report?.groups ?? []
  const open = groups.filter((group) => group.status === 'UNRESOLVED')
  const done = groups.filter((group) => group.status !== 'UNRESOLVED')
  const shown = (decision === 'done' ? done : open).slice().sort((left, right) => checkins(right) - checkins(left))
  const people = Math.max(2, ...open.map((group) => group.members.length))
  return (
    <section className="people-tab-panel" aria-labelledby="people-same-cnic-title">
      <header className="people-tab-header">
        <h2 id="people-same-cnic-title">{SAME_CNIC.title(people)}</h2>
        <p>{SAME_CNIC.intro}</p>
        <div className="ui-filter-chips" role="group" aria-label={SAME_CNIC.needsDecision}>
          <button type="button" className="ui-filter-chip" aria-pressed={decision !== 'done'} onClick={() => onDecisionChange('')}><span>{SAME_CNIC.needsDecision}</span> <span className="ui-filter-chip-count">{open.length}</span></button>
          <button type="button" className="ui-filter-chip" aria-pressed={decision === 'done'} onClick={() => onDecisionChange('done')}><span>{SAME_CNIC.confirmedChip}</span> <span className="ui-filter-chip-count">{done.length}</span></button>
        </div>
      </header>
      {Boolean(error) && <ErrorState title={SAME_CNIC.loadFailed} error={error} onRetry={onRetry} compact />}
      {loading && !report && <Skeleton variant="block" count={2} label={SAME_CNIC.loading} />}
      {!loading && !error && !shown.length && (decision === 'done'
        ? <EmptyState icon="check" title={SAME_CNIC.noneConfirmed} />
        : <EmptyState icon="check" title={SAME_CNIC.emptyTitle}>{SAME_CNIC.emptyBody}</EmptyState>)}
      <div className="people-card-grid">
        {shown.map((group) => {
          const unresolved = group.status === 'UNRESOLVED'
          const highlighted = Boolean(highlightUserKey && group.members.some((member) => member.user_key === highlightUserKey))
          const editTarget = group.members[group.members.length - 1]
          return (
            <article key={group.group_token} ref={highlighted ? highlightRef : undefined} className={`people-cnic-card ${unresolved ? 'is-open' : 'is-done'} ${highlighted ? 'is-highlighted' : ''}`} data-pattern={unresolved ? 'blocked' : 'confirmed'}>
              <header>
                <div><strong>{SAME_CNIC.cnic} {group.cnic_masked}</strong><small>{SAME_CNIC.classification[group.classification] || SAME_CNIC.classification.MIXED_NAMES_HIGH_RISK}</small></div>
                <StatusChip tone={unresolved ? 'attention' : 'working'} label={unresolved ? SAME_CNIC.needsDecision : SAME_CNIC.confirmed} labelPrefix={null} size="sm" code={group.status} />
              </header>
              <ul className="people-cnic-members">
                {group.members.map((member) => {
                  const evidence = member.punch_evidence
                  return (
                    <li key={member.user_key}>
                      <span className="avatar" aria-hidden="true">{personInitials(member.display_name)}</span>
                      <span>
                        <strong data-user-text>{SAME_CNIC.member(member.display_name, member.user_id)}</strong>
                        <small>{evidence?.captured_count ? `${SAME_CNIC.checkins(evidence.captured_count)}${evidence.last_captured_at ? `, ${SAME_CNIC.last(day(evidence.last_captured_at))}` : ''}` : SAME_CNIC.noCheckins}</small>
                      </span>
                    </li>
                  )
                })}
              </ul>
              <p>{unresolved ? SAME_CNIC.advice[group.recommended_action] || SAME_CNIC.adviceFallback : group.resolution_reason}</p>
              <footer>
                {unresolved
                  ? <>
                      <button type="button" className="button secondary" onClick={() => onConfirm(group)}>{SAME_CNIC.yes}</button>
                      {editTarget && <button type="button" className="button secondary" onClick={() => onEditMember(editTarget.user_key)}>{SAME_CNIC.editMember(editTarget.user_id)}</button>}
                    </>
                  : <button type="button" className="button secondary" onClick={() => onUndo(group)}>{SAME_CNIC.undo}</button>}
              </footer>
              <TechnicalDetails>{[group.classification, group.recommended_action, group.status, ...group.members.map((member) => `UID ${member.uid}`)].join(' · ')}</TechnicalDetails>
            </article>
          )
        })}
      </div>
    </section>
  )
}
