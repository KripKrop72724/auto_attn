import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_PREFS, PREFS_KEY, getPrefs, reloadPrefsForTests, resetPrefs, sanitizePrefs, setPrefs, usePrefs } from './prefs'

beforeEach(() => {
  window.localStorage.clear()
  reloadPrefsForTests()
})
afterEach(() => {
  vi.restoreAllMocks()
  resetPrefs()
  window.localStorage.clear()
})

describe('browser preferences (one key, add.prefs)', () => {
  it('writes nothing until a choice changes, then only the one key', () => {
    expect(getPrefs()).toEqual(DEFAULT_PREFS)
    expect(window.localStorage.length).toBe(0)
    setPrefs({ division: 'all' })
    expect(window.localStorage.length).toBe(0)
    setPrefs({ division: 'life' })
    expect(Object.keys(window.localStorage)).toEqual([PREFS_KEY])
    expect(JSON.parse(window.localStorage.getItem(PREFS_KEY) || '{}')).toEqual({ division: 'life', technical: false, menuNoteSeen: false })
  })

  it('keeps only the three known fields with valid values', () => {
    expect(sanitizePrefs({ division: 'LIFE', technical: 'yes', menuNoteSeen: true, password: 'secret' }))
      .toEqual({ division: 'all', technical: false, menuNoteSeen: true })
    window.localStorage.setItem(PREFS_KEY, '{not json')
    reloadPrefsForTests()
    expect(getPrefs()).toEqual(DEFAULT_PREFS)
    window.localStorage.setItem(PREFS_KEY, JSON.stringify({ division: 'health', technical: true, extra: 'x' }))
    reloadPrefsForTests()
    expect(getPrefs()).toEqual({ division: 'health', technical: true, menuNoteSeen: false })
  })

  it('works when the browser blocks storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    reloadPrefsForTests()
    expect(getPrefs()).toEqual(DEFAULT_PREFS)
    expect(setPrefs({ technical: true }).technical).toBe(true)
    expect(getPrefs().technical).toBe(true)
  })

  it('updates the page when a choice changes here or in another tab', () => {
    const { result } = renderHook(() => usePrefs())
    act(() => result.current[1]({ technical: true }))
    expect(result.current[0].technical).toBe(true)
    act(() => {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify({ division: 'health', technical: false, menuNoteSeen: true }))
      window.dispatchEvent(new StorageEvent('storage', { key: PREFS_KEY }))
    })
    expect(result.current[0]).toEqual({ division: 'health', technical: false, menuNoteSeen: true })
  })
})
