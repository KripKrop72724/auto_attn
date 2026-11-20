import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App, {
  buildMachinePreview,
  bulkDeletionConfirmation,
  CommandProgress,
  confirmationMatches,
  formatAlertDiagnostics,
  metricsTime,
  pktInputToUtc,
  pktTodayBounds,
  StatusBadge,
  validateUserDraft,
} from './App'
import { api } from './api'
import { personProblems } from './words/people'
import type { Device, DeviceUser, Overview } from './types'
import {
  conflictedUser, deliveryAlert, device, fetchStub, fullCnic, maskedCnic, response, selectUserTerminal, useDashboardStubs, user,
} from './test/renderApp'

describe('State Life ADD interface · People', () => {
  useDashboardStubs(() => Promise.all([import('./features/Fleet'), import('./features/UsersWorkspace')]))

  it('renders only masked CNIC in the selected-terminal users workspace', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await selectUserTerminal()
    expect(await screen.findByText(maskedCnic)).toBeTruthy()
    expect(document.body.textContent).not.toContain(fullCnic)
    expect(screen.getByRole('button', { name: /edit ayesha fatima/i })).toBeTruthy()
    fireEvent.click(screen.getByLabelText(/more for ayesha fatima/i))
    expect(screen.getByRole('button', { name: /remove ayesha fatima/i })).toBeTruthy()
  }, 20_000)

  it('marks duplicate CNIC identities with text and a non-color-only pattern', async () => {
    vi.stubGlobal('fetch', fetchStub([conflictedUser]))
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await selectUserTerminal()
    const warning = (await screen.findAllByText(/same cnic as 1008/i))[0]
    expect(warning.closest('article')?.classList.contains('identity-conflict')).toBe(true)
    expect(warning.closest('article')?.getAttribute('data-pattern')).toBe('blocked')
    expect(screen.getByRole('tab', { name: /same cnic/i })).toBeTruthy()
    expect(document.body.textContent).not.toContain(fullCnic)
    expect(document.body.textContent).not.toMatch(/UID 8/)
    expect(personProblems(conflictedUser).map((chip) => chip.label)).toContain('Same CNIC as 1008')
    fireEvent.click(screen.getByRole('tab', { name: /same cnic/i }))
    fireEvent.click(await screen.findByRole('button', { name: /yes, same person/i }))
    expect(await screen.findByRole('heading', { name: /are these the same person/i })).toBeTruthy()
    expect(screen.getByText(/nothing on the machine changes/i)).toBeTruthy()
    expect(screen.getByLabelText(/to confirm, type exactly: same employee/i)).toBeTruthy()
    expect(screen.getByLabelText(/your password/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(screen.getByRole('tab', { name: /people/i }))
    fireEvent.click(await screen.findByRole('button', { name: /edit ayesha fatima/i }))
    expect(
      await screen.findByLabelText(/correct cnic \(needed — same cnic as someone else\)/i),
    ).toBeTruthy()
  })

  it('saves exact HR evidence while retaining the manual attendance hold', async () => {
    const fetchMock = fetchStub([user], true)
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await selectUserTerminal()
    fireEvent.click(screen.getByRole('tab', { name: /old check-ins/i }))
    expect(await screen.findByRole('heading', { name: /old check-ins without a cnic/i })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /enter hr details/i }))
    fireEvent.change(await screen.findByLabelText(/cnic \(from hr\)/i), {
      target: { value: fullCnic },
    })
    fireEvent.change(screen.getByLabelText(/employee id in hr's records/i), {
      target: { value: '5294' },
    })
    fireEvent.change(screen.getByLabelText(/how did you check this/i), {
      target: { value: 'Verified against the authoritative HR directory.' },
    })
    fireEvent.change(screen.getByLabelText(/to confirm, type exactly: cl04196 -> hr 5294/i), {
      target: { value: 'CL04196 -> HR 5294' },
    })
    fireEvent.change(screen.getByLabelText(/your password/i), {
      target: { value: 'test-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save details/i }))

    expect(await screen.findByText(/Details saved. Next: send the not-matched check-ins/i)).toBeTruthy()
    const resolutionCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/historical-identities/resolve') &&
      (init as RequestInit | undefined)?.method === 'POST',
    )
    expect(resolutionCall).toBeTruthy()
    const requestBody = JSON.parse(
      String((resolutionCall?.[1] as RequestInit | undefined)?.body),
    )
    expect(requestBody).toMatchObject({
      source_user_key: 'historical-user-key',
      expected_version: 366,
      source_cnic: fullCnic,
      directory_employee_id: '5294',
      directory_service_number: 'CL04196',
      directory_employee_name: 'Dr Mehtab',
      typed_confirmation: 'CL04196 -> HR 5294',
    })
  })

  it('routes exact orphaned event cohorts through the versioned HR evidence endpoint', async () => {
    const fetchMock = fetchStub([user], false, true)
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await selectUserTerminal()
    fireEvent.click(screen.getByRole('tab', { name: /old check-ins/i }))
    expect((await screen.findAllByText(/no longer on this machine/i)).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /enter hr details/i }))
    fireEvent.change(await screen.findByLabelText(/cnic \(from hr\)/i), {
      target: { value: fullCnic },
    })
    fireEvent.change(screen.getByLabelText(/employee id in hr's records/i), {
      target: { value: '5294' },
    })
    fireEvent.change(screen.getByLabelText(/how did you check this/i), {
      target: { value: 'Verified exact event cohort against the HR directory.' },
    })
    fireEvent.change(screen.getByLabelText(/to confirm, type exactly: cl04999 -> hr 5294/i), {
      target: { value: 'CL04999 -> HR 5294' },
    })
    fireEvent.change(screen.getByLabelText(/your password/i), {
      target: { value: 'test-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save details/i }))

    expect(await screen.findByText(/Details saved. Next: send the not-matched check-ins/i)).toBeTruthy()
    const resolutionCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/historical-identities/resolve-event-group') &&
      (init as RequestInit | undefined)?.method === 'POST',
    )
    expect(resolutionCall).toBeTruthy()
    const requestBody = JSON.parse(
      String((resolutionCall?.[1] as RequestInit | undefined)?.body),
    )
    expect(requestBody).toMatchObject({
      group_token: 'a'.repeat(64),
      source_user_id: 'CL04999',
      source_uid: '77',
      directory_employee_id: '5294',
      directory_service_number: 'CL04999',
      directory_employee_name: 'Former Employee',
    })
  })

  it('routes linked historical cohorts to the certified current-user enrichment dialog', async () => {
    const missingIdentityUser: DeviceUser = {
      ...user,
      cnic_masked: null,
      cnic_available: false,
      identity_complete: false,
      machine_name_preview: user.display_name,
    }
    const fetchMock = fetchStub([missingIdentityUser], false, false, true)
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await selectUserTerminal()
    fireEvent.click(screen.getByRole('tab', { name: /old check-ins/i }))
    fireEvent.click(await screen.findByRole('button', { name: /add cnic/i }))
    expect(await screen.findByRole('heading', { name: /^edit /i })).toBeTruthy()
    expect(screen.getByLabelText(/cnic \(needed/i)).toBeTruthy()
  })

  it('resolves one exact legacy cohort against the unchanged current identity without an employee id', async () => {
    const currentUser: DeviceUser = {
      ...user,
      user_id: '03752',
      uid: '11',
      display_name: 'Ubaidullah',
    }
    const fetchMock = fetchStub([currentUser], false, false, false, true)
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await selectUserTerminal()
    fireEvent.click(screen.getByRole('tab', { name: /old check-ins/i }))
    fireEvent.click(
      await screen.findByRole('button', { name: /confirm same person/i }),
    )
    expect(
      await screen.findByRole('heading', {
        name: /confirm it is the same person/i,
      }),
    ).toBeTruthy()
    expect(screen.queryByLabelText(/employee id in hr's records/i)).toBeNull()
    fireEvent.change(screen.getByLabelText(/cnic \(from hr\)/i), {
      target: { value: fullCnic },
    })
    fireEvent.change(screen.getByLabelText(/how did you check this/i), {
      target: { value: 'Oracle raw capture exactly matches the current identity.' },
    })
    fireEvent.change(screen.getByLabelText(/to confirm, type exactly: 03752 -> current 03752/i), {
      target: { value: '03752 -> CURRENT 03752' },
    })
    fireEvent.change(screen.getByLabelText(/your password/i), {
      target: { value: 'test-password' },
    })
    fireEvent.click(
      screen.getByRole('button', { name: /^save$/i }),
    )

    expect(await screen.findByText(/Details saved. Next: send the not-matched check-ins/i)).toBeTruthy()
    const resolutionCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes('/historical-identities/resolve-current-identity') &&
      (init as RequestInit | undefined)?.method === 'POST',
    )
    expect(resolutionCall).toBeTruthy()
    const requestBody = JSON.parse(
      String((resolutionCall?.[1] as RequestInit | undefined)?.body),
    )
    expect(requestBody).toMatchObject({
      group_token: 'c'.repeat(64),
      source_user_id: '03752',
      source_uid: '',
      target_user_key: user.user_key,
      expected_version: user.row_version,
      source_cnic: fullCnic,
      verified_employee_name: 'Ubaidullah',
      typed_confirmation: '03752 -> CURRENT 03752',
    })
    expect(requestBody).not.toHaveProperty('directory_employee_id')
  })

})

describe('user-operation safety rules', () => {
  it('builds a valid UTF-8 24-byte machine-name projection', () => {
    const preview = buildMachinePreview('زارا State Life Employee', fullCnic, true)
    expect(new TextEncoder().encode(preview).length).toBeLessThanOrEqual(24)
    expect(preview.endsWith(`-S-${fullCnic}`)).toBe(true)
  })

  it('validates CNIC, numeric override, password, and typed deletion confirmation', () => {
    expect(
      validateUserDraft({
        displayName: 'Ayesha',
        cnic: '123',
        password: 'secret',
      }),
    ).toMatch(/13 digits/)
    expect(
      validateUserDraft({
        displayName: 'Ayesha',
        cnic: fullCnic,
        password: 'secret',
        userIdOverride: 'employee-seven',
      }),
    ).toMatch(/only have digits/)
    expect(confirmationMatches('Ayesha Fatima', user)).toBe(true)
    expect(confirmationMatches('1007', user)).toBe(true)
    expect(confirmationMatches('Ayesha', user)).toBe(false)
  })
})
