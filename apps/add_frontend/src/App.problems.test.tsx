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
  conflictedUser, deliveryAlert, device, fetchStub, fullCnic, maskedCnic, response, selectUserTerminal, useDashboardStubs, user, appReady, openMenuPage,
} from './test/renderApp'

describe('State Life ADD interface · Problems', () => {
  useDashboardStubs(() => Promise.all([import('./features/Home'), import('./features/Problems')]))

  const quarantineAlert = {
    ...deliveryAlert,
    id: 10,
    code: 'ATTENDANCE_EVENT_QUARANTINED',
    message: '1 attendance row quarantined for review.',
    details: { open_items: 1 },
    plain_title: '1 check-in set aside for checking',
  }
  const withQuarantineAlert = () => {
    const base = vi.mocked(fetch).getMockImplementation()!
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.startsWith('/api/v1/alerts') && init?.method !== 'POST') {
        const answer = await base(input, init)
        const body = await answer.json()
        body.rows = [...body.rows, { ...quarantineAlert, device: body.rows[0].device }]
        return response(body)
      }
      return base(input, init)
    }))
  }
  const openProblems = async () => {
    render(<App />)
    await appReady()
    openMenuPage('Problems')
    await screen.findByRole('heading', { name: 'Problems', level: 1 })
  }

  it('renders only allowlisted problem details, and only inside Technical details', async () => {
    await openProblems()
    const evidence = await screen.findByText(/Category HTTP_503 · HTTP 503 · Attempt 4/i)
    expect(evidence.closest('[data-technical]')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Check-ins not reaching the HR system (trying again)' })).toBeTruthy()
    expect(document.body.textContent).not.toContain('must-not-render')
    expect(document.body.textContent).not.toContain(fullCnic)
    // Every Technical details box open: still nothing unsafe.
    document.querySelectorAll('details').forEach((element) => { element.open = true })
    expect(document.body.textContent).not.toContain('must-not-render')
    expect(document.body.textContent).not.toContain(fullCnic)
    expect(formatAlertDiagnostics({ failure_category: 'unsafe detail!', secret: 'x' })).toBe('')
  })

  it('renders bounded device-health evidence, binding, latch, rejection types, and resolution', () => {
    expect(formatAlertDiagnostics({
      diagnostics_schema_version: 2,
      boot_id: '1a2b3c4d-5e6f-4a1b-8c2d-0123456789ab',
      firmware_version: '2.5.2',
      binding: 'INFERRED_PREVIOUS',
      evidence: { summary: 'add_delivery STOPPED · restarts 3', workers: [{ name: 'add_delivery', state: 'STOPPED' }] },
      latch: { kind: 'LED_LATCH_NO_IO_ERRORS', source: 'add_connector.c:3764', firmware_version: '2.6.15' },
      resolution: { kind: 'OPERATOR', actor: 'StateHealthAdmin', reason: 'Stranded by the recovery image.', at: '2026-10-10T08:00:00Z' },
    })).toBe(
      'Evidence: add_delivery STOPPED · restarts 3 · Firmware 2.5.2 · boot 1a2b3c4d · Inferred on an earlier boot · '
      + 'LED latch: no I/O errors this boot · Resolved by StateHealthAdmin',
    )
    expect(formatAlertDiagnostics({
      message_type: 'log',
      error_type: 'ValidationError',
      error_category: 'SCHEMA_INVALID',
      types: { heartbeat: { count: 2 }, queue_evidence: { count: 1 }, 'not a type!': {} },
      resolution: { kind: 'MESSAGE_ACCEPTED', at: '2026-10-10T08:00:00Z' },
    })).toBe('Rejected heartbeat, queue_evidence · Resolved: message accepted')
    expect(formatAlertDiagnostics({ message_type: 'heartbeat', error_category: 'DATABASE_UNAVAILABLE' })).toBe('Rejected heartbeat')
    expect(formatAlertDiagnostics({ message_type: 'heartbeat' })).toBe('')
    expect(formatAlertDiagnostics({ evidence: { summary: 'x'.repeat(400) } })).toBe(`Evidence: ${'x'.repeat(300)}`)
    expect(formatAlertDiagnostics({ firmware_version: '<script>', boot_id: 'bad boot', binding: 'lower', latch: 'LED' })).toBe('')
  })

  it('keeps the password to show and to check a set-aside check-in, from the problem card', async () => {
    withQuarantineAlert()
    await openProblems()
    const card = await screen.findByRole('article', { name: '1 check-in set aside for checking' })
    fireEvent.click(within(card).getByRole('button', { name: /Check them/ }))
    const sheet = await screen.findByRole('dialog', { name: 'Check-ins set aside for checking' })
    fireEvent.click(await within(sheet).findByRole('button', { name: /^check$/i }))
    expect(await within(sheet).findByRole('heading', { name: 'Check a set-aside check-in' })).toBeTruthy()
    expect(within(sheet).getByText(/ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH/).closest('[data-technical]')).toBeTruthy()

    const reason = within(sheet).getByLabelText('What did you find?')
    const password = within(sheet).getByLabelText('Enter your password to confirm')
    fireEvent.change(reason, { target: { value: 'Inspect retained malformed source evidence.' } })
    fireEvent.change(password, { target: { value: 'local-quarantine-step-up' } })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Show original record' }))
    expect(await screen.findByText(/protected-attendance-evidence/i)).toBeTruthy()
    const revealCall = vi.mocked(fetch).mock.calls.find(([path, init]) =>
      String(path).endsWith('/api/v1/attendance-quarantine/91/reveal') && init?.method === 'POST')
    expect(revealCall).toBeTruthy()
    expect(JSON.parse(String(revealCall?.[1]?.body)).idempotency_key).toMatch(/^attendance-quarantine-reveal:/)
    expect(document.body.textContent).not.toContain('local-quarantine-step-up')
    fireEvent.click(within(sheet).getByRole('button', { name: 'Hide' }))
    expect(screen.queryByText(/protected-attendance-evidence/i)).toBeNull()

    fireEvent.change(reason, { target: { value: 'Confirmed malformed source evidence.' } })
    fireEvent.change(password, { target: { value: 'local-quarantine-step-up' } })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Mark as checked' }))
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([path, init]) =>
      String(path).endsWith('/api/v1/attendance-quarantine/91/review') && init?.method === 'POST')).toBe(true))
    const reviewCall = vi.mocked(fetch).mock.calls.find(([path, init]) =>
      String(path).endsWith('/api/v1/attendance-quarantine/91/review') && init?.method === 'POST')
    expect(JSON.parse(String(reviewCall?.[1]?.body)).idempotency_key).toMatch(/^attendance-quarantine-review:/)
    await waitFor(() => expect(within(sheet).queryByRole('heading', { name: 'Check a set-aside check-in' })).toBeNull())
  })

})
