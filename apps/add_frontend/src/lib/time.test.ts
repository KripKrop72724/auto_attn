import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ago, clock, dateTime, day, dayTime, duration, elapsedSince, pakistanDay, pktInputToUtc, pktRange, pktTodayBounds,
  whenWithAgo,
} from './time'

// 6 Nov 2026, 10:42 am in Pakistan (UTC+5).
const now = new Date('2026-11-06T05:42:00Z')

afterEach(() => { vi.unstubAllEnvs() })

describe('Pakistan time helpers', () => {
  it('shows the clock in Pakistan time without seconds, whatever the browser zone', () => {
    // The helpers pass the zone to Intl, so the browser's own zone never matters.
    expect(clock('2026-11-06T05:42:31Z')).toBe('10:42 am')
    expect(clock('2026-11-06T12:05:00Z')).toBe('5:05 pm')
    expect(clock('2026-11-05T19:00:00Z')).toBe('12:00 am')
    expect(clock(null)).toBe('')
    expect(clock('not a date')).toBe('')
  })

  it('names the Pakistan calendar day, adding the year only when it is not this year', () => {
    // 7:30 pm UTC on 5 Nov is already 6 Nov in Pakistan.
    expect(pakistanDay('2026-11-05T19:30:00Z')).toBe('2026-11-06')
    expect(day('2026-11-05T19:30:00Z', now)).toBe('Fri 6 Nov')
    expect(day('2025-11-06T05:42:00Z', now)).toBe('6 Nov 2025')
  })

  it('says today, yesterday or the date with the time', () => {
    expect(dayTime('2026-11-06T04:00:00Z', now)).toBe('9:00 am')
    expect(dayTime('2026-11-05T12:41:00Z', now)).toBe('yesterday, 5:41 pm')
    expect(dayTime('2026-11-02T12:41:00Z', now)).toBe('Mon 2 Nov, 5:41 pm')
    expect(dayTime('2025-12-30T12:41:00Z', now)).toBe('30 Dec 2025, 5:41 pm')
    expect(dayTime(undefined, now)).toBe('')
  })

  it('says how long ago, never in the future', () => {
    expect(ago('2026-11-06T05:42:00Z', now)).toBe('just now')
    expect(ago('2026-11-06T05:50:00Z', now)).toBe('just now')
    expect(ago('2026-11-06T05:41:48Z', now)).toBe('12 s ago')
    expect(ago('2026-11-06T05:38:00Z', now)).toBe('4 min ago')
    expect(ago('2026-11-05T10:42:00Z', now)).toBe('19 h ago')
    expect(ago('2026-11-03T05:42:00Z', now)).toBe('3 days ago')
    expect(ago('', now)).toBe('')
    expect(whenWithAgo('2026-11-06T03:42:00Z', now)).toBe('8:42 am · 2 h ago')
  })

  it('reuses the /status duration words', () => {
    expect(duration('2026-11-06T05:41:30Z', now)).toBe('less than a minute')
    expect(duration('2026-11-06T02:22:00Z', now)).toBe('3 h 20 min')
    expect(duration('2026-11-03T05:42:00Z', now)).toBe('3 days')
    expect(duration('2026-11-07T05:42:00Z', now)).toBe('less than a minute')
  })

  it('writes Pakistan ranges on one day and across days', () => {
    expect(pktRange('2026-11-06T04:00:00Z', '2026-11-06T12:00:00Z', now)).toBe('Fri 6 Nov, 9:00 am – 5:00 pm')
    expect(pktRange('2026-11-06T04:00:00Z', '2026-11-07T12:00:00Z', now)).toBe('Fri 6 Nov, 9:00 am – Sat 7 Nov, 5:00 pm')
    expect(pktRange(null, null, now)).toBe('')
  })

  it('keeps the moved App helpers exactly as before', () => {
    expect(pktInputToUtc('2026-11-06T09:00')).toBe('2026-11-06T04:00:00.000Z')
    expect(pktInputToUtc('')).toBe('')
    expect(pktTodayBounds(new Date('2026-11-05T19:30:00Z'))).toEqual({ from: '2026-11-06T00:00', to: '2026-11-06T23:59' })
    expect(dateTime(null)).toBe('—')
    expect(elapsedSince('2026-11-06T05:05:00Z', now.getTime())).toBe('37 min')
    expect(elapsedSince('2026-11-03T05:42:00Z', now.getTime())).toBe('3 d')
    expect(elapsedSince(null, now.getTime())).toBe('')
  })
})
