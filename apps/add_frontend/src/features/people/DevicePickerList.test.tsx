import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Device } from '../../types'
import { expectPlainText } from '../../words/testing'
import { DevicePickerList, type PickerShow } from './DevicePickerList'
import type { PeopleSummaryRow } from './model'

const base: Device = {
  connector_id: 'x', hardware_id: 'hw', zone_id: 'ZONE', zone_name: 'Lahore', device_id: '1', display_name: 'X',
  state: 'ONLINE', connected: true, firmware_version: '2.7.3', onboarding_generation: 1, last_onboarded_at: null,
  last_seen_at: new Date().toISOString(), current_activity: null, last_error_code: null,
  zkt: { user_count: 100 } as Device['zkt'],
}

const make = (connector_id: string, display_name: string, extra: Partial<Device> = {}): Device => ({ ...base, connector_id, display_name, zone_id: display_name, ...extra })

// Names from the live picker (30 Oct): Life and Health, one spare with the same code as a device in use.
const devices: Device[] = [
  make('swat', 'ZONE-SWAT-01', { zone_name: 'Swat' }),
  make('bld1', 'LF-ZONE-BLD1-01', { zkt: { user_count: 71 } as Device['zkt'] }),
  make('bld1-spare', 'LF-ZONE-BLD1-01', { is_spare: true, state: 'OFFLINE', connected: false, zkt: { user_count: 189 } as Device['zkt'] }),
  make('bld9-3', 'LF-ZONE-BLD9-03'),
  make('bld9-2', 'LF-ZONE-BLD9-02'),
  make('bld9-10', 'LF-ZONE-BLD9-10'),
  make('multan-1', 'ZONE-MULTAN-01', { zone_name: 'Multan' }),
  make('multan-2', 'ZONE-MULTAN-02', { zone_name: 'Multan', state: 'OFFLINE', connected: false }),
  make('quetta', 'ZONE-QUETTA-01', { zone_name: 'Quetta', zkt: null }),
]

function Picker({ loaded = true, list = devices, error, summary = null, summaryFailed = false, division = 'all' as 'all' | 'life' | 'health' }: { loaded?: boolean; list?: Device[]; error?: unknown; summary?: Map<string, PeopleSummaryRow> | null; summaryFailed?: boolean; division?: 'all' | 'life' | 'health' }) {
  const [query, setQuery] = useState('')
  const [show, setShow] = useState<PickerShow>('')
  const [spares, setSpares] = useState(false)
  return (
    <MemoryRouter>
      <DevicePickerList
        devices={list} loaded={loaded} error={error} onRetry={() => undefined}
        summary={summary} summaryFailed={summaryFailed}
        query={query} onQueryChange={setQuery} show={show} onShowChange={setShow} spares={spares} onSparesChange={setSpares}
        division={division} onShowAllDivisions={() => undefined}
        hrefFor={(id) => `/people/${id}`} onPick={(_, event) => event.preventDefault()}
      />
    </MemoryRouter>
  )
}

const links = () => Array.from(document.querySelectorAll<HTMLAnchorElement>('.people-device-list a.ui-row-link'))
const names = () => links().map((link) => link.getAttribute('aria-label') || '')

describe('People device picker', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows placeholder rows while the device list loads, never a false "no match"', () => {
    render(<Picker loaded={false} list={[]} />)
    expect(screen.getByText('Loading devices…')).toBeTruthy()
    expect(screen.queryByText(/No device matches/)).toBeNull()
    expect(screen.getByText('Life · –')).toBeTruthy()
  })

  it('lists every device at once with an empty search, Life before Health, by place name', () => {
    const view = render(<Picker />)
    const groups = Array.from(document.querySelectorAll('.ui-datalist-group, .ui-datalist-group-row')).map((node) => node.textContent)
    expect(groups[0]).toMatch(/^Life · 4/)
    expect(groups[1]).toMatch(/^Health · 4/)
    expect(names()).toEqual([
      'Open Building 1',
      'Open Building 9 · Device 2',
      'Open Building 9 · Device 3',
      'Open Building 9 · Device 10',
      'Open Multan · Device 1',
      'Open Multan · Device 2',
      'Open Quetta',
      'Open Swat',
    ])
    expect(screen.getByText('Changes will wait until it is back online.')).toBeTruthy()
    expect(screen.getByText('No machine linked yet')).toBeTruthy()
    expectPlainText(view.container)
  })

  it('hides spares until asked and never gives them the name of the device in use', () => {
    render(<Picker />)
    expect(names()).not.toContain('Open Building 1 · Spare')
    fireEvent.click(screen.getByRole('button', { name: 'Show spare devices (1)' }))
    expect(names()).toContain('Open Building 1 · Spare')
    expect(names().filter((name) => name === 'Open Building 1')).toHaveLength(1)
    expect(screen.getByText(/Spare \(not in use\) · 1/)).toBeTruthy()
  })

  it('searches by place or city and says when nothing matches', () => {
    render(<Picker />)
    fireEvent.change(screen.getByLabelText('Search devices'), { target: { value: 'multan' } })
    expect(names()).toEqual(['Open Multan · Device 1', 'Open Multan · Device 2'])
    fireEvent.change(screen.getByLabelText('Search devices'), { target: { value: 'zzz' } })
    expect(screen.getByText('No device matches “zzz”.')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'Clear search' }).at(-1) as HTMLElement)
    expect(names()).toHaveLength(8)
  })

  it('shows only Life with the Life switch', () => {
    render(<Picker division="life" />)
    expect(names()).toHaveLength(4)
    expect(names().every((name) => name.startsWith('Open Building'))).toBe(true)
  })

  it('offers Try again when the device list could not load', () => {
    render(<Picker list={[]} error={new Error('down')} />)
    expect(screen.getByText("Couldn't load the device list.")).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('hides the people-to-fix column and chips when the totals fail, without an error', () => {
    render(<Picker summaryFailed />)
    expect(screen.queryByText('Need fixing')).toBeNull()
    expect(screen.queryByRole('button', { name: /Has people to fix/ })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('counts people to fix and admin access from the totals and filters by them', async () => {
    const row = (connector_id: string, needs_fixing: number, admin = false): PeopleSummaryRow => ({
      connector_id, people: 100, with_cnic: 100 - needs_fixing, needs_fixing, no_cnic: needs_fixing, same_cnic_people: 0,
      cnic_typed_wrongly: 0, admins: 1, list_read_at: null,
      admin_access: admin ? { user_id: '1007', user_display_name: 'Ayesha Khan', expires_at: null } : null,
    })
    const summary = new Map([['bld9-3', row('bld9-3', 2)], ['bld9-2', row('bld9-2', 0, true)]])
    render(<Picker summary={summary} />)
    expect(screen.getByText('2 need fixing')).toBeTruthy()
    expect(screen.getByText('Temporary admin access is on')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Has people to fix/ }))
    await waitFor(() => expect(names()).toEqual(['Open Building 9 · Device 3']))
  })
})
