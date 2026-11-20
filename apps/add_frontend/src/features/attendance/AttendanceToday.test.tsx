import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AttendanceToday } from './AttendanceToday'
import type { SummaryState } from './useAttendanceSummary'

const totals = { received: 3412, sent: 3300, waiting: 80, trying_again: 12, not_matched: 28, set_aside: 3, problem: 1, oldest_waiting_at: '2026-11-06T04:12:00Z' }
const data = {
  from_time: '2026-11-05T19:00:00Z', to_time: null, computed_at: '2026-11-06T07:42:10Z', max_age_seconds: 15, totals, last_sent_at: null,
  by_division: { life: { ...totals, received: 1234 }, health: { ...totals, received: 2178 } },
}
const state = (overrides: Partial<SummaryState> = {}): SummaryState => ({ data, loading: false, failed: false, tooLong: false, retry: vi.fn(async () => undefined), ...overrides })
const now = Date.parse('2026-11-06T07:42:20Z')

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe("today's totals", () => {
  it('shimmers while loading and never shows a fake 0', () => {
    const { container } = render(<AttendanceToday state={state({ data: null, loading: true })} division="all" chosen={[]} onChoose={vi.fn()} rangeLabel="Today · Fri 6 Nov" now={now} />)
    expect(container.textContent).not.toMatch(/\b0\b/)
    expect(container.querySelectorAll('.ui-skeleton').length).toBeGreaterThan(0)
  })

  it('shows a dash and Try again when the totals fail, while the list keeps working', () => {
    const retry = vi.fn(async () => undefined)
    render(<AttendanceToday state={state({ data: null, failed: true, retry })} division="all" chosen={[]} onChoose={vi.fn()} rangeLabel="Today · Fri 6 Nov" now={now} />)
    expect(screen.getByRole('alert').textContent).toContain("Couldn't load today's totals.")
    expect(screen.getAllByText('–').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(retry).toHaveBeenCalled()
  })

  it('splits Received into Life and Health on All, and sets the status from a tile', () => {
    const onChoose = vi.fn()
    render(<AttendanceToday state={state()} division="all" chosen={[]} onChoose={onChoose} rangeLabel="Today · Fri 6 Nov" now={now} />)
    const region = screen.getByRole('region', { name: "Today's totals" })
    expect(within(region).getByText('Life 1,234 · Health 2,178')).toBeTruthy()
    expect(within(region).getByText("97% of today's")).toBeTruthy()
    expect(within(region).getByText(/Oldest since 9:12 am/)).toBeTruthy()
    expect(within(region).getByText(/12 trying again/)).toBeTruthy()
    expect(within(region).getByText('Refused by the HR system')).toBeTruthy()
    fireEvent.click(within(region).getByRole('button', { name: /^Not matched/ }))
    expect(onChoose).toHaveBeenCalledWith('not_matched')
  })

  it('says check-ins and check-outs when one division is chosen, and limits totals to 31 days', () => {
    const view = render(<AttendanceToday state={state()} division="life" chosen={['waiting']} onChoose={vi.fn()} rangeLabel="1 Nov – 4 Nov" now={now} />)
    expect(screen.getByText('check-ins and check-outs')).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Totals for 1 Nov – 4 Nov' })).toBeTruthy()
    view.rerender(<AttendanceToday state={state({ data: null, tooLong: true })} division="life" chosen={[]} onChoose={vi.fn()} rangeLabel="1 Oct – 4 Nov" now={now} />)
    expect(screen.getByRole('note').textContent).toBe('Totals are shown for up to 31 days. Pick a shorter range to see them.')
  })

  it('shows a Today card and sideways status chips on a phone', () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('max-width: 760px'), media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    const onChoose = vi.fn()
    render(<AttendanceToday state={state()} division="all" chosen={['waiting']} onChoose={onChoose} rangeLabel="Today · Fri 6 Nov" now={now} />)
    expect(screen.getByText('3,412 received')).toBeTruthy()
    expect(screen.getByText('97% sent')).toBeTruthy()
    const chips = screen.getByRole('group', { name: 'Status' })
    expect(within(chips).getByRole('button', { name: /Waiting/ }).getAttribute('aria-pressed')).toBe('true')
    expect(within(chips).getByRole('button', { name: /Problem/ })).toBeTruthy()
    fireEvent.click(within(chips).getByRole('button', { name: /Waiting/ }))
    expect(onChoose).toHaveBeenCalledWith(null)
  })
})
