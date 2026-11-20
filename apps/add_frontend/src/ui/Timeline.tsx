import type { ReactNode } from 'react'
import { Icon, type IconName } from '../Icon'
import type { Tone } from './StatusChip'
import { TechnicalDetails } from './TechnicalDetails'
import { cx } from './cx'
import './steps.css'

export type TimelineItem = {
  id: string
  /** Already-formatted time, e.g. "Today, 3:04 pm" (lib/time dayTime()). */
  time: string
  /** Machine-readable time for <time dateTime>. */
  dateTime?: string
  title: ReactNode
  description?: ReactNode
  icon?: IconName
  tone?: Tone
  /** Codes and raw messages for this item, closed by default. */
  technical?: ReactNode
}

export function Timeline({ items, label, className }: { items: TimelineItem[]; label?: string; className?: string }) {
  return (
    <ol className={cx('ui-timeline', className)} aria-label={label}>
      {items.map((item) => (
        <li key={item.id} className={cx('ui-timeline-item', item.tone && `tone-${item.tone}`)} data-tone={item.tone}>
          <span className="ui-timeline-marker" aria-hidden="true">{item.icon ? <Icon name={item.icon} size="sm" /> : <span className="ui-timeline-dot" />}</span>
          <div className="ui-timeline-content">
            <time dateTime={item.dateTime}>{item.time}</time>
            <p className="ui-timeline-title">{item.title}</p>
            {item.description && <p className="ui-timeline-text">{item.description}</p>}
            {item.technical && <TechnicalDetails>{item.technical}</TechnicalDetails>}
          </div>
        </li>
      ))}
    </ol>
  )
}
