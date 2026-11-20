import { UI_TEXT } from './uiText'
import { cx } from './cx'

export type Division = 'life' | 'health'

// Square-ish tag with a marker and the word, so it never reads as a status pill
// or a selected filter chip, and never relies on colour alone.
export function DivisionTag({ division, className }: { division: Division; className?: string }) {
  return (
    <span className={cx('ui-division-tag', `is-${division}`, className)} data-division={division}>
      {division === 'life' ? UI_TEXT.life : UI_TEXT.health}
    </span>
  )
}
