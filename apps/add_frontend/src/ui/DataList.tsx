import { Fragment, type MouseEvent, type ReactNode } from 'react'
import { Icon } from '../Icon'
import { useIsPhone } from './useMediaQuery'
import { cx } from './cx'
import './datalist.css'

export type DataColumn<T> = {
  id: string
  header: string
  cell: (row: T) => ReactNode
  /** The row's name: holds the link and is line 1 of a phone card. One per list. */
  primary?: boolean
  /** Where the value goes on a phone card. Default 'meta'. */
  card?: 'chips' | 'reason' | 'meta' | 'hidden'
  align?: 'start' | 'end'
  /** Visually hide the header text (it is still read out). */
  hideHeader?: boolean
  className?: string
}

export type DataGroup = { id: string; label: string }

export type DataListProps<T> = {
  rows: T[]
  columns: DataColumn<T>[]
  getKey: (row: T) => string
  /** Accessible name of the list (table caption). */
  label: string
  /** Makes the whole row a link. */
  getHref?: (row: T) => string | undefined
  /** In-app navigation for links (call preventDefault), or the action for rows without href. */
  onRowClick?: (row: T, event: MouseEvent<HTMLElement>) => void
  /** Spoken name of the row link, e.g. "Building 9 · Device 3, Offline, last heard 2 hours ago". */
  rowLabel?: (row: T) => string
  /** Sticky group headings such as "Life (6)"; rows keep their order inside each group. */
  groupBy?: (row: T) => DataGroup
  /** Rows that just changed get data-changed and a 1.2 s highlight. */
  changedKeys?: ReadonlySet<string>
  empty?: ReactNode
  /** Force the layout; default is a table from 761 px and cards up to 760 px. */
  layout?: 'auto' | 'table' | 'cards'
  className?: string
}

function groupRows<T>(rows: T[], groupBy?: (row: T) => DataGroup) {
  if (!groupBy) return [{ group: null as DataGroup | null, rows }]
  const groups = new Map<string, { group: DataGroup; rows: T[] }>()
  for (const row of rows) {
    const group = groupBy(row)
    const entry = groups.get(group.id) || { group, rows: [] }
    entry.rows.push(row)
    groups.set(group.id, entry)
  }
  return Array.from(groups.values())
}

// Table on desktop, stacked cards on phones (name, then chips, then the reason
// line). The whole row opens the item through one real link in the name cell,
// so middle-click, "open in new tab" and copy link all work.
export function DataList<T>({
  rows,
  columns,
  getKey,
  label,
  getHref,
  onRowClick,
  rowLabel,
  groupBy,
  changedKeys,
  empty,
  layout = 'auto',
  className,
}: DataListProps<T>) {
  const phone = useIsPhone()
  const cards = layout === 'cards' || (layout === 'auto' && phone)
  const primary = columns.find((column) => column.primary) || columns[0]
  const groups = groupRows(rows, groupBy)
  if (!rows.length) return <>{empty ?? null}</>

  const rowLink = (row: T, content: ReactNode) => {
    const href = getHref?.(row)
    const name = rowLabel?.(row)
    if (href) return <a className="ui-row-link" href={href} aria-label={name} onClick={onRowClick ? (event) => onRowClick(row, event) : undefined}>{content}</a>
    if (onRowClick) return <button type="button" className="ui-row-link" aria-label={name} onClick={(event) => onRowClick(row, event)}>{content}</button>
    return content
  }
  const linked = Boolean(getHref || onRowClick)

  if (cards) {
    return (
      <div className={cx('ui-datalist', 'is-cards', className)} role="region" aria-label={label}>
        {groups.map(({ group, rows: groupRowsList }) => (
          <Fragment key={group?.id || 'all'}>
            {group && <h3 className="ui-datalist-group">{group.label}</h3>}
            <ul className="ui-datalist-cards">
              {groupRowsList.map((row) => {
                const key = getKey(row)
                const chips = columns.filter((column) => column !== primary && column.card === 'chips')
                const reason = columns.filter((column) => column !== primary && column.card === 'reason')
                const meta = columns.filter((column) => column !== primary && (column.card ?? 'meta') === 'meta')
                return (
                  <li key={key} className={cx('ui-datalist-card', linked && 'is-linked')} data-key={key} data-changed={changedKeys?.has(key) ? '' : undefined}>
                    <div className="ui-card-line ui-card-name">{rowLink(row, primary.cell(row))}{linked && <Icon name="chevron" size="sm" className="ui-row-chevron" />}</div>
                    {chips.length > 0 && <div className="ui-card-line ui-card-chips">{chips.map((column) => <Fragment key={column.id}>{column.cell(row)}</Fragment>)}</div>}
                    {reason.length > 0 && <div className="ui-card-line ui-card-reason">{reason.map((column) => <Fragment key={column.id}>{column.cell(row)}</Fragment>)}</div>}
                    {meta.length > 0 && (
                      <dl className="ui-card-meta">
                        {meta.map((column) => <div key={column.id}><dt>{column.header}</dt><dd>{column.cell(row)}</dd></div>)}
                      </dl>
                    )}
                  </li>
                )
              })}
            </ul>
          </Fragment>
        ))}
      </div>
    )
  }

  return (
    <div className={cx('ui-datalist', 'is-table', className)}>
      <table className="ui-table">
        <caption className="sr-only">{label}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.id} scope="col" className={cx(column.align === 'end' && 'is-end', column.className)}>
                {column.hideHeader ? <span className="sr-only">{column.header}</span> : column.header}
              </th>
            ))}
          </tr>
        </thead>
        {groups.map(({ group, rows: groupRowsList }) => (
          <tbody key={group?.id || 'all'}>
            {group && <tr className="ui-datalist-group-row"><th scope="colgroup" colSpan={columns.length}>{group.label}</th></tr>}
            {groupRowsList.map((row) => {
              const key = getKey(row)
              return (
                <tr key={key} className={cx(linked && 'is-linked')} data-key={key} data-changed={changedKeys?.has(key) ? '' : undefined}>
                  {columns.map((column) => column === primary
                    ? <th key={column.id} scope="row" className={cx('ui-cell-primary', column.className)}>{rowLink(row, column.cell(row))}</th>
                    : <td key={column.id} className={cx(column.align === 'end' && 'is-end', column.className)}>{column.cell(row)}</td>)}
                </tr>
              )
            })}
          </tbody>
        ))}
      </table>
    </div>
  )
}
