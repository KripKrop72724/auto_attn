// Filters live in the page URL, so a link, a reload or Back shows the same list.
// Rules shared by every page (01-foundation "Filters", plan 3.7):
// - Shared key names: q, status, division, sort, tab, when (today | yesterday |
//   7d | custom) with from / to (Pakistan dates YYYY-MM-DD), device, type, city,
//   page. A page may add its own keys.
// - Forbidden keys: anything matching /cnic|password|reason|confirmation/i. A
//   schema that declares one throws (tests fail), so a secret never reaches
//   browser history or server logs. Use `why` for an "offline reason" filter.
// - A search that looks like a CNIC (6 or more digits or dashes) is never
//   written to the URL; Employee IDs are shorter.
// - Chip and select changes push history (Back undoes a filter); typing
//   replaces it, 250 ms after the last key.
// - Values the schema does not accept are ignored and read as the default.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

export const RESERVED_URL_KEYS = ['q', 'status', 'division', 'sort', 'tab', 'when', 'from', 'to', 'device', 'type', 'city', 'page'] as const
export const FORBIDDEN_URL_KEY = /cnic|password|reason|confirmation/i
/** A value that could be part of a CNIC or phone number: never put it in the URL. */
export const SENSITIVE_DIGITS = /^[\d\s-]{6,}$/
export const TYPING_DEBOUNCE_MS = 250

export interface UrlField<T> {
  /** Reads the URL value; `null` when the key is absent. Return the default for anything not accepted. */
  parse(raw: string | null): T
  /** Writes the value; `null` removes the key. The default value is always removed. */
  serialize(value: T): string | null
  defaultValue: T
  /** Free typing (search): replace history after a pause instead of pushing it. */
  typing?: boolean
}

export type UrlSchema = Record<string, UrlField<any>>
export type UrlValues<S extends UrlSchema> = { [K in keyof S]: S[K] extends UrlField<infer T> ? T : never }

/** One of a fixed list of values ("tab", "status", "sort"). */
export function oneOf<const T extends string>(values: readonly T[], defaultValue: T): UrlField<T> {
  const allowed = new Set<string>(values)
  return {
    parse: (raw) => (raw !== null && allowed.has(raw) ? raw as T : defaultValue),
    serialize: (value) => value,
    defaultValue,
  }
}

/** Free text such as a search. Typing replaces history; a CNIC-like value is never written. */
export function text(defaultValue = '', { maxLength = 120, typing = true } = {}): UrlField<string> {
  return {
    parse: (raw) => (raw === null ? defaultValue : raw.slice(0, maxLength)),
    serialize: (value) => {
      const trimmed = value.trim()
      if (!trimmed || SENSITIVE_DIGITS.test(trimmed)) return null
      return trimmed.slice(0, maxLength)
    },
    defaultValue,
    typing,
  }
}

/** A whole number such as a page; anything else reads as the default. */
export function integer(defaultValue: number, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}): UrlField<number> {
  return {
    parse: (raw) => {
      if (raw === null || !/^\d+$/.test(raw)) return defaultValue
      const value = Number.parseInt(raw, 10)
      return value >= min && value <= max ? value : defaultValue
    },
    serialize: (value) => String(value),
    defaultValue,
  }
}

/** An on/off filter written as `key=1`. */
export function flag(): UrlField<boolean> {
  return { parse: (raw) => raw === '1' || raw === 'true', serialize: (value) => (value ? '1' : null), defaultValue: false }
}

/** A Pakistan calendar day, "2026-11-06". */
export function pktDate(): UrlField<string> {
  return {
    parse: (raw) => (raw && /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : ''),
    serialize: (value) => (/^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null),
    defaultValue: '',
  }
}

/** Throws when a schema declares a key that must never be in a URL. */
export function assertAllowedKeys(keys: Iterable<string>) {
  for (const key of keys) {
    if (FORBIDDEN_URL_KEY.test(key)) throw new Error(`The URL key "${key}" is not allowed: it could expose private data.`)
  }
}

export function readUrlValues<S extends UrlSchema>(schema: S, search: string | URLSearchParams): UrlValues<S> {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search
  const values: Record<string, unknown> = {}
  for (const [key, field] of Object.entries(schema)) {
    values[key] = FORBIDDEN_URL_KEY.test(key) ? field.defaultValue : field.parse(params.get(key))
  }
  return values as UrlValues<S>
}

/**
 * Applies changed values to a query string and returns the new one ("" or "?a=1").
 * Keys not in the schema are kept, so page keys and `division` survive.
 */
export function writeUrlValues<S extends UrlSchema>(schema: S, search: string, change: Partial<UrlValues<S>>): string {
  const params = new URLSearchParams(search)
  for (const [key, value] of Object.entries(change)) {
    const field = schema[key]
    if (!field || value === undefined || FORBIDDEN_URL_KEY.test(key)) continue
    const serialized = Object.is(value, field.defaultValue) ? null : field.serialize(value)
    if (serialized === null || serialized === '') params.delete(key)
    else params.set(key, serialized)
  }
  const query = params.toString()
  return query ? `?${query}` : ''
}

