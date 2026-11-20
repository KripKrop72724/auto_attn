import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react'
import { formatCount } from './uiText'
import { cx } from './cx'

export type TabItem<T extends string = string> = {
  id: T
  label: string
  /** Shown as a small count after the label. */
  count?: number | null
  disabled?: boolean
}

const tabId = (prefix: string, id: string) => `${prefix}-tab-${id}`
const panelId = (prefix: string, id: string) => `${prefix}-panel-${id}`

const scrollIntoViewIfNeeded = (element: HTMLElement | null) => {
  if (element && typeof element.scrollIntoView === 'function') element.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}

// WAI-ARIA tabs: arrow keys, Home and End move and select; the selected tab is
// scrolled into view and edge fades show there are more tabs on a phone. The
// page keeps `value` in the URL (?tab=) through useUrlState.
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
  idPrefix,
  className,
}: {
  tabs: TabItem<T>[]
  value: T
  onChange: (value: T) => void
  /** Accessible name of the tab list. */
  label: string
  /** Prefix for tab and panel ids; pair with TabPanel. */
  idPrefix: string
  className?: string
}) {
  const listRef = useRef<HTMLDivElement>(null)
  const [overflow, setOverflow] = useState({ start: false, end: false })
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null)

  const measure = useCallback(() => {
    const list = listRef.current
    if (!list) return
    const maxScroll = list.scrollWidth - list.clientWidth
    setOverflow({ start: list.scrollLeft > 1, end: maxScroll - list.scrollLeft > 1 })
    const active = list.querySelector<HTMLElement>('[aria-selected="true"]')
    setIndicator(active && active.offsetWidth ? { left: active.offsetLeft, width: active.offsetWidth } : null)
  }, [])

  useLayoutEffect(() => {
    measure()
    scrollIntoViewIfNeeded(listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]') || null)
  }, [measure, value, tabs.length])

  useEffect(() => {
    const list = listRef.current
    if (!list || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(list)
    return () => observer.disconnect()
  }, [measure])

  const enabled = tabs.filter((tab) => !tab.disabled)
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = enabled.findIndex((tab) => tab.id === value)
    let next: TabItem<T> | undefined
    if (event.key === 'ArrowRight') next = enabled[(index + 1) % enabled.length]
    else if (event.key === 'ArrowLeft') next = enabled[(index - 1 + enabled.length) % enabled.length]
    else if (event.key === 'Home') next = enabled[0]
    else if (event.key === 'End') next = enabled[enabled.length - 1]
    if (!next) return
    event.preventDefault()
    onChange(next.id)
    window.requestAnimationFrame(() => document.getElementById(tabId(idPrefix, next.id))?.focus())
  }

  return (
    <div className={cx('ui-tabs', overflow.start && 'fade-start', overflow.end && 'fade-end', className)}>
      <div ref={listRef} className="ui-tablist" role="tablist" aria-label={label} onKeyDown={move} onScroll={measure}>
        {tabs.map((tab) => {
          const selected = tab.id === value
          return (
            <button
              key={tab.id}
              id={tabId(idPrefix, tab.id)}
              type="button"
              role="tab"
              className="ui-tab"
              aria-selected={selected}
              aria-controls={panelId(idPrefix, tab.id)}
              tabIndex={selected ? 0 : -1}
              disabled={tab.disabled}
              onClick={() => onChange(tab.id)}
            >
              <span>{tab.label}</span>{' '}
              {typeof tab.count === 'number' && <span className="ui-tab-count">{formatCount(tab.count)}</span>}
            </button>
          )
        })}
        {indicator && <span className="ui-tab-indicator" aria-hidden="true" style={{ transform: `translateX(${indicator.left}px)`, width: indicator.width }} />}
      </div>
    </div>
  )
}

export function TabPanel({ idPrefix, id, children, className }: { idPrefix: string; id: string; children: ReactNode; className?: string }) {
  return <div id={panelId(idPrefix, id)} role="tabpanel" aria-labelledby={tabId(idPrefix, id)} tabIndex={0} className={cx('ui-tabpanel', className)}>{children}</div>
}

export type SegmentOption<T extends string = string> = { id: T; label: string }

// A small set of choices where exactly one is on (role="radiogroup"). Arrow keys
// move and choose, the thumb slides behind the chosen option.
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  label,
  className,
  fullWidth = false,
}: {
  options: SegmentOption<T>[]
  value: T
  onChange: (value: T) => void
  label: string
  className?: string
  fullWidth?: boolean
}) {
  const groupRef = useRef<HTMLDivElement>(null)
  const index = Math.max(0, options.findIndex((option) => option.id === value))
  const choose = (next: number) => {
    const option = options[(next + options.length) % options.length]
    onChange(option.id)
    window.requestAnimationFrame(() => groupRef.current?.querySelector<HTMLElement>(`[data-segment="${option.id}"]`)?.focus())
  }
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { event.preventDefault(); choose(index + 1) }
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { event.preventDefault(); choose(index - 1) }
    else if (event.key === 'Home') { event.preventDefault(); choose(0) }
    else if (event.key === 'End') { event.preventDefault(); choose(options.length - 1) }
  }
  return (
    <div
      ref={groupRef}
      className={cx('ui-segmented', fullWidth && 'is-full', className)}
      role="radiogroup"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={{ '--segment-count': options.length, '--segment-index': index } as CSSProperties}
    >
      {options.map((option) => {
        const checked = option.id === value
        return (
          <button
            key={option.id}
            type="button"
            role="radio"
            className="ui-segment"
            data-segment={option.id}
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(option.id)}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
