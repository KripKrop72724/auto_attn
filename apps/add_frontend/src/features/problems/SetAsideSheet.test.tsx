import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../ui'
import { expectPlainText } from '../../words/testing'
import { SetAsideSheet, recordFields } from './SetAsideSheet'

const item = (id: number) => ({
  id,
  receipt_id: '11111111-2222-3333-4444-555555555555',
  connector_id: 'connector-one',
  device_id: '7',
  display_name: 'ZONE-SWAT-01',
  zone_id: 'ZONE-SWAT-01',
  batch_id: 'safe-batch-id',
  item_index: 1,
  error_code: 'ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH',
  error_path: 'event_uid',
  payload_digest: 'a'.repeat(64),
  review_state: 'OPEN',
  reviewed_by: null,
  review_reason: null,
  reviewed_at: null,
  observed_at: '2026-07-13T12:05:00Z',
  evidence_available: true,
  handling: 'QUARANTINED_NON_BLOCKING',
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
let open = [item(91)]
let revealGate: Promise<void> | null = null

describe('SetAsideSheet', () => {
  beforeEach(() => {
    open = [item(91)]
    revealGate = null
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.pathname.endsWith('/reveal') && init?.method === 'POST') {
        if (revealGate) await revealGate
        return json({ id: 91, payload_digest: 'a'.repeat(64), error_code: null, error_path: null, payload: { user_id: '1007', timestamp: '2026-07-13 17:05:00', terminal_value: 'protected-attendance-evidence' } })
      }
      if (url.pathname.endsWith('/review') && init?.method === 'POST') {
        open = []
        return json({ id: 91, review_state: 'REVIEWED' })
      }
      if (url.pathname === '/api/v1/attendance-quarantine') return json({ totals: { all: 1, open: open.length }, filtered_total: open.length, rows: open, next_cursor: null })
      throw new Error(`Unexpected request: ${url.pathname}`)
    }))
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const openSheet = (onChanged = vi.fn()) => {
    render(<ToastProvider><SetAsideSheet deviceId="connector-one" place="Swat" division="health" onClose={vi.fn()} onChanged={onChanged} /></ToastProvider>)
    return onChanged
  }

  const fill = () => {
    fireEvent.change(screen.getByLabelText('What did you find?'), { target: { value: 'Record is damaged, nothing to send.' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'local-quarantine-step-up' } })
  }

  it('lists the device’s set-aside check-ins in plain words', async () => {
    openSheet()
    const sheet = await screen.findByRole('dialog', { name: 'Check-ins set aside for checking' })
    expect(await within(sheet).findByText('A value in the record could not be read')).toBeTruthy()
    expect(within(sheet).getByText(/Swat · 1 to check/)).toBeTruthy()
    const request = vi.mocked(fetch).mock.calls.map(([input]) => new URL(String(input), 'http://localhost'))[0]
    expect(request.searchParams.get('device_id')).toBe('connector-one')
    expect(request.searchParams.get('review_state')).toBe('OPEN')
    expect(request.searchParams.get('limit')).toBe('50')
    expectPlainText(sheet)
  })

  it('keeps separate busy labels, a countdown and hides the record when the page goes to the background', async () => {
    let release: () => void = () => undefined
    revealGate = new Promise((resolve) => { release = resolve })
    openSheet()
    fireEvent.click(await screen.findByRole('button', { name: 'Check' }))
    expect(screen.getByRole('heading', { name: 'Check a set-aside check-in' })).toBeTruthy()
    expect(screen.getByText('ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH').closest('[data-technical]')).toBeTruthy()
    fill()
    fireEvent.click(screen.getByRole('button', { name: 'Show original record' }))
    expect(await screen.findByRole('button', { name: /Checking password…/ })).toBeTruthy()
    expect(screen.queryByText('Saving…')).toBeNull()
    expect(screen.getByRole('button', { name: 'Mark as checked' })).toBeTruthy()
    release()
    expect(await screen.findByText(/Hides in 60 s/)).toBeTruthy()
    expect(screen.getByText('1007')).toBeTruthy()
    expect(screen.getByText(/protected-attendance-evidence/)).toBeTruthy()
    const body = JSON.parse(String(vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/reveal'))?.[1]?.body))
    expect(body.idempotency_key).toMatch(/^attendance-quarantine-reveal:/)
    expect(document.body.textContent).not.toContain('local-quarantine-step-up')
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(screen.queryByText(/protected-attendance-evidence/)).toBeNull()
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
  })

  it('marks the last one as checked, says All checked and reloads the problems', async () => {
    const onChanged = openSheet()
    fireEvent.click(await screen.findByRole('button', { name: 'Check' }))
    fill()
    fireEvent.click(screen.getByRole('button', { name: 'Mark as checked' }))
    expect(await screen.findAllByText('All checked.')).toBeTruthy()
    const review = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/attendance-quarantine/91/review'))
    expect(JSON.parse(String(review?.[1]?.body)).idempotency_key).toMatch(/^attendance-quarantine-review:/)
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('reads the known fields of the original record', () => {
    expect(recordFields({ user_id: '1007', timestamp: '2026-07-13 17:05:00', nested: { a: 1 } })).toEqual([
      { label: 'Employee ID', value: '1007' },
      { label: 'Time', value: '2026-07-13 17:05:00' },
    ])
    expect(recordFields('nope')).toEqual([])
  })
})
