// Small time helpers the words modules need for sentences such as
// "Offline since 10:42 am · 19 h". Pakistan time, rule 5. The Foundation's
// fe/lib/time.ts (FND-4) is the app-wide helper; once it lands these can
// re-export from there. Kept here so fe/words has no dependency on pages.

const TIME_ZONE = 'Asia/Karachi'

const CLOCK = new Intl.DateTimeFormat('en-GB', { timeZone: TIME_ZONE, hour: 'numeric', minute: '2-digit', hour12: true })
const DAY = new Intl.DateTimeFormat('en-GB', { timeZone: TIME_ZONE, weekday: 'short', day: 'numeric', month: 'short' })
const DAY_YEAR = new Intl.DateTimeFormat('en-GB', { timeZone: TIME_ZONE, weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })
const YEAR = new Intl.DateTimeFormat('en-GB', { timeZone: TIME_ZONE, year: 'numeric' })
const DATE_KEY = new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' })

const toDate = (value: string | number | Date | null | undefined): Date | null => {
  if (value === null || value === undefined || value === '') return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** "10:42 am" in Pakistan time. */
export function clockText(value: string | number | Date | null | undefined): string {
  const date = toDate(value)
  if (!date) return ''
  return CLOCK.format(date).replace(/\s?([AP])M$/i, (_, half: string) => ` ${half.toLowerCase()}m`).replace(/^0(?=\d:)/, '')
}

/** "Thu 6 Nov", with the year only when it is not this year. */
export function dayText(value: string | number | Date | null | undefined, now: Date = new Date()): string {
  const date = toDate(value)
  if (!date) return ''
  const format = YEAR.format(date) === YEAR.format(now) ? DAY : DAY_YEAR
  return format.format(date).replace(',', '')
}

/** "10:42 am" today, "yesterday, 5:41 pm", or "Thu 6 Nov, 5:41 pm". */
export function dayTimeText(value: string | number | Date | null | undefined, now: Date = new Date()): string {
  const date = toDate(value)
  if (!date) return ''
  const key = DATE_KEY.format(date)
  if (key === DATE_KEY.format(now)) return clockText(date)
  if (key === DATE_KEY.format(new Date(now.getTime() - 86_400_000))) return `yesterday, ${clockText(date)}`
  return `${dayText(date, now)}, ${clockText(date)}`
}

/** "45 s", "12 min", "3 h 20 min", "2 days". Negative values clamp to 0. */
export function durationText(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    const rest = minutes % 60
    return rest && hours < 10 ? `${hours} h ${rest} min` : `${hours} h`
  }
  const days = Math.floor(hours / 24)
  return days === 1 ? '1 day' : `${days} days`
}

/** Time since a moment, never in the future. */
export function sinceText(value: string | number | Date | null | undefined, now: Date = new Date()): string {
  const date = toDate(value)
  if (!date) return ''
  return durationText(now.getTime() - date.getTime())
}

/** "just now", "12 min ago", "3 h ago", "2 days ago". Future times read "just now". */
export function agoText(value: string | number | Date | null | undefined, now: Date = new Date()): string {
  const date = toDate(value)
  if (!date) return ''
  const elapsed = now.getTime() - date.getTime()
  if (elapsed < 45_000) return 'just now'
  return `${durationText(elapsed)} ago`
}
