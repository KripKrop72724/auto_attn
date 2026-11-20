import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TechnicalDetailsProvider } from '../../ui'
import { expectPlainText } from '../../words/testing'
import { ProblemCard, canMarkFixed, fixedLine, seenLine } from './ProblemCard'
import { inspectorPath, problemLink } from './problemLinks'
import type { ProblemRow } from './types'

const row = (overrides: Partial<ProblemRow> = {}): ProblemRow => ({
  id: 812,
  code: 'ESP_DURABILITY_FAULT',
  severity: 'HIGH',
  state: 'OPEN',
  message: 'Attendance preservation needs recovery: storage DEGRADED; 0.0% used.',
  details: { firmware_version: '3.1.0', boot_id: 'ac276ea4-0000-4000-8000-000000000001', binding: 'OBSERVED' },
  first_seen_at: '2026-10-30T04:24:00Z',
  last_seen_at: '2026-10-30T07:42:00Z',
  acknowledged_at: null,
  acknowledged_by: null,
  resolved_at: null,
  resolution: null,
  plain_title: 'Link box cannot save attendance safely',
  plain_detail: 'Its memory has a problem, so check-ins could be lost if the power or internet goes off.',
  next_step: 'Do not switch the link box off. Call IT.',
  operator: { resolvable: true, refusal_code: null, refusal: null },
  device: { connector_id: 'connector-bld9-03', display_name: 'LF-ZONE-BLD9-03', zone_id: 'LF-ZONE-BLD9-03', hardware_id: 'ac:27:6e:a3:45:44', division: 'life', friendly_name: 'Building 9 · Device 3' },
  ...overrides,
})

let path = ''
function Probe() {
  path = `${useLocation().pathname}${useLocation().search}`
  return null
}

const renderCard = (props: Partial<Parameters<typeof ProblemCard>[0]> = {}, technical = false) => render(
  <MemoryRouter initialEntries={['/problems?division=life']}>
    <TechnicalDetailsProvider show={technical}>
      <Routes>
        <Route path="/problems" element={<ProblemCard row={row()} showDevice showOpenDevice onMarkSeen={vi.fn()} onMarkFixed={vi.fn()} {...props} />} />
        <Route path="*" element={<Probe />} />
      </Routes>
    </TechnicalDetailsProvider>
  </MemoryRouter>,
)

