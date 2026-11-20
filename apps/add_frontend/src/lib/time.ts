// Every time on the dashboard is Pakistan time, whatever the browser's own time
// zone is. Writing rule 5: "10:42 am · 2 h ago"; dates "Thu 6 Nov", with the year
// only when it is not this year.
import { formatDuration } from '../publicStatus'

export const PAKISTAN_TIME_ZONE = 'Asia/Karachi'

type TimeInput = string | number | Date | null | undefined

const toDate = (value: TimeInput): Date | null => {
  if (value === null || value === undefined || value === '') return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

const clockFormat = new Intl.DateTimeFormat('en-US', { timeZone: PAKISTAN_TIME_ZONE, hour: 'numeric', minute: '2-digit' })
const dayKeyFormat = new Intl.DateTimeFormat('en-CA', { timeZone: PAKISTAN_TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' })
const dayFormat = new Intl.DateTimeFormat('en-GB', { timeZone: PAKISTAN_TIME_ZONE, weekday: 'short', day: 'numeric', month: 'short' })
const dayYearFormat = new Intl.DateTimeFormat('en-GB', { timeZone: PAKISTAN_TIME_ZONE, day: 'numeric', month: 'short', year: 'numeric' })

/** "10:42 am" in Pakistan time, no seconds. Empty for a missing or broken value. */
export function clock(value: TimeInput): string {
  const date = toDate(value)
  return date ? clockFormat.format(date).replace(/\s?AM$/, ' am').replace(/\s?PM$/, ' pm') : ''
}

/** "2026-11-06": the Pakistan calendar day of a moment. */
export function pakistanDay(value: TimeInput): string {
  const date = toDate(value)
  return date ? dayKeyFormat.format(date) : ''
}

/** "Thu 6 Nov", or "6 Nov 2025" when it is not this year. */
export function day(value: TimeInput, now: TimeInput = Date.now()): string {
  const date = toDate(value)
  if (!date) return ''
  const sameYear = pakistanDay(date).slice(0, 4) === pakistanDay(now).slice(0, 4)
  return (sameYear ? dayFormat : dayYearFormat).format(date).replace(',', '')
}

/** "10:42 am", "yesterday, 5:41 pm", "Thu 6 Nov, 5:41 pm" or "6 Nov 2025, 5:41 pm". */
export function dayTime(value: TimeInput, now: TimeInput = Date.now()): string {
  const date = toDate(value)
  const current = toDate(now) || new Date()
  if (!date) return ''
  const time = clock(date)
  const key = pakistanDay(date)
  if (key === pakistanDay(current)) return time
  if (key === pakistanDay(current.getTime() - 86_400_000)) return `yesterday, ${time}`
  return `${day(date, current)}, ${time}`
}

/**
 * "just now", "12 s ago", "4 min ago", "19 h ago", "3 days ago". A time in the
 * future (a browser clock behind the server) is "just now", never "in 2 minutes".
 */
export function ago(value: TimeInput, now: TimeInput = Date.now()): string {
  const date = toDate(value)
  const current = toDate(now) || new Date()
  if (!date) return ''
  const seconds = Math.floor((current.getTime() - date.getTime()) / 1000)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds} s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} h ago`
  const days = Math.floor(hours / 24)
  return `${days} days ago`
}

/** "less than a minute", "12 min", "3 h 20 min", "1 day 4 h", "3 days": time since a moment. */
export function duration(since: TimeInput, now: TimeInput = Date.now()): string {
  const date = toDate(since)
  const current = toDate(now) || new Date()
  return date ? formatDuration(date.toISOString(), current) : ''
}

/** "10:42 am · 2 h ago", the usual way to show when something happened. */
export function whenWithAgo(value: TimeInput, now: TimeInput = Date.now()): string {
  const date = toDate(value)
  return date ? `${dayTime(date, now)} · ${ago(date, now)}` : ''
}

/**
 * A Pakistan-time range: "Thu 6 Nov, 9:00 am – 5:00 pm" on one day, or
 * "Thu 6 Nov, 9:00 am – Fri 7 Nov, 5:00 pm" across days.
 */
export function pktRange(from: TimeInput, to: TimeInput, now: TimeInput = Date.now()): string {
  const start = toDate(from)
  const end = toDate(to)
  if (!start && !end) return ''
  if (!start) return `until ${day(end, now)}, ${clock(end)}`
  if (!end) return `from ${day(start, now)}, ${clock(start)}`
  const startText = `${day(start, now)}, ${clock(start)}`
  return pakistanDay(start) === pakistanDay(end)
    ? `${startText} – ${clock(end)}`
    : `${startText} – ${day(end, now)}, ${clock(end)}`
}

// ---------------------------------------------------------------------------
// Helpers moved from App.tsx (DLV-6). Their output is unchanged so every page
// keeps its words until that page's own restyle switches to the helpers above.

export const dateTime = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat('en-PK', {
        dateStyle: 'medium',
        timeStyle: 'medium',
        timeZone: PAKISTAN_TIME_ZONE,
      }).format(new Date(value))
    : '—'

export const relativeTime = (value?: string | null) => {
  if (!value) return 'Never seen'
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 48) return formatter.format(hours, 'hour')
  return formatter.format(Math.round(hours / 24), 'day')
}

export const pktInputToUtc = (value: string) => value ? new Date(`${value}:00+05:00`).toISOString() : ''

export const pktTodayBounds = (now = new Date()) => {
  const today = pakistanDay(now)
  return { from: `${today}T00:00`, to: `${today}T23:59` }
}

// Compact elapsed time for reason lines: '37 min', '5 h 12 min', '3 d'.
export function elapsedSince(value?: string | null, now = Date.now()) {
  const started = value ? Date.parse(value) : NaN
  if (!Number.isFinite(started)) return ''
  const minutes = Math.floor(Math.max(0, now - started) / 60_000)
  if (minutes < 1) return 'under 1 min'
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`
  return `${Math.floor(hours / 24)} d`
}
