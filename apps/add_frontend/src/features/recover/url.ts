// Recover attendance keeps its tab and filters in the page URL, through the
// router, so a link, a reload or Back shows the same list and the global
// Division switch never overwrites them. No reason or password ever goes here.
import { useCallback, useMemo } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { recoverSection } from '../../routing'
import type { RecoverTab } from '../../words/recover'

/** Keys each tab owns; switching tabs drops the others. Old names are still read. */
export const TAB_KEYS: Record<RecoverTab, string[]> = {
  recoveries: ['status', 'device', 'q'],
  unreadable: ['review', 'problem', 'device_id', 'code', 'record', 'corrected', 'job_id', 'review_state', 'disposition', 'error_code', 'ordinal'],
  held: [],
}

export type UrlChanges = Record<string, string | null | undefined>

export function useRecoverUrl() {
  const location = useLocation()
  const navigate = useNavigate()
  const params = useMemo(() => new URLSearchParams(location.search), [location.search])
  const tab = recoverSection(location.search) as RecoverTab
  const update = useCallback((changes: UrlChanges, options: { replace?: boolean } = {}) => {
    const next = new URLSearchParams(location.search)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    const query = next.toString()
    const target = `${location.pathname}${query ? `?${query}` : ''}${location.hash}`
    if (target === `${location.pathname}${location.search}${location.hash}`) return
    navigate(target, { replace: options.replace, state: location.state })
  }, [location.hash, location.pathname, location.search, location.state, navigate])
  const setTab = useCallback((next: RecoverTab, extra: UrlChanges = {}) => {
    const changes: UrlChanges = { tab: next === 'recoveries' ? null : next }
    for (const [name, keys] of Object.entries(TAB_KEYS)) {
      if (name !== next) keys.forEach((key) => { changes[key] = null })
    }
    update({ ...changes, ...extra })
  }, [update])
  return { params, tab, update, setTab }
}
