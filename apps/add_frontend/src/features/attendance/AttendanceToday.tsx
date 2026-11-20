// The day's totals (ATT-8): five tiles on desktop, a "Today" card and status
// chips on phones. Every number comes from the server summary; while it loads
// the tiles shimmer and never show 0. A tile or chip sets the status filter.
import type { ReactNode } from 'react'
import { Icon } from '../../Icon'
import { StatTile, useIsPhone } from '../../ui'
import { clock, ago } from '../../lib/time'
import type { DivisionChoice } from '../../lib/prefs'
import type { DeliveryGroup } from '../../words/attendance'
import { GROUP_CHIP } from './attendanceWords'
import type { SummaryState } from './useAttendanceSummary'
import type { GroupTotals } from './types'

const fmt = (value: number) => value.toLocaleString('en-PK')
const OLD_WAIT_MS = 15 * 60_000

export function splitLine(summary: SummaryState['data'], key: keyof Omit<GroupTotals, 'oldest_waiting_at'> = 'received') {
  if (!summary) return ''
  return `Life ${fmt(summary.by_division.life[key])} · Health ${fmt(summary.by_division.health[key])}`
}

export function AttendanceToday({ state, division, chosen, onChoose, rangeLabel, now = Date.now() }: {
  state: SummaryState
  division: DivisionChoice
  chosen: DeliveryGroup[]
  onChoose: (group: DeliveryGroup | null) => void
  rangeLabel: string
  now?: number
}) {
  const phone = useIsPhone()
  const { data, loading, failed, tooLong } = state
  const totals = data?.totals
  const pending = loading && !data
  const value = (key: keyof Omit<GroupTotals, 'oldest_waiting_at'>) => (totals ? totals[key] : null)
  const sentShare = totals && totals.received ? Math.round((totals.sent / totals.received) * 100) : null
  const isChosen = (group: DeliveryGroup) => chosen.length === 1 && chosen[0] === group
  const toggle = (group: DeliveryGroup) => onChoose(isChosen(group) ? null : group)
  const oldest = totals?.oldest_waiting_at ? Date.parse(totals.oldest_waiting_at) : NaN
  const waitingLine = totals && totals.waiting > 0 && Number.isFinite(oldest) && now - oldest > OLD_WAIT_MS
    ? `Oldest since ${clock(totals.oldest_waiting_at)}`
    : 'Will be sent automatically'
  const label = rangeLabel.startsWith('Today') ? "Today's totals" : `Totals for ${rangeLabel.replace(/^Yesterday · /, '')}`

  if (tooLong) {
    return <p className="attendance-totals-note" role="note">Totals are shown for up to 31 days. Pick a shorter range to see them.</p>
  }

  const problem = (
    failed && !data
      ? <p className="attendance-totals-error" role="alert">Couldn't load today's totals. <button className="text-button" type="button" onClick={() => void state.retry()}>Try again</button></p>
      : null
  )

  if (phone) {
    const chips: Array<{ group: DeliveryGroup; count: number | null }> = [
      { group: 'waiting', count: value('waiting') },
      { group: 'not_matched', count: value('not_matched') },
      { group: 'set_aside', count: value('set_aside') },
      { group: 'sent', count: value('sent') },
      ...(totals && totals.problem > 0 ? [{ group: 'problem' as const, count: totals.problem }] : []),
    ]
    return (
      <section className="attendance-today-phone" aria-label={label}>
        <button type="button" className="attendance-today-card" onClick={() => onChoose(null)} aria-busy={pending}>
          {pending
            ? <><span className="ui-skeleton ui-skeleton-line" aria-hidden="true" /><span className="sr-only">Loading totals</span></>
            : <>
              <strong className="attendance-today-received">{totals ? `${fmt(totals.received)} received` : '—'}</strong>
              {sentShare !== null && <span className="attendance-today-bar" aria-hidden="true"><span style={{ width: `${sentShare}%` }} /></span>}
              {sentShare !== null && <span className="attendance-today-share">{sentShare}% sent</span>}
              {division === 'all' && data && <small>{splitLine(data)}</small>}
            </>}
        </button>
        {problem}
        <div className="attendance-today-chips" role="group" aria-label="Status">
          {chips.map(({ group, count }) => (
            <button key={group} type="button" className="attendance-today-chip" data-group={group} aria-pressed={isChosen(group)} onClick={() => toggle(group)}>
              <Icon name={GROUP_CHIP[group].icon} size="sm" />
              <span>{GROUP_CHIP[group].label}</span>
              <strong>{count === null ? (pending ? '…' : '—') : fmt(count)}</strong>
            </button>
          ))}
        </div>
      </section>
    )
  }

  const tile = (group: DeliveryGroup | null, title: string, key: keyof Omit<GroupTotals, 'oldest_waiting_at'>, subtitle: ReactNode, tone: 'neutral' | 'working' | 'attention' | 'offline' = 'neutral') => (
    <StatTile
      key={title}
      label={title}
      value={pending ? null : value(key)}
      loading={pending}
      subtitle={subtitle}
      tone={tone}
      className={group && isChosen(group) ? 'attendance-tile is-selected' : 'attendance-tile'}
      pressed={group ? isChosen(group) : undefined}
      onClick={() => (group ? toggle(group) : onChoose(null))}
    />
  )
  return (
    <section className="attendance-today" aria-label={label}>
      <div className="attendance-tiles">
        {tile(null, 'Received', 'received', division === 'all' ? splitLine(data) || ' ' : 'check-ins and check-outs')}
        {tile('sent', 'Sent to HR system', 'sent', sentShare === null ? ' ' : `${sentShare}% of ${rangeLabel.startsWith('Today') ? "today's" : 'these'}`, 'working')}
        {tile('waiting', 'Waiting', 'waiting', <>{waitingLine}{totals && totals.trying_again > 0 && <><br />{fmt(totals.trying_again)} trying again</>}</>)}
        {tile('not_matched', 'Not matched', 'not_matched', 'Person not found or no CNIC', totals && totals.not_matched ? 'attention' : 'neutral')}
        {tile('set_aside', 'Set aside', 'set_aside', 'Set aside for checking')}
        {totals && totals.problem > 0 && tile('problem', 'Refused by the HR system', 'problem', 'Never sent again by itself', 'offline')}
      </div>
      {problem}
    </section>
  )
}

/** "Updated 12 s ago" from the summary's `computed_at`. */
export const updatedLine = (computedAt: string | null | undefined, now = Date.now()) => (computedAt ? `Updated ${ago(computedAt, now)}` : '')