describe('ProblemCard', () => {
  afterEach(() => cleanup())

  it('shows every line in plain words, in order, with the code only in Technical details', () => {
    const { container } = renderCard()
    const card = screen.getByRole('article', { name: 'Link box cannot save attendance safely' })
    const text = card.textContent || ''
    const order = ['Important', 'New', 'Link box cannot save attendance safely', 'Building 9 · Device 3', 'Its memory has a problem', 'What to do:', 'Started', 'Closes by itself when the link box confirms']
    const positions = order.map((part) => text.indexOf(part))
    expect(positions.every((value) => value >= 0)).toBe(true)
    expect([...positions].sort((a, b) => a - b)).toEqual(positions)
    expect(within(card).getByLabelText('How serious: Important')).toBeTruthy()
    expect(within(card).getByText('Life')).toBeTruthy()
    const technical = card.querySelector('[data-technical]') as HTMLDetailsElement
    expect(technical.open).toBe(false)
    expect(within(technical).getByText('ESP_DURABILITY_FAULT')).toBeTruthy()
    expect(within(technical).getByText(/storage DEGRADED/)).toBeTruthy()
    expect(within(technical).getByText('#812')).toBeTruthy()
    expect(within(technical).getByText('ac:27:6e:a3:45:44')).toBeTruthy()
    expectPlainText(container)
  })

  it('opens Technical details when the preference is on', () => {
    renderCard({}, true)
    const card = screen.getByRole('article', { name: 'Link box cannot save attendance safely' })
    expect((card.querySelector('[data-technical]') as HTMLDetailsElement).open).toBe(true)
  })

  it('never offers Mark as fixed when the backend refuses it, and says how it closes instead', () => {
    const { container } = renderCard({ row: row({ code: 'ESP_PRESERVATION_UNVERIFIED', plain_title: 'Link box memory not checked yet', operator: { resolvable: false, refusal_code: 'ALERT_CONDITION_CURRENT', refusal: 'Held on this boot.' } }) })
    const card = screen.getByRole('article', { name: 'Link box memory not checked yet' })
    expect(within(card).queryByRole('button', { name: /Mark as fixed/ })).toBeNull()
    expect(within(card).getByText(/^Closes after a software update that can check the link box memory\. Closes by itself only\.$/)).toBeTruthy()
    expectPlainText(container)
  })

  it('falls back to the code alone when the backend sends no close rule', () => {
    expect(canMarkFixed(row({ operator: undefined }))).toBe(true)
    expect(canMarkFixed(row({ operator: undefined, code: 'ORDS_DELIVERY_FAILED' }))).toBe(false)
    expect(canMarkFixed(row({ state: 'RESOLVED' }))).toBe(false)
  })

  it('never shows the system message as the title', () => {
    renderCard({ row: row({ plain_title: null, plain_detail: null, next_step: null, code: 'SOMETHING_NEW' }) })
    expect(screen.getByRole('heading', { name: 'Link box problem' })).toBeTruthy()
    expect(screen.getByText(/storage DEGRADED/).closest('[data-technical]')).toBeTruthy()
  })

  it('opens other pages through the router and keeps the division', () => {
    renderCard({ row: row({ code: 'OTA_DEVICE_ROLLED_BACK', plain_title: 'Software update undone' }) })
    fireEvent.click(screen.getByRole('link', { name: /Open Software updates/ }))
    expect(path).toBe('/updates?tab=updates&device=LF-ZONE-BLD9-03&division=life')
    expect(screen.queryByRole('article')).toBeNull()
  })

  it('opens the set-aside sheet with Check them', () => {
    const onSetAside = vi.fn()
    renderCard({ row: row({ code: 'ATTENDANCE_EVENT_QUARANTINED', plain_title: '3 check-ins set aside for checking' }), onSetAside })
    fireEvent.click(screen.getByRole('button', { name: /Check them/ }))
    expect(onSetAside).toHaveBeenCalledTimes(1)
  })

  it('keeps the inspector allow-list: only Recover attendance paths become links', () => {
    expect(inspectorPath({ inspector_path: '/reconciliation?tab=source-exceptions&device_id=abc-1' })).toBe('/recover?tab=unreadable&device_id=abc-1')
    expect(inspectorPath({ inspector_path: '/recover?tab=unreadable&device_id=abc-1' })).toBe('/recover?tab=unreadable&device_id=abc-1')
    expect(inspectorPath({ inspector_path: 'https://evil.example/' })).toBeNull()
    expect(inspectorPath({ inspector_path: '/recover?tab=unreadable&device_id=a"b' })).toBeNull()
    expect(problemLink(row({ code: 'TERMINAL_SOURCE_EXCEPTION', details: { inspector_path: '//evil.example' } }))).toBeNull()
    expect(problemLink(row({ code: 'TERMINAL_SOURCE_EXCEPTION', details: { inspector_path: '/reconciliation?tab=source-exceptions&device_id=x1' } }))).toEqual({ kind: 'link', label: 'See the records', to: '/recover?tab=unreadable&device_id=x1' })
  })

  it('shows busy only on the clicked card, and the inline error', () => {
    renderCard({ busy: 'seen', rowError: 'failed' })
    expect(screen.getByRole('button', { name: /Marking…/ })).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toBe("Couldn't mark it as seen. Try again.")
  })

  it('says seen and fixed once, in plain words', () => {
    const seen = row({ acknowledged_at: '2026-10-30T05:02:00Z', acknowledged_by: 'Sara', details: { acknowledged_by: 'Sara', acknowledgement_note: 'Power back at 2 pm' } })
    expect(seenLine(seen, 'Sara').head).toMatch(/^Seen by you · /)
    expect(seenLine(seen, 'Ali').head).toMatch(/^Seen by Sara · /)
    expect(seenLine(seen).note).toBe('Power back at 2 pm')
    expect(fixedLine(row({ state: 'RESOLVED', resolved_at: '2026-10-30T06:05:00Z', resolution: { kind: 'OPERATOR', actor: 'Sara', reason: 'Cable replaced at the branch', at: '2026-10-30T06:05:00Z' } }))).toEqual({ line: 'Marked fixed by Sara at 11:05 am', reason: 'Cable replaced at the branch' })
    expect(fixedLine(row({ state: 'RESOLVED', resolved_at: '2026-10-30T07:44:00Z' })).line).toBe('Fixed by itself at 12:44 pm · lasted 3 h 20 min')
    renderCard({ row: row({ state: 'RESOLVED', resolved_at: '2026-10-30T07:44:00Z' }) })
    const card = screen.getByRole('article')
    expect(within(card).getAllByText(/Fixed/).length).toBe(2)
    expect(within(card).queryByRole('button', { name: /Mark as/ })).toBeNull()
  })
})
