import { useEffect, useState } from 'react'

export const PHONE_QUERY = '(max-width: 760px)'
export const DESKTOP_QUERY = '(min-width: 1024px)'

function matches(query: string) {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && Boolean(window.matchMedia(query)?.matches)
  } catch {
    return false
  }
}

export function useMediaQuery(query: string) {
  const [value, setValue] = useState(() => matches(query))
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    let list: MediaQueryList | undefined
    try {
      list = window.matchMedia(query)
    } catch {
      return
    }
    if (!list) return
    const update = () => setValue(Boolean(list?.matches))
    update()
    if (typeof list.addEventListener === 'function') {
      list.addEventListener('change', update)
      return () => list?.removeEventListener('change', update)
    }
    return undefined
  }, [query])
  return value
}

export const useIsPhone = () => useMediaQuery(PHONE_QUERY)
