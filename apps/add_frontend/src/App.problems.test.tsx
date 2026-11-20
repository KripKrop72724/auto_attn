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
  conflictedUser, deliveryAlert, device, fetchStub, fullCnic, maskedCnic, response, selectUserTerminal, useDashboardStubs, user, appReady, openMenuPage,
} from './test/renderApp'

describe('State Life ADD interface · Problems', () => {
  useDashboardStubs(() => Promise.all([import('./features/Home'), import('./features/Monitoring')]))

  it('renders only allowlisted alert diagnostics', async () => {
    render(<App />)
    await appReady()
    openMenuPage('Problems')
    expect(await screen.findByText(/Category HTTP_503 · HTTP 503 · Attempt 4/i)).toBeTruthy()
    expect(await screen.findByRole('heading', { name: /attendance quarantine/i })).toBeTruthy()
    expect(screen.getByText(/ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH · event_uid/i)).toBeTruthy()
    expect(screen.getByText(/valid and newer punches continue/i)).toBeTruthy()
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

  it('requires audited step-up to reveal and review attendance quarantine', async () => {
    render(<App />)
    await appReady()
    openMenuPage('Problems')
    fireEvent.click(await screen.findByRole('button', { name: /^review$/i }))
    expect(await screen.findByRole('heading', { name: /review quarantined attendance/i })).toBeTruthy()

    const reason = screen.getByLabelText(/audited reason/i)
    const password = screen.getByLabelText(/administrator password/i)
    fireEvent.change(reason, { target: { value: 'Inspect retained malformed source evidence.' } })
    fireEvent.change(password, { target: { value: 'local-quarantine-step-up' } })
    fireEvent.click(screen.getByRole('button', { name: /reveal evidence/i }))
    expect(await screen.findByText(/protected-attendance-evidence/i)).toBeTruthy()
    const revealCall = vi.mocked(fetch).mock.calls.find(([path, init]) =>
      String(path).endsWith('/api/v1/attendance-quarantine/91/reveal') && init?.method === 'POST')
    expect(revealCall).toBeTruthy()
    expect(JSON.parse(String(revealCall?.[1]?.body)).idempotency_key).toMatch(/^attendance-quarantine-reveal:/)
    expect(document.body.textContent).not.toContain('local-quarantine-step-up')
    fireEvent.click(screen.getByRole('button', { name: /hide now/i }))
    expect(screen.queryByText(/protected-attendance-evidence/i)).toBeNull()

    fireEvent.change(reason, { target: { value: 'Confirmed malformed source evidence.' } })
    fireEvent.change(password, { target: { value: 'local-quarantine-step-up' } })
    fireEvent.click(screen.getByRole('button', { name: /mark reviewed/i }))
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([path, init]) =>
      String(path).endsWith('/api/v1/attendance-quarantine/91/review') && init?.method === 'POST')).toBe(true))
    const reviewCall = vi.mocked(fetch).mock.calls.find(([path, init]) =>
      String(path).endsWith('/api/v1/attendance-quarantine/91/review') && init?.method === 'POST')
    expect(JSON.parse(String(reviewCall?.[1]?.body)).idempotency_key).toMatch(/^attendance-quarantine-review:/)
    expect(screen.queryByRole('heading', { name: /review quarantined attendance/i })).toBeNull()
  })

})