export interface SetUrlOptions {
  /** Force push or replace; by default typing fields replace and the rest push. */
  history?: 'push' | 'replace'
}

/**
 * Typed URL filters for one page. The values update at once; the URL follows
 * (pushed for chips, replaced 250 ms after typing stops).
 */
/**
 * The search of the page an open device panel sits over, or null when no panel
 * is open. The panel lives at /devices/:id and keeps that page's address in
 * history state (`from`).
 */
export function panelOrigin(pathname: string, state: unknown): string | null {
  if (!/^\/devices\/[^/]+\/?$/.test(pathname)) return null
  const from = (state as { from?: unknown } | null)?.from
  if (typeof from !== 'string' || !from) return null
  const query = from.split('#')[0].split('?')[1]
  return query ? `?${query}` : ''
}

export function useUrlState<S extends UrlSchema>(schema: S): [UrlValues<S>, (change: Partial<UrlValues<S>>, options?: SetUrlOptions) => void] {
  const location = useLocation()
  const navigate = useNavigate()
  const schemaRef = useRef(schema)
  schemaRef.current = schema
  const keys = Object.keys(schema).join('\u0000')
  // A programming mistake, caught by the tests; production just never writes the key.
  useMemo(() => { if (import.meta.env.DEV || import.meta.env.MODE === 'test') assertAllowedKeys(keys.split('\u0000').filter(Boolean)) }, [keys])

  // Behind an open device panel (/devices/:id with the page it came from in
  // history state) the page keeps reading its own filters from that saved
  // address, and never writes onto the panel's address.
  const behind = panelOrigin(location.pathname, location.state)
  const search = behind === null ? location.search : behind
  const behindRef = useRef(behind !== null)
  behindRef.current = behind !== null
  const fromUrl = useMemo(() => readUrlValues(schemaRef.current, search), [search, keys])
  const [overlay, setOverlay] = useState<Partial<UrlValues<S>>>({})
  const pending = useRef<{ timer: number; change: Partial<UrlValues<S>> } | null>(null)
  const searchRef = useRef(search)
  searchRef.current = search
  const pathRef = useRef(location.pathname)
  pathRef.current = location.pathname

  // Values already sent to the URL but not yet shown there: the router commits
  // its navigation in a transition, so a checkbox would otherwise spring back
  // for a moment after a click.
  const unsaved = useRef<Partial<UrlValues<S>>>({})

  // The URL changed (Back, a link, or our own write landed): it wins over typed values not yet written.
  useEffect(() => {
    if (pending.current) return
    unsaved.current = {}
    setOverlay({})
  }, [search])

  const flush = useCallback(() => {
    const queued = pending.current
    if (!queued) return
    window.clearTimeout(queued.timer)
    pending.current = null
    if (behindRef.current) return
    const change = { ...unsaved.current, ...queued.change }
    const next = writeUrlValues(schemaRef.current, searchRef.current, change)
    if (next === (searchRef.current || '')) {
      unsaved.current = {}
      setOverlay({})
      return
    }
    // The overlay stays until the new address lands (the effect above clears it).
    unsaved.current = change
    navigate(`${pathRef.current}${next}${window.location.hash}`, { replace: true, preventScrollReset: true })
  }, [navigate])

  useEffect(() => () => {
    if (pending.current) window.clearTimeout(pending.current.timer)
  }, [])

  const set = useCallback((change: Partial<UrlValues<S>>, options: SetUrlOptions = {}) => {
    if (behindRef.current) return
    const typing = Object.keys(change).some((key) => schemaRef.current[key]?.typing)
    const mode = options.history || (typing ? 'replace' : 'push')
    if (mode === 'replace' && typing && !options.history) {
      setOverlay((current) => ({ ...current, ...change }))
      const merged = { ...(pending.current?.change || {}), ...change }
      if (pending.current) window.clearTimeout(pending.current.timer)
      pending.current = { change: merged, timer: window.setTimeout(flush, TYPING_DEBOUNCE_MS) }
      return
    }
    const merged = { ...unsaved.current, ...(pending.current?.change || {}), ...change }
    if (pending.current) {
      window.clearTimeout(pending.current.timer)
      pending.current = null
    }
    const next = writeUrlValues(schemaRef.current, searchRef.current, merged)
    if (next === (searchRef.current || '')) {
      unsaved.current = {}
      setOverlay({})
      return
    }
    // Show the new values at once; the URL follows when the router commits.
    unsaved.current = merged
    setOverlay(merged)
    navigate(`${pathRef.current}${next}${window.location.hash}`, { replace: mode === 'replace', preventScrollReset: true })
  }, [flush, navigate])

  const values = useMemo(() => ({ ...fromUrl, ...overlay }) as UrlValues<S>, [fromUrl, overlay])
  return [values, set]
}
