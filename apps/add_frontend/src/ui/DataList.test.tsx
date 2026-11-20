import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DataList, type DataColumn } from './DataList'
import { StatusChip } from './StatusChip'
import { DivisionTag } from './DivisionTag'
import { setUpKitDom, tearDownKitDom } from './testUtils'

type Row = { id: string; name: string; division: 'life' | 'health'; status: string; ago: string }
const rows: Row[] = [
  { id: 'a', name: 'Building 9 · Device 3', division: 'life', status: 'Offline', ago: '2 hours ago' },
  { id: 'b', name: 'Swat', division: 'health', status: 'Working', ago: 'just now' },
  { id: 'c', name: 'Building 1', division: 'life', status: 'Working', ago: '1 min ago' },
]
const columns: DataColumn<Row>[] = [
  { id: 'name', header: 'Device', primary: true, cell: (row) => row.name },
  { id: 'division', header: 'Division', card: 'chips', cell: (row) => <DivisionTag division={row.division} /> },
  { id: 'status', header: 'Status', card: 'chips', cell: (row) => <StatusChip tone={row.status === 'Offline' ? 'offline' : 'working'} label={row.status} /> },
  { id: 'ago', header: 'Last heard', cell: (row) => row.ago },
]
const props = {
  rows,
  columns,
  label: 'Devices',
  getKey: (row: Row) => row.id,
  getHref: (row: Row) => `/devices/${row.id}`,
  rowLabel: (row: Row) => `${row.name}, ${row.status}, last heard ${row.ago}`,
  groupBy: (row: Row) => row.division === 'life' ? { id: 'life', label: 'Life (2)' } : { id: 'health', label: 'Health (1)' },
}

describe('DataList', () => {
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('is a table on desktop, with group headings and one real link per row', () => {
    const container = setUpKitDom()
    render(<DataList {...props} changedKeys={new Set(['b'])} />, { container })
    const table = screen.getByRole('table', { name: 'Devices' })
    expect(within(table).getByRole('columnheader', { name: 'Last heard' })).toBeTruthy()
    expect(within(table).getByText('Life (2)')).toBeTruthy()
    const link = screen.getByRole('link', { name: 'Building 9 · Device 3, Offline, last heard 2 hours ago' })
    expect(link.getAttribute('href')).toBe('/devices/a')
    const names = screen.getAllByRole('link').map((item) => item.textContent)
    expect(names).toEqual(['Building 9 · Device 3', 'Building 1', 'Swat'])
    expect(document.querySelector('tr[data-key="b"]')?.hasAttribute('data-changed')).toBe(true)
    expect(document.querySelector('tr[data-key="a"]')?.hasAttribute('data-changed')).toBe(false)
  })

  it('is stacked cards on a phone: name, then chips, then the rest', () => {
    const container = setUpKitDom({ phone: true })
    render(<DataList {...props} />, { container })
    expect(screen.queryByRole('table')).toBeNull()
    const region = screen.getByRole('region', { name: 'Devices' })
    const card = region.querySelector('[data-key="a"]') as HTMLElement
    const lines = Array.from(card.children).map((child) => child.className)
    expect(lines[0]).toContain('ui-card-name')
    expect(lines[1]).toContain('ui-card-chips')
    expect(within(card).getByText('Last heard')).toBeTruthy()
    expect(within(card).getByRole('link').getAttribute('href')).toBe('/devices/a')
    expect(screen.getByRole('heading', { name: 'Health (1)' })).toBeTruthy()
  })

  it('routes in-app through onRowClick and shows the empty state for no rows', () => {
    const container = setUpKitDom()
    const onRowClick = vi.fn((_row: Row, event: { preventDefault: () => void }) => event.preventDefault())
    const { rerender } = render(<DataList {...props} onRowClick={onRowClick} />, { container })
    fireEvent.click(screen.getByRole('link', { name: /Swat/ }))
    expect(onRowClick).toHaveBeenCalledWith(rows[1], expect.anything())
    rerender(<DataList {...props} rows={[]} empty={<p>No devices match.</p>} />)
    expect(screen.getByText('No devices match.')).toBeTruthy()
  })
})
