// After a deploy the server serves new page files and the old ones are gone.
// This notices a newer dashboard (the start file `/assets/index-*.js` named in
// `/` differs from the running one) and handles the one case that used to break
// an open page: a page file that no longer exists.
import { useCallback, useEffect, useRef, useState } from 'react'

export const NEW_VERSION_CHECK_MS = 10 * 60_000
const START_FILE = /\/assets\/index-[A-Za-z0-9_-]+\.js/

/** The start file of the running page, or '' in development (no hashed file). */
export function runningStartFile(doc: Document = document): string {
  for (const script of Array.from(doc.querySelectorAll<HTMLScriptElement>('script[type="module"][src]'))) {
    const match = START_FILE.exec(script.getAttribute('src') || '')
    if (match) return match[0]
  }
  return ''
}

/** The start file named in a served page, or '' when there is none. */
export function servedStartFile(html: string): string {
  return START_FILE.exec(html)?.[0] || ''
}

/**
 * 'new' when the server now serves another start file, 'same' when not, and
 * 'unknown' when it could not tell (a failed check never shows anything).
 */
export async function checkForNewVersion(
  running = runningStartFile(),
  load: (path: string, init: RequestInit) => Promise<Response> = (path, init) => fetch(path, init),
): Promise<'new' | 'same' | 'unknown'> {
  if (!running) return 'unknown'
  try {
    const response = await load('/', { cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'text/html' } })
    if (!response.ok) return 'unknown'
    const served = servedStartFile(await response.text())
    if (!served) return 'unknown'
    return served === running ? 'same' : 'new'
  } catch {
    return 'unknown'
  }
}

/**
 * True once a newer dashboard is on the server. Checks when the live updates
 * reconnect (a deploy restarts the server), when the tab becomes visible, and
 * every 10 minutes.
 */
export function useNewVersion(connectionKey: unknown, enabled = true): boolean {
  const [available, setAvailable] = useState(false)
  const busy = useRef(false)
  const lastCheck = useRef(0)
  const check = useCallback(async () => {
    if (!enabled || busy.current || available) return
    busy.current = true
    lastCheck.current = Date.now()
    try {
      if ((await checkForNewVersion()) === 'new') setAvailable(true)
    } finally {
      busy.current = false
    }
  }, [available, enabled])

  const first = useRef(true)
  useEffect(() => {
    // The first value is the page's own start; only later reconnects can mean a deploy.
    if (first.current) { first.current = false; return }
    void check()
  }, [check, connectionKey])

  useEffect(() => {
    if (!enabled) return
    const onVisible = () => {
      if (document.visibilityState === 'visible' && Date.now() - lastCheck.current > 30_000) void check()
    }
    document.addEventListener('visibilitychange', onVisible)
    const timer = window.setInterval(() => void check(), NEW_VERSION_CHECK_MS)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.clearInterval(timer)
    }
  }, [check, enabled])
  return available
}

const CHUNK_ERROR = /dynamically imported module|Loading chunk|Importing a module script failed|error loading dynamically imported module/i
const RELOADED_FLAG = 'addReloadedOnce'

/** A page file could not be downloaded (usually: the dashboard was updated). */
export const isChunkLoadError = (error: unknown) =>
  error instanceof Error ? CHUNK_ERROR.test(`${error.name} ${error.message}`) : typeof error === 'string' && CHUNK_ERROR.test(error)

const historyState = (): Record<string, unknown> => {
  const state = window.history.state
  return state && typeof state === 'object' ? state as Record<string, unknown> : {}
}

/**
 * Reloads the page once for a chunk error. The "already reloaded" mark lives in
 * history.state (never in browser storage) and keeps React Router's own fields.
 * Returns true when a reload was started.
 */
export function reloadOnceForChunkError(error: unknown, reload: () => void = () => window.location.reload()): boolean {
  if (!isChunkLoadError(error) || historyState()[RELOADED_FLAG]) return false
  try {
    window.history.replaceState({ ...historyState(), [RELOADED_FLAG]: true }, '')
  } catch {
    return false
  }
  reload()
  return true
}
