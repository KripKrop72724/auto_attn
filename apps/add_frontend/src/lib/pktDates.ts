// Pakistan-time date windows for the Attendance filters (ATT-6). Pakistan has
// no daylight saving, so a Pakistan day starts at 19:00 UTC the day before,
// whatever the browser's own time zone is.
import { day, pakistanDay } from './time'

export type WhenPreset = 'today' | 'yesterday' | '7d' | 'custom'

const PKT_OFFSET_MS = 5 * 60 * 60 * 1000
const DAY_MS = 86_400_000

const parts = (key: string) => key.split('-').map((value) => Number.parseInt(value, 10)) as [number, number, number]

/** Midnight at the start of a Pakistan day ("2026-11-06"), as a UTC moment. */
export function pktDayStartUtc(key: string): Date {
  const [year, month, date] = parts(key)
  return new Date(Date.UTC(year, month - 1, date) - PKT_OFFSET_MS)
}

/** The Pakistan day `days` after `key` ("2026-11-06", -1 → "2026-11-05"). */
export function addPktDays(key: string, days: number): string {
  const [year, month, date] = parts(key)
  return new Date(Date.UTC(year, month - 1, date) + days * DAY_MS).toISOString().slice(0, 10)
}

export const todayKey = (now: Date | number = Date.now()) => pakistanDay(now)

/** Milliseconds until the next Pakistan midnight (at least 1 s). */
export function msUntilNextPktMidnight(now: Date | number = Date.now()): number {
  const current = typeof now === 'number' ? now : now.getTime()
  const next = pktDayStartUtc(addPktDays(pakistanDay(current), 1)).getTime()
  return Math.max(1000, next - current)
}

export type PktWindow = {
  /** Inclusive lower bound (ISO, UTC). */
  from_time: string
  /** Exclusive upper bound, or null for an open window that keeps the live list moving. */
  to_time: string | null
  /** "Today · Thu 6 Nov", "Yesterday · Wed 5 Nov", "31 Oct – 6 Nov". */
  label: string
  /** Whole days covered, for the 31-day totals limit. */
  days: number
}

const shortDay = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short' })
const keyLabel = (key: string) => shortDay.format(new Date(`${key}T00:00:00Z`))

/**
 * The request window for a preset. Today and Last 7 days send only the lower
 * bound, worked out now, so new check-ins keep arriving; Yesterday and picked
 * dates end at the next Pakistan midnight (exclusive).
 */
export function pktPresetRange(preset: WhenPreset, now: Date | number = Date.now(), from = '', to = ''): PktWindow {
  const today = todayKey(now)
  if (preset === 'yesterday') {
    const key = addPktDays(today, -1)
    return {
      from_time: pktDayStartUtc(key).toISOString(),
      to_time: pktDayStartUtc(today).toISOString(),
      label: `Yesterday · ${day(pktDayStartUtc(key).getTime() + PKT_OFFSET_MS, now)}`,
      days: 1,
    }
  }
  if (preset === '7d') {
    const start = addPktDays(today, -6)
    return { from_time: pktDayStartUtc(start).toISOString(), to_time: null, label: `${keyLabel(start)} – ${keyLabel(today)}`, days: 7 }
  }
  if (preset === 'custom' && /^\d{4}-\d{2}-\d{2}$/.test(from)) {
    const end = /^\d{4}-\d{2}-\d{2}$/.test(to) ? to : today
    const days = Math.round((pktDayStartUtc(end).getTime() - pktDayStartUtc(from).getTime()) / DAY_MS) + 1
    return {
      from_time: pktDayStartUtc(from).toISOString(),
      to_time: pktDayStartUtc(addPktDays(end, 1)).toISOString(),
      label: from === end ? keyLabel(from) : `${keyLabel(from)} – ${keyLabel(end)}`,
      days,
    }
  }
  return {
    from_time: pktDayStartUtc(today).toISOString(),
    to_time: null,
    label: `Today · ${day(now, now)}`,
    days: 1,
  }
}
