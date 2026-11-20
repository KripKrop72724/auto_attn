import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App, {
  buildMachinePreview,
  bulkDeletionConfirmation,
  CommandProgress,
  confirmationMatches,
  formatAlertDiagnostics,
  identityConflictText,
  metricsTime,
  pktInputToUtc,
  pktTodayBounds,
  StatusBadge,
  validateUserDraft,
} from './App'
import { api } from './api'
import type { Device, DeviceUser, Overview } from './types'
import {
  conflictedUser, deliveryAlert, device, fetchStub, fullCnic, maskedCnic, response, selectUserTerminal, useDashboardStubs, user,
} from './test/renderApp'

describe('State Life ADD interface · Software updates', () => {
  useDashboardStubs(() => Promise.all([import('./features/Fleet'), import('./features/Firmware')]))

  it('updates firmware in the selected zones with one click and no password, reason or typed version', async () => {
    // Finish the lazy module's test-time transform before timing interactions.
    // The browser suite exercises route loading from the production bundle.
    await import('./features/Firmware')
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Firmware' }))
    expect(await screen.findByRole('heading', { name: 'Firmware', level: 1 })).toBeTruthy()
    const update = screen.getByRole('button', { name: 'Update firmware' }) as HTMLButtonElement
    await waitFor(() => expect(update.disabled).toBe(false))
    fireEvent.click(update)

    // The newest publishable release is preselected; only its HIL target's zone is offered.
    expect((screen.getByLabelText('Release') as HTMLSelectElement).value).toBe('release-2-2-4')
    expect(screen.queryByLabelText('Administrator password')).toBeNull()
    expect(screen.queryByLabelText('Audited reason')).toBeNull()
    expect(screen.queryByLabelText(/Type exact version/)).toBeNull()
    const start = screen.getByRole('button', { name: 'Update 0 zones' }) as HTMLButtonElement
    expect(start.disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Select all eligible' }))
    expect((screen.getByRole('checkbox', { name: new RegExp(device.zone_id) }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Update 1 zone' }))

    expect(await screen.findByText(/1 device queued/)).toBeTruthy()
    const bulkCalls = fetchMock.mock.calls.filter(
      ([input, init]) => String(input).endsWith('/api/v1/firmware/campaigns/bulk') && init?.method === 'POST',
    )
    expect(bulkCalls).toHaveLength(1)
    const body = JSON.parse(String(bulkCalls[0]?.[1]?.body))
    expect(body).toEqual({
      release_id: 'release-2-2-4',
      zone_ids: [device.zone_id],
      idempotency_key: expect.stringMatching(/^firmware-bulk:/),
    })
    // The old single-zone flow (preflight, then create) is not used.
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/campaigns/preflight'))).toBe(false)
  })

  it('shows stability warnings and a waiting offline device as information after a one-click update', async () => {
    await import('./features/Firmware')
    const base = fetchStub()
    const warning = {
      code: 'ESP_RESET_RATE_HIGH',
      connector_id: device.connector_id,
      display_name: device.display_name,
      message: `${device.display_name}: 46 ESP resets in 30 online hours on 2.6.15 over 7 days (1.53/h; the warning threshold is 0.5/h).`,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/api/v1/firmware/campaigns/bulk')) {
        return response({
          release_id: 'release-2-2-4',
          created: 1,
          results: [{
            zone_id: device.zone_id, outcome: 'CREATED', campaign_id: 'campaign-one', status: 'ACTIVE', message: null,
            stability_warnings: [warning], eligible: 1, fleet_hold: 'NATIONWIDE_TARGET_OFFLINE', closed: [],
          }],
        })
      }
      return base(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Firmware' }))
    expect(await screen.findByRole('heading', { name: 'Firmware', level: 1 })).toBeTruthy()
    const update = screen.getByRole('button', { name: 'Update firmware' }) as HTMLButtonElement
    await waitFor(() => expect(update.disabled).toBe(false))
    fireEvent.click(update)
    fireEvent.click(screen.getByRole('checkbox', { name: new RegExp(device.zone_id) }))
    fireEvent.click(screen.getByRole('button', { name: 'Update 1 zone' }))
    expect(await screen.findByText(/46 ESP resets in 30 online hours/)).toBeTruthy()
    expect(screen.getByText(/Created; waiting for the device to come online/)).toBeTruthy()
    expect(screen.queryByLabelText(/Override the stability warnings/)).toBeNull()
  })

})
