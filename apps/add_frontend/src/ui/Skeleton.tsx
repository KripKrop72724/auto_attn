import { cx } from './cx'
import { UI_TEXT } from './uiText'
import './content.css'

export type SkeletonVariant = 'line' | 'block' | 'tile' | 'row'

// Grey shapes that stand in for content while it loads (static tint under
// reduced motion). Decorative; give one `label` per region so a screen reader
// hears "Loading…" once.
export function Skeleton({ variant = 'line', count = 1, width, label, className }: { variant?: SkeletonVariant; count?: number; width?: string; label?: string; className?: string }) {
  const shapes = Array.from({ length: Math.max(1, count) }, (_, index) => (
    <span key={index} className={cx('ui-skeleton', `ui-skeleton-${variant}`, className)} style={width ? { width } : undefined} aria-hidden="true" />
  ))
  if (!label) return <>{shapes}</>
  return (
    <div className="ui-skeleton-group" role="status">
      <span className="sr-only">{label || UI_TEXT.loading}</span>
      {shapes}
    </div>
  )
}
