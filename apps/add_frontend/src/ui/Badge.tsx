import type { ReactNode } from 'react'
import { cx } from './cx'
import './chips.css'

export type BadgeTone = 'neutral' | 'info' | 'danger'

// A count or a short word ("Switched off"). hiddenText adds the full sentence
// for screen readers, inside the element so it is never skipped.
export function Badge({ tone = 'neutral', children, hiddenText, className }: { tone?: BadgeTone; children: ReactNode; hiddenText?: string; className?: string }) {
  return (
    <span className={cx('ui-badge', `tone-${tone}`, className)}>
      <span aria-hidden={hiddenText ? 'true' : undefined}>{children}</span>
      {hiddenText && <span className="sr-only">{hiddenText}</span>}
    </span>
  )
}

/** "99+" above 99. */
export const capCount = (value: number, cap = 99) => value > cap ? `${cap}+` : String(Math.max(0, value))
