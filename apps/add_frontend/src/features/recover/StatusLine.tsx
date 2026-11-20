// One status line instead of the four engineering tiles (08-advanced 1.1).
// Each non-zero part is a link that sets the Recoveries filter; zero parts are
// grey text. No number shows before the list has arrived.
import { useRef, type KeyboardEvent } from 'react'
import { ErrorState, Skeleton, TechnicalDetails } from '../../ui'
import { RECOVER_PAGE, RECOVER_TABS, schedulerLines, statusSegments, tabCount, type RecoverStatusFilter, type RecoverTab, type StatusTotals } from '../../words/recover'
import type { ReconciliationScheduler } from '../../types'

export function RecoverStatusLine({ totals, scheduler, enabled, error, onRetry, onFilter }: {
  totals: StatusTotals | null
  scheduler: ReconciliationScheduler | null
  enabled: boolean | null
  error: unknown
  onRetry: () => unknown
  onFilter: (filter: RecoverStatusFilter) => void
}) {
  if (!totals && error) return <ErrorState compact title={RECOVER_PAGE.loadFailed} error={error} onRetry={onRetry} className="recover-status-error" />
  if (!totals) return <div className="recover-status-line is-loading"><Skeleton variant="line" width="280px" label="Loading recoveries" /></div>
  const segments = statusSegments(totals)
  const quiet = segments.every((segment) => segment.count === 0)
  const backlogFull = scheduler && (scheduler.history_backlog ?? 0) >= (scheduler.history_backlog_limit ?? Number.MAX_SAFE_INTEGER)
  return (
    <div className="recover-status">
      {enabled === false ? (
        <p className="recover-status-line">{RECOVER_PAGE.switchedOff}</p>
      ) : quiet ? (
        <p className="recover-status-line">{RECOVER_PAGE.nothingRunning}</p>
      ) : (
        <p className="recover-status-line" aria-label="Recoveries now">
          {segments.map((segment, index) => (
            <span key={segment.filter} className="recover-status-part" data-urgent={segment.urgent ? '' : undefined}>
              {index > 0 && <span aria-hidden="true" className="recover-status-sep"> · </span>}
              {segment.count > 0 ? (
                <button type="button" className="text-button recover-status-link" onClick={() => onFilter(segment.filter)}>
                  {segment.urgent && <span className="recover-status-dot" aria-hidden="true">●</span>}
                  {segment.text}
                </button>
              ) : (
                <span className="recover-status-zero">{segment.text}</span>
              )}
            </span>
          ))}
        </p>
      )}
      {backlogFull && <p className="recover-status-note">{RECOVER_PAGE.backlogFull}</p>}
      {Boolean(error) && <ErrorState compact title={RECOVER_PAGE.loadFailed} message="Showing the last figures we had." error={error} onRetry={onRetry} />}
      {scheduler && (
        <TechnicalDetails className="recover-status-tech">
          <ul>{schedulerLines(scheduler).map((line) => <li key={line}>{line}</li>)}</ul>
        </TechnicalDetails>
      )}
    </div>
  )
}

/** Three tabs with action-only counts, "—" while loading and "999+" for big numbers. */
export function RecoverTabs({ value, onChange, counts, phone }: {
  value: RecoverTab
  onChange: (tab: RecoverTab) => void
  counts: Partial<Record<RecoverTab, number | null | undefined>>
  phone: boolean
}) {
  const ids: RecoverTab[] = ['recoveries', 'unreadable', 'held']
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % ids.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + ids.length) % ids.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = ids.length - 1
    else return
    event.preventDefault()
    onChange(ids[next])
    refs.current[next]?.focus()
  }
  return (
    <div className="ui-tabs recover-tabs">
      <div className="ui-tablist" role="tablist" aria-label={RECOVER_PAGE.sections}>
        {ids.map((id, index) => {
          const count = counts[id]
          const showCount = id !== 'held' && (count === null || (typeof count === 'number' && count > 0))
          return (
            <button
              key={id}
              ref={(node) => { refs.current[index] = node }}
              id={`recover-tab-${id}`}
              type="button"
              role="tab"
              className="ui-tab"
              aria-selected={value === id}
              aria-controls={value === id ? `recover-panel-${id}` : undefined}
              tabIndex={value === id ? 0 : -1}
              onKeyDown={(event) => move(event, index)}
              onClick={() => onChange(id)}
            >
              <span>{phone ? RECOVER_TABS[id].phone : RECOVER_TABS[id].label}</span>
              {showCount && <>{' '}<span className="ui-tab-count">{count === null ? '—' : tabCount(count as number)}</span></>}
            </button>
          )
        })}
      </div>
    </div>
  )
}
