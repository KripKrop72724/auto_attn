import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { Icon, type IconName } from '../Icon'
import type { Tone } from './StatusChip'
import { formatCount } from './uiText'
import { cx } from './cx'
import './content.css'

export type StatTileProps = {
  label: string
  /** Numbers are shown "158,560" (en-PK). null shows a dash. */
  value: number | string | null
  subtitle?: ReactNode
  tone?: Tone
  icon?: IconName
  /** A link: the chevron shows all the time. Use onClick to route in-app (call preventDefault). */
  href?: string
  onClick?: (event: MouseEvent<HTMLElement>) => void
  loading?: boolean
  /** A button tile that filters a list: true while its filter is on (aria-pressed). */
  pressed?: boolean
  className?: string
}

export function StatTile({ label, value, subtitle, tone = 'neutral', icon, href, onClick, loading = false, pressed, className }: StatTileProps) {
  const shown = value === null || value === undefined ? '–' : typeof value === 'number' ? formatCount(value) : value
  const previous = useRef(shown)
  const [changed, setChanged] = useState(false)
  useEffect(() => {
    if (loading || previous.current === shown) return
    previous.current = shown
    setChanged(true)
    const timer = window.setTimeout(() => setChanged(false), 600)
    return () => window.clearTimeout(timer)
  }, [loading, shown])

  const classes = cx('ui-stat-tile', `tone-${tone}`, (href || onClick) && 'is-link', pressed && 'is-pressed', changed && 'is-changed', loading && 'is-loading', className)
  const body = (
    <>
      <span className="ui-stat-label">{icon && <Icon name={icon} size="sm" />}<span>{label}</span></span>
      {loading
        ? <span className="ui-skeleton ui-skeleton-line ui-stat-skeleton" aria-hidden="true" />
        : <strong className="ui-stat-value">{shown}</strong>}
      {subtitle && !loading && <span className="ui-stat-subtitle">{subtitle}</span>}
      {(href || onClick) && <Icon name="chevron" size="sm" className="ui-stat-chevron" />}
      {loading && <span className="sr-only">{`Loading ${label}…`}</span>}
    </>
  )
  if (href) return <a className={classes} href={href} onClick={onClick} data-tone={tone}>{body}</a>
  if (onClick) return <button type="button" className={classes} onClick={onClick} data-tone={tone} aria-pressed={pressed}>{body}</button>
  return <div className={classes} data-tone={tone}>{body}</div>
}
