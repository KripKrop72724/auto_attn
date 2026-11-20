// HOME-11 live motion: which rows just changed status (1.2 s tint through
// DataList `changedKeys`) and one polite sentence when a device goes offline or
// comes back. The first answer never counts as a change.
import { useEffect, useRef, useState } from 'react'
import type { DeviceView } from './deviceList'
import { DEVICES_WORDS } from '../../words/home'

const HIGHLIGHT_MS = 1200

export function useStatusChange(views: readonly DeviceView[], loaded: boolean) {
  const previous = useRef<Map<string, string> | null>(null)
  const [changed, setChanged] = useState<ReadonlySet<string>>(() => new Set())
  const [announcement, setAnnouncement] = useState('')
  const timer = useRef(0)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  useEffect(() => {
    if (!loaded) return
    const next = new Map(views.map((view) => [view.id, view.status.group]))
    const before = previous.current
    previous.current = next
    if (!before) return
    const keys: string[] = []
    let sentence = ''
    for (const view of views) {
      const was = before.get(view.id)
      if (!was || was === view.status.group) continue
      keys.push(view.id)
      if (view.status.group === 'offline' && was === 'working') sentence = DEVICES_WORDS.announceOffline(view.name)
      else if (view.status.group === 'working' && was === 'offline') sentence = DEVICES_WORDS.announceWorking(view.name)
    }
    if (!keys.length) return
    setChanged(new Set(keys))
    if (sentence) setAnnouncement(sentence)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setChanged(new Set()), HIGHLIGHT_MS)
  }, [loaded, views])

  return { changed, announcement }
}
