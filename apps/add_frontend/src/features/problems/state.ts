// New / Seen / Fixed for one problem row. Shared by the Problems page and the
// device page without pulling in either page's styles.
const upper = (value: string | null | undefined) => (value || '').trim().toUpperCase()

type Row = { state: string; acknowledged_at?: string | null }

export const problemIsFixed = (row: Pick<Row, 'state'>) => upper(row.state) === 'RESOLVED'
export const problemIsSeen = (row: Row) =>
  !problemIsFixed(row) && (upper(row.state) === 'ACKNOWLEDGED' || Boolean(row.acknowledged_at))
export const problemIsNew = (row: Row) =>
  upper(row.state) === 'OPEN' && !row.acknowledged_at
