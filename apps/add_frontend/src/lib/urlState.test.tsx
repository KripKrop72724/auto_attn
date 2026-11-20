import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, useLocation, useNavigationType } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  assertAllowedKeys, flag, integer, oneOf, pktDate, readUrlValues, text, useUrlState, writeUrlValues,
} from './urlState'

const schema = {
  q: text(),
  status: oneOf(['all', 'offline', 'working'] as const, 'all'),
  page: integer(1, { min: 1 }),
  spares: flag(),
  from: pktDate(),
}

afterEach(() => { vi.useRealTimers() })

describe('URL filter values', () => {
  it('reads defaults for missing or unaccepted values', () => {
    expect(readUrlValues(schema, '?status=offline&page=3&spares=1&from=2026-11-06&q=swat')).toEqual({
      q: 'swat', status: 'offline', page: 3, spares: true, from: '2026-11-06',
    })
    expect(readUrlValues(schema, '?status=DROP&page=-1&spares=yes&from=6-11-2026')).toEqual({
      q: '', status: 'all', page: 1, spares: false, from: '',
    })
  })

  it('writes only what changed, drops defaults and keeps other keys', () => {
    expect(writeUrlValues(schema, '?division=life&tab=x', { status: 'offline', page: 1 })).toBe('?division=life&tab=x&status=offline')
    expect(writeUrlValues(schema, '?status=offline', { status: 'all' })).toBe('')
    expect(writeUrlValues(schema, '', { q: '  multan  ' })).toBe('?q=multan')
  })

  it('never writes a value that could be a CNIC or phone number', () => {
    expect(writeUrlValues(schema, '', { q: '35202-1234567-1' })).toBe('')
    expect(writeUrlValues(schema, '', { q: '3520212' })).toBe('')
    expect(writeUrlValues(schema, '', { q: '10072' })).toBe('?q=10072')
  })

  it('refuses private key names', () => {
    expect(() => assertAllowedKeys(['q', 'cnic'])).toThrow(/not allowed/)
    expect(() => assertAllowedKeys(['password'])).toThrow()
    expect(() => assertAllowedKeys(['offline_reason'])).toThrow()
    expect(() => assertAllowedKeys(['confirmation_text'])).toThrow()
    expect(() => assertAllowedKeys(['why', 'division', 'when'])).not.toThrow()
    expect(writeUrlValues({ cnic: text() }, '', { cnic: 'x' })).toBe('')
  })
})

describe('useUrlState', () => {
  const wrapper = (initial: string) => ({ children }: { children: ReactNode }) =>
    <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>

  it('pushes history for chips so Back undoes a filter', () => {
    const { result } = renderHook(() => ({ state: useUrlState(schema), location: useLocation(), type: useNavigationType() }), { wrapper: wrapper('/devices?division=life') })
    act(() => result.current.state[1]({ status: 'offline' }))
    expect(result.current.location.search).toBe('?division=life&status=offline')
    expect(result.current.type).toBe('PUSH')
    expect(result.current.state[0].status).toBe('offline')
  })

  it('shows typing at once and replaces history 250 ms after the last key', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => ({ state: useUrlState(schema), location: useLocation(), type: useNavigationType() }), { wrapper: wrapper('/devices') })
    act(() => result.current.state[1]({ q: 'mul' }))
    act(() => result.current.state[1]({ q: 'multan' }))
    expect(result.current.state[0].q).toBe('multan')
    expect(result.current.location.search).toBe('')
    act(() => { vi.advanceTimersByTime(260) })
    expect(result.current.location.search).toBe('?q=multan')
    expect(result.current.type).toBe('REPLACE')
  })

  it('throws in tests when a page declares a private key', () => {
    expect(() => renderHook(() => useUrlState({ cnic: text() }), { wrapper: wrapper('/people') })).toThrow(/not allowed/)
  })
})
