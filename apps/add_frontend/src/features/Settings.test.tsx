import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from '../App'
import { getPrefs, resetPrefs } from '../lib/prefs'
import { appReady, fetchStub, response, useDashboardStubs } from '../test/renderApp'
import { expectPlainText } from '../words/testing'

const notifySettings = {
  state: 'OK', configured: true, mode: 'ALL', punch_policy: 'ALL_PUNCHES', pilot_zone_count: 0, pilot_cnic_count: 0,
  last_success_at: null, last_failure_at: null, last_failure_category: null, consecutive_failures: 0, preferred_host: 'PRIMARY',
  primary_reprobe_at: null, paused_until: null, oldest_pending_at: null, oldest_pending_age_seconds: 0, started_at: null,
  last_24h: { notified: 12, pending: 0, retrying: 0, failed: 0, not_sent: 0, not_sent_reasons: {}, window_hours: 24 },
  revision: 1, pilot_zone_ids: [], pilot_cnics: [], updated_by: null, updated_at: null, endpoint_host: null, fallback_host: null,
  cnic_json_type: 'number', stale_window_hours: 12, coalesce_seconds: 5, retry_window_hours: 12, modes: ['OFF', 'PILOT', 'ALL'], punch_policies: ['ALL_PUNCHES', 'FIRST_PER_DAY'],
}

describe('Settings', () => {
  useDashboardStubs(() => import('./Settings'))

  const open = async () => {
    resetPrefs()
    const base = fetchStub()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input) === '/api/v1/checkin-notifications' ? response(notifySettings) : base(input, init)))
    window.history.replaceState({}, '', '/settings')
    render(<App />)
    await appReady('Settings')
  }

  it('switches technical details on and saves only that choice', async () => {
    await open()
    const toggle = screen.getByRole('switch', { name: /Show technical details/ })
    expect((toggle as HTMLInputElement).checked).toBe(false)
    fireEvent.click(toggle)
    expect(getPrefs().technical).toBe(true)
    expect(Object.keys(window.localStorage)).toEqual(['add.prefs'])
    resetPrefs()
  })

  it('holds the phone messages, contact IT, the old screens link and the account, with no Division switch', async () => {
    await open()
    expect(await screen.findByRole('heading', { name: 'Check-in phone messages' })).toBeTruthy()
    const card = screen.getByRole('region', { name: 'Check-in phone messages' })
    await waitFor(() => expect(card.querySelector('.notify-card-counts')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Change settings' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Contact IT' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /Open the old screens/ }).getAttribute('href')).toBe('/classic/')
    expect(screen.getByText('Signed in as StateHealthAdmin')).toBeTruthy()
    expect(screen.queryByRole('radiogroup', { name: 'Division' })).toBeNull()
    expectPlainText(document.querySelector('.settings-page .ui-page-header') as HTMLElement)
  })
})
