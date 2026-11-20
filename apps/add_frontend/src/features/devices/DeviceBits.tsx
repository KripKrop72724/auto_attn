// Small parts shared by Home and Devices: "Updated 12 s ago", the last-seen
// words and the status chip of one device row.
import { useEffect, useState } from 'react'
import { ago, clock, dayTime, duration } from '../../lib/time'
import { StatusChip } from '../../ui/StatusChip'
import { DEVICES_WORDS, REFRESH_WORDS } from '../../words/home'
import { chipTone, type DeviceView } from './deviceList'

/** Re-renders every `interval` ms so "… ago" words stay true. */
export function useNow(interval = 10_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), interval)
    return () => window.clearInterval(timer)
  }, [interval])
  return now
}

/** "Updated 12 s ago" (title: "Updated at 10:42 am"); ticks every 10 s. */
export function UpdatedAgo({ at, className }: { at: Date | string | null | undefined; className?: string }) {
  const now = useNow()
  if (!at) return null
  return (
    <span className={className ? `updated-ago ${className}` : 'updated-ago'} title={REFRESH_WORDS.updatedAt(clock(at))}>
      {REFRESH_WORDS.updatedAgo(ago(at, now))}
    </span>
  )
}

/** "just now", "12 s ago", "yesterday, 5:41 pm · 19 h ago"; "Not seen yet" when never. */
export function lastSeenText(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return DEVICES_WORDS.notSeen
  const seconds = (now - Date.parse(value)) / 1000
  if (!Number.isFinite(seconds)) return DEVICES_WORDS.notSeen
  if (seconds < 3600) return ago(value, now)
  return `${dayTime(value, now)} · ${ago(value, now)}`
}

/** "Since 9:24 am · 3 h 20 min" for a status that has a start time. */
export function sinceText(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return ''
  const when = dayTime(value, now)
  const long = duration(value, now)
  return long ? `Since ${when} · ${long}` : `Since ${when}`
}

export function DeviceStatusChip({ view, size = 'md' }: { view: DeviceView; size?: 'sm' | 'md' }) {
  return <StatusChip tone={chipTone(view.status.group)} label={view.status.chip} code={view.status.code} size={size} />
}
