// The Division choice (All · Life · Health) for the page, kept in the URL
// (`?division=life`) so a link or reload shows the same devices. When the URL
// has none and the person chose Life or Health before (add.prefs), the URL gets
// it back with `replace`. The choice is saved only when the person changes it.
import { useCallback, useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { parseDivision, type DivisionChoice } from './division'
import { usePrefs } from './prefs'

/** Pages where the switch does not apply (a single device, set-up, settings). */
export const routesWithoutDivision = (pathname: string) =>
  /^\/(setup|settings)(\/|$)/.test(pathname) || /^\/(devices|people)\/[^/]+/.test(pathname)

export function withDivision(search: string, division: DivisionChoice): string {
  const params = new URLSearchParams(search)
  if (division === 'all') params.delete('division')
  else params.set('division', division)
  const query = params.toString()
  return query ? `?${query}` : ''
}

export function useDivision(): [DivisionChoice, (next: DivisionChoice) => void] {
  const location = useLocation()
  const navigate = useNavigate()
  const [prefs, setPrefs] = usePrefs()
  const fromUrl = parseDivision(location.search)
  const applies = !routesWithoutDivision(location.pathname)

  useEffect(() => {
    if (!applies || fromUrl !== 'all' || prefs.division === 'all') return
    if (new URLSearchParams(location.search).has('division')) return
    navigate(`${location.pathname}${withDivision(location.search, prefs.division)}${location.hash}`, { replace: true, state: location.state })
  }, [applies, fromUrl, location.hash, location.pathname, location.search, location.state, navigate, prefs.division])

  const setDivision = useCallback((next: DivisionChoice) => {
    setPrefs({ division: next })
    if (next === fromUrl) return
    navigate(`${location.pathname}${withDivision(location.search, next)}${location.hash}`, { state: location.state })
  }, [fromUrl, location.hash, location.pathname, location.search, location.state, navigate, setPrefs])

  // An explicit "All" in the URL is not possible (All has no key), so a stored
  // Life/Health wins until the person picks All again.
  return [applies ? (fromUrl !== 'all' ? fromUrl : prefs.division) : 'all', setDivision]
}
