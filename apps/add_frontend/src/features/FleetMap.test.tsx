import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Device } from '../types'
import { FleetMap } from './FleetMap'

const makeDevice = (overrides: Partial<Device> = {}): Device => ({
  connector_id: 'tower-three',
  hardware_id: 'e0:72:a1:d6:f3:28',
  zone_id: 'ZONE-SLICTOWER-3FL',
  zone_name: 'SLICTOWER',
  device_id: '1',
  display_name: 'SLICTOWER · 3rd Floor',
  state: 'ONLINE',
  connected: true,
  firmware_version: '2.4.5',
  onboarding_generation: 2,
  last_onboarded_at: null,
  last_seen_at: '2026-08-10T10:00:00Z',
  current_activity: 'LIVE_CAPTURE',
  last_error_code: null,
  zkt: null,
  ...overrides,
})

afterEach(cleanup)

describe('FleetMap', () => {
  it('selects a location, exposes device actions, and keeps unknown locations explicit', () => {
    const inspect = vi.fn()
    const manageUsers = vi.fn()
    const tower = makeDevice()
    const unknown = makeDevice({
      connector_id: 'unknown-location',
      zone_id: 'ZONE-UNKNOWN-01',
      zone_name: '',
      display_name: 'New branch connector',
      last_seen_at: null,
      current_activity: null,
    })
    render(
      <FleetMap
        devices={[tower, unknown]}
        loading={false}
        onInspect={inspect}
        onManageUsers={manageUsers}
        formatRelativeTime={(value) => value || 'Never seen'}
      />,
    )

    expect(screen.getByRole('heading', { name: '1 operating location' })).toBeTruthy()
    expect(screen.getByText('Location not mapped')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open Islamabad location, 1 device' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Islamabad, 1 device, All online/i }))
    expect(screen.getByRole('heading', { name: 'Islamabad' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: `Inspect ${tower.display_name}` }))
    expect(inspect).toHaveBeenCalledWith(tower)
    fireEvent.click(screen.getAllByRole('button', { name: 'Manage users' })[0])
    expect(manageUsers).toHaveBeenCalledWith(tower)
    fireEvent.click(screen.getByRole('button', { name: 'Close Islamabad details' }))
    expect(screen.queryByRole('heading', { name: 'Islamabad' })).toBeNull()
  })

  it('renders safe loading and empty states', () => {
    const props = {
      devices: [] as Device[],
      onInspect: vi.fn(),
      onManageUsers: vi.fn(),
      formatRelativeTime: () => 'Never seen',
    }
    const { rerender } = render(<FleetMap {...props} loading />)
    expect(screen.getByText('Synchronizing national fleet…')).toBeTruthy()
    rerender(<FleetMap {...props} loading={false} />)
    expect(screen.getByText('No mapped devices match this view.')).toBeTruthy()
    expect(screen.getByText('Awaiting devices')).toBeTruthy()
  })
})
