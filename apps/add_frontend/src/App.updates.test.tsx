import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

  it('updates software in the chosen devices with one click and no password, reason or typed version', async () => {
    // Finish the lazy module's test-time transform before timing interactions.
    // The browser suite exercises route loading from the production bundle.
    await import('./features/Firmware')
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState({}, '', '/updates')
    render(<App />)
    const update = (await screen.findByRole('button', { name: 'Update software' }, { timeout: 10_000 })) as HTMLButtonElement
    expect(screen.getAllByRole('heading', { name: 'Software updates', level: 1 }).length).toBeGreaterThan(0)
    await waitFor(() => expect(update.disabled).toBe(false))
    fireEvent.click(update)

    // The newest version that may start is preselected; only its test device's place is offered.
    const sheet = await screen.findByRole('dialog', { name: 'Update software' })
    expect((within(sheet).getByRole('radio', { name: /2\.2\.4/ }) as HTMLInputElement).checked).toBe(true)
    expect(within(sheet).getByText(/Test devices only/)).toBeTruthy()
    expect(within(sheet).queryByLabelText(/password/i)).toBeNull()
    expect(within(sheet).queryByLabelText('Audited reason')).toBeNull()
    expect(within(sheet).queryByLabelText(/Type exact version/)).toBeNull()
    const start = within(sheet).getByRole('button', { name: 'Update 0 devices' }) as HTMLButtonElement
    expect(start.disabled).toBe(true)
    fireEvent.click(within(sheet).getByRole('checkbox', { name: /Choose all/ }))
    fireEvent.click(within(sheet).getByRole('button', { name: /^Update 1 device/ }))

    expect(await within(sheet).findByText('Started.')).toBeTruthy()
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

  it('shows restart warnings and a waiting offline device as information after a one-click update', async () => {
    await import('./features/Firmware')
    const base = fetchStub()
    const warning = {
      code: 'ESP_RESET_RATE_HIGH',
      connector_id: device.connector_id,
      display_name: device.display_name,
      history: { resets: 46, days: 7, online_hours: 30 },
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
    window.history.replaceState({}, '', '/updates')
    render(<App />)
    const update = (await screen.findByRole('button', { name: 'Update software' }, { timeout: 10_000 })) as HTMLButtonElement
    await waitFor(() => expect(update.disabled).toBe(false))
    fireEvent.click(update)
    const sheet = await screen.findByRole('dialog', { name: 'Update software' })
    fireEvent.click(within(sheet).getByRole('checkbox', { name: /Choose all/ }))
    fireEvent.click(within(sheet).getByRole('button', { name: /^Update 1 device/ }))
    expect(await within(sheet).findByText(/restarted 46 times in the last 7 days/)).toBeTruthy()
    expect(within(sheet).getByText(/^Started\. It begins when .+ comes online\.$/)).toBeTruthy()
    expect(screen.queryByLabelText(/Override the stability warnings/)).toBeNull()
  })

})
