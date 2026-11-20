import { cx } from './cx'

export type SpinnerSize = 'sm' | 'md'

// A turning ring (a static ring under reduced motion). With a label it is a
// polite status message; without one it is decoration next to visible text.
export function Spinner({ size = 'sm', label, className }: { size?: SpinnerSize; label?: string; className?: string }) {
  if (label) {
    return (
      <span className={cx('ui-spinner-wrap', className)} role="status">
        <span className={cx('ui-spinner', `ui-spinner-${size}`)} aria-hidden="true" />
        <span className="sr-only">{label}</span>
      </span>
    )
  }
  return <span className={cx('ui-spinner', `ui-spinner-${size}`, className)} aria-hidden="true" />
}
