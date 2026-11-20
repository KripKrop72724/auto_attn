import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { AnchoredLayer } from '../AnchoredLayer'
import { Icon } from '../Icon'
import { Sheet } from './Sheet'
import { useIsPhone } from './useMediaQuery'
import { UI_TEXT, formatCount } from './uiText'
import { cx } from './cx'

export type FilterChip = {
  id: string
  label: string
  /** Computed by the page (client lists) or from the API (server lists). */
  count?: number | null
}

export type ActiveFilter = { id: string; label: string; onRemove: () => void }

export type SortOption = { id: string; label: string }

export type FilterBarProps = {
  search?: {
    value: string
    onChange: (value: string) => void
    /** Visible label is hidden; this is the accessible name and placeholder. */
    label?: string
    placeholder?: string
  }
  /** Quick chips. One chosen (`chipValue` string) or several (string[]). */
  chips?: FilterChip[]
  chipValue?: string | string[]
  onChipChange?: (id: string) => void
  chipsLabel?: string
  /** Contents of "More filters": a Sheet on phones, a popover on desktop. */
  moreFilters?: ReactNode
  /** How many of the "More filters" are set, shown on the button. */
  moreFiltersCount?: number
  /** The filters in force, each with ×, plus "Clear all". */
  active?: ActiveFilter[]
  onClearAll?: () => void
  /** "Showing 6 of 20". */
  result?: { shown: number; total: number }
  sort?: { value: string; options: SortOption[]; onChange: (value: string) => void; label?: string }
  /** Page actions at the end of the bar (e.g. Select). */
  actions?: ReactNode
  className?: string
}

const isTypingTarget = (target: EventTarget | null) => {
  const element = target as HTMLElement | null
  if (!element) return false
  return Boolean(element.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'))
}

// One filter bar for every list. Controlled: the page keeps the values in the
// URL through useUrlState() and passes them in.
export function FilterBar({
  search,
  chips,
  chipValue,
  onChipChange,
  chipsLabel = 'Quick filters',
  moreFilters,
  moreFiltersCount = 0,
  active,
  onClearAll,
  result,
  sort,
  actions,
  className,
}: FilterBarProps) {
  const id = useId()
  const searchRef = useRef<HTMLInputElement>(null)
  const moreRef = useRef<HTMLButtonElement>(null)
  const [moreOpen, setMoreOpen] = useState(false)
  const phone = useIsPhone()

  useEffect(() => {
    if (!search) return
    const handle = (event: KeyboardEvent) => {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target)) return
      if (document.getElementById('root')?.hasAttribute('inert')) return
      event.preventDefault()
      searchRef.current?.focus()
    }
    document.addEventListener('keydown', handle)
    return () => document.removeEventListener('keydown', handle)
  }, [search])

  const chosen = (chipId: string) => Array.isArray(chipValue) ? chipValue.includes(chipId) : chipValue === chipId
  const searchLabel = search?.label || UI_TEXT.search

  return (
    <div className={cx('ui-filter-bar', className)}>
      <div className="ui-filter-row">
        {search && (
          <div className="ui-filter-search">
            <Icon name="search" size="sm" className="ui-filter-search-icon" />
            <label className="sr-only" htmlFor={`${id}-search`}>{searchLabel}</label>
            <input
              ref={searchRef}
              id={`${id}-search`}
              type="search"
              value={search.value}
              placeholder={search.placeholder || searchLabel}
              autoComplete="off"
              enterKeyHint="search"
              onChange={(event) => search.onChange(event.target.value)}
            />
            {search.value && (
              <button type="button" className="icon-button ui-icon-button ui-filter-search-clear" aria-label={UI_TEXT.clearSearch} onClick={() => { search.onChange(''); searchRef.current?.focus() }}>
                <Icon name="x" size="sm" />
              </button>
            )}
          </div>
        )}
        {sort && (
          <label className="ui-filter-sort">
            <span className="sr-only">{sort.label || UI_TEXT.sort}</span>
            <Icon name="sort" size="sm" />
            <select value={sort.value} onChange={(event) => sort.onChange(event.target.value)} aria-label={sort.label || UI_TEXT.sort}>
              {sort.options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
            </select>
          </label>
        )}
        {moreFilters && (
          <button
            ref={moreRef}
            type="button"
            className="button secondary ui-filter-more"
            aria-haspopup="dialog"
            aria-expanded={moreOpen}
            onClick={() => setMoreOpen((open) => !open)}
          >
            <Icon name="filter" size="sm" />
            <span>{UI_TEXT.moreFilters}</span>{' '}
            {moreFiltersCount > 0 && <span className="ui-filter-more-count">{formatCount(moreFiltersCount)}</span>}
          </button>
        )}
        {actions && <div className="ui-filter-actions">{actions}</div>}
      </div>
      {chips && chips.length > 0 && (
        <div className="ui-filter-chips" role="group" aria-label={chipsLabel}>
          {chips.map((chip) => (
            <button
              key={chip.id}
              type="button"
              className="ui-filter-chip"
              aria-pressed={chosen(chip.id)}
              onClick={() => onChipChange?.(chip.id)}
            >
              <span>{chip.label}</span>{' '}
              {typeof chip.count === 'number' && <span className="ui-filter-chip-count">{formatCount(chip.count)}</span>}
            </button>
          ))}
        </div>
      )}
      {((active && active.length > 0) || result) && (
        <div className="ui-filter-status">
          {result && <p className="ui-filter-result" aria-live="polite">{UI_TEXT.showing(formatCount(result.shown), formatCount(result.total))}</p>}
          {active && active.length > 0 && (
            <ul className="ui-filter-active">
              {active.map((filter) => (
                <li key={filter.id}>
                  <button type="button" className="ui-filter-active-chip" onClick={filter.onRemove} aria-label={UI_TEXT.removeFilter(filter.label)}>
                    <span>{filter.label}</span>
                    <Icon name="x" size="sm" />
                  </button>
                </li>
              ))}
              {onClearAll && <li><button type="button" className="text-button ui-filter-clear" onClick={onClearAll}>{UI_TEXT.clearAll}</button></li>}
            </ul>
          )}
        </div>
      )}
      {moreOpen && moreFilters && (phone
        ? (
          <Sheet
            title={UI_TEXT.moreFilters}
            onClose={() => setMoreOpen(false)}
            returnFocusRef={moreRef}
            placement="bottom"
            footer={<button type="button" className="button primary full" onClick={() => setMoreOpen(false)}>{UI_TEXT.showResults}</button>}
          >
            {moreFilters}
          </Sheet>
        )
        : (
          <AnchoredLayer anchorRef={moreRef} preferredWidth={360} onDismiss={(reason) => { setMoreOpen(false); if (reason === 'escape') moreRef.current?.focus() }}>
            <div className="ui-filter-popover" role="dialog" aria-label={UI_TEXT.moreFilters}>{moreFilters}</div>
          </AnchoredLayer>
        ))}
    </div>
  )
}
