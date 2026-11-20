// The browser keeps one key, `add.prefs`, and only after the person changes a
// choice (owner decision D-25). Nothing else is ever stored: no filters, no
// device lists, no names, no secrets. Storage can be missing or throw (private
// windows, blocked site data), so every read and write is guarded and the
// dashboard works the same without it.
import { useCallback, useSyncExternalStore } from 'react'
import type { DivisionKey } from './deviceNames'

export const PREFS_KEY = 'add.prefs'

export type DivisionChoice = 'all' | DivisionKey

export interface Prefs {
  /** The Life / Health / All choice, used when the URL has no `division`. */
  division: DivisionChoice
  /** Show "Technical details" open by default. */
  technical: boolean
  /** The menu-change note on Home was closed. */
  menuNoteSeen: boolean
}

export const DEFAULT_PREFS: Prefs = Object.freeze({ division: 'all', technical: false, menuNoteSeen: false })

const isDivision = (value: unknown): value is DivisionChoice => value === 'all' || value === 'life' || value === 'health'

/** Keeps only the three known fields with valid values; anything else falls back to the default. */
export function sanitizePrefs(value: unknown): Prefs {
  const raw = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
  return {
    division: isDivision(raw.division) ? raw.division : DEFAULT_PREFS.division,
    technical: typeof raw.technical === 'boolean' ? raw.technical : DEFAULT_PREFS.technical,
    menuNoteSeen: typeof raw.menuNoteSeen === 'boolean' ? raw.menuNoteSeen : DEFAULT_PREFS.menuNoteSeen,
  }
}

const storage = (): Storage | null => {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

function readStored(): Prefs {
  try {
    const text = storage()?.getItem(PREFS_KEY)
    return text ? sanitizePrefs(JSON.parse(text)) : DEFAULT_PREFS
  } catch {
    return DEFAULT_PREFS
  }
}

let current: Prefs | null = null
const listeners = new Set<() => void>()
const notify = () => listeners.forEach((listener) => listener())

export function getPrefs(): Prefs {
  if (!current) current = readStored()
  return current
}

/** Changes one or more choices and saves them. Saving failures are ignored: the choice still applies to this tab. */
export function setPrefs(change: Partial<Prefs>): Prefs {
  const next = sanitizePrefs({ ...getPrefs(), ...change })
  const previous = getPrefs()
  if (next.division === previous.division && next.technical === previous.technical && next.menuNoteSeen === previous.menuNoteSeen) {
    return previous
  }
  current = next
  try {
    storage()?.setItem(PREFS_KEY, JSON.stringify(next))
  } catch {
    // Storage full or blocked: keep the choice in memory for this tab.
  }
  notify()
  return next
}

/** Forgets the stored choices (tests, and a future "Reset" in Settings). */
export function resetPrefs() {
  current = DEFAULT_PREFS
  try {
    storage()?.removeItem(PREFS_KEY)
  } catch {
    // Nothing to remove when storage is blocked.
  }
  notify()
}

// Another tab changed the choice: pick it up here too.
const onStorage = (event: StorageEvent) => {
  if (event.key !== null && event.key !== PREFS_KEY) return
  current = readStored()
  notify()
}

const subscribe = (listener: () => void) => {
  if (!listeners.size && typeof window !== 'undefined') window.addEventListener('storage', onStorage)
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (!listeners.size && typeof window !== 'undefined') window.removeEventListener('storage', onStorage)
  }
}

/** The current choices and a setter; re-renders when they change in this tab or another. */
export function usePrefs(): [Prefs, (change: Partial<Prefs>) => void] {
  const prefs = useSyncExternalStore(subscribe, getPrefs, () => DEFAULT_PREFS)
  const update = useCallback((change: Partial<Prefs>) => { setPrefs(change) }, [])
  return [prefs, update]
}

/** Test helper: drop the in-memory copy so the next read comes from storage again. */
export function reloadPrefsForTests() {
  current = null
}
