import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { PREFS_KEY, reloadPrefsForTests, resetPrefs } from './prefs'
import { routesWithoutDivision, useDivision, withDivision } from './useDivision'

const wrapper = (initial: string) => ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>

beforeEach(() => { window.localStorage.clear(); reloadPrefsForTests() })
afterEach(() => { resetPrefs(); window.localStorage.clear() })

describe('Division in the URL', () => {
  it('reads the URL, writes nothing until the person changes it, then saves add.prefs only', () => {
    const { result } = renderHook(() => ({ division: useDivision(), location: useLocation() }), { wrapper: wrapper('/devices?division=life&q=swat') })
    expect(result.current.division[0]).toBe('life')
    expect(window.localStorage.length).toBe(0)
    act(() => result.current.division[1]('health'))
    expect(result.current.location.search).toBe('?division=health&q=swat')
    expect(Object.keys(window.localStorage)).toEqual([PREFS_KEY])
    act(() => result.current.division[1]('all'))
    expect(result.current.location.search).toBe('?q=swat')
    expect(result.current.division[0]).toBe('all')
  })

  it('puts a saved choice back into a URL that has none', () => {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify({ division: 'life', technical: false, menuNoteSeen: false }))
    reloadPrefsForTests()
    const { result } = renderHook(() => ({ division: useDivision(), location: useLocation() }), { wrapper: wrapper('/problems') })
    expect(result.current.location.search).toBe('?division=life')
    expect(result.current.division[0]).toBe('life')
  })

  it('does not apply on one device, Set up a device or Settings', () => {
    expect(routesWithoutDivision('/devices/connector-one')).toBe(true)
    expect(routesWithoutDivision('/people/connector-one')).toBe(true)
    expect(routesWithoutDivision('/setup')).toBe(true)
    expect(routesWithoutDivision('/settings')).toBe(true)
    expect(routesWithoutDivision('/devices')).toBe(false)
    expect(withDivision('?a=1', 'life')).toBe('?a=1&division=life')
    expect(withDivision('?division=life', 'all')).toBe('')
  })

  it('keeps the list choice from the address under an open device, and never adds the saved one there', () => {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify({ division: 'health', technical: false, menuNoteSeen: false }))
    reloadPrefsForTests()
    const open = renderHook(() => ({ division: useDivision(), location: useLocation() }), { wrapper: wrapper('/devices/connector-one?division=life') })
    expect(open.result.current.division[0]).toBe('life')
    const bare = renderHook(() => ({ division: useDivision(), location: useLocation() }), { wrapper: wrapper('/people/connector-one') })
    expect(bare.result.current.division[0]).toBe('all')
    expect(bare.result.current.location.search).toBe('')
  })
})
