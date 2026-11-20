import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { FilterBar } from './FilterBar'
import { setUpKitDom, tearDownKitDom } from './testUtils'

class ResizeObserverStub { observe() {} disconnect() {} }

function Harness({ onSort = () => undefined }: { onSort?: (value: string) => void }) {
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('all')
  const [city, setCity] = useState<string | null>(null)
  return <FilterBar
    search={{ value: q, onChange: setQ, label: 'Search devices' }}
    chips={[{ id: 'all', label: 'All', count: 20 }, { id: 'offline', label: 'Offline', count: 2 }]}
    chipValue={status}
    onChipChange={setStatus}
    moreFilters={<label>City<select value={city || ''} onChange={(event) => setCity(event.target.value || null)}><option value="">Any city</option><option value="lahore">Lahore</option></select></label>}
    moreFiltersCount={city ? 1 : 0}
    active={[...(status !== 'all' ? [{ id: 'status', label: 'Offline', onRemove: () => setStatus('all') }] : []), ...(city ? [{ id: 'city', label: 'Lahore', onRemove: () => setCity(null) }] : [])]}
    onClearAll={() => { setStatus('all'); setCity(null); setQ('') }}
    result={{ shown: status === 'offline' ? 2 : 20, total: 20 }}
    sort={{ value: 'worst', onChange: onSort, options: [{ id: 'worst', label: 'Worst first' }, { id: 'name', label: 'Name' }] }}
  />
}

describe('FilterBar', () => {
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('searches with a clear button and focuses the search on "/"', () => {
    const container = setUpKitDom()
    render(<Harness />, { container })
    const search = screen.getByRole('searchbox', { name: 'Search devices' }) as HTMLInputElement
    fireEvent.keyDown(document.body, { key: '/' })
    expect(document.activeElement).toBe(search)
    fireEvent.change(search, { target: { value: 'swat' } })
    expect(search.value).toBe('swat')
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(search.value).toBe('')
  })

  it('shows chips with counts, the active filters with ×, Clear all and the result line', () => {
    const container = setUpKitDom()
    render(<Harness />, { container })
    expect(screen.getByText('Showing 20 of 20')).toBeTruthy()
    const offline = screen.getByRole('button', { name: 'Offline 2' })
    fireEvent.click(offline)
    expect(offline.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('Showing 2 of 20')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Offline' }))
    expect(screen.getByRole('button', { name: 'Offline 2' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Offline 2' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    expect(screen.queryByRole('button', { name: /Remove filter/ })).toBeNull()
  })

  it('opens More filters as a bottom sheet on a phone', () => {
    const container = setUpKitDom({ phone: true })
    render(<Harness />, { container })
    fireEvent.click(screen.getByRole('button', { name: 'More filters' }))
    const sheet = screen.getByRole('dialog', { name: 'More filters' })
    expect(sheet.getAttribute('data-placement')).toBe('bottom')
    fireEvent.change(screen.getByRole('combobox', { name: 'City' }), { target: { value: 'lahore' } })
    fireEvent.click(screen.getByRole('button', { name: 'Show results' }))
    expect(screen.queryByRole('dialog', { name: 'More filters' })).toBeNull()
    expect(screen.getByRole('button', { name: 'More filters 1' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remove filter: Lahore' })).toBeTruthy()
  })

  it('opens More filters as a popover on desktop and sorts', () => {
    const container = setUpKitDom()
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
    const onSort = vi.fn()
    render(<Harness onSort={onSort} />, { container })
    fireEvent.click(screen.getByRole('button', { name: 'More filters' }))
    expect(document.querySelector('.anchored-layer .ui-filter-popover')).not.toBeNull()
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), { target: { value: 'name' } })
    expect(onSort).toHaveBeenCalledWith('name')
  })
})
