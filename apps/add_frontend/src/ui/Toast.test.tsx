import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider, useToast, type ToastApi } from './Toast'
import { setUpKitDom, tearDownKitDom } from './testUtils'

let api: ToastApi
function Capture() {
  api = useToast()
  return <p data-testid="legacy" data-text={api.toast?.text || ''} />
}

describe('Toast', () => {
  let container: HTMLElement
  beforeEach(() => {
    container = setUpKitDom()
    vi.useFakeTimers()
    render(<ToastProvider><Capture /></ToastProvider>, { container })
  })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('keeps up to three messages, dropping the oldest, and exposes the newest in the old shape', () => {
    act(() => { api.notice('One'); api.notice('Two'); api.notice('Three'); api.notice('Four') })
    expect(screen.queryByText('One')).toBeNull()
    expect(document.querySelectorAll('.ui-toast')).toHaveLength(3)
    expect(api.toast).toEqual({ kind: 'notice', text: 'Four' })
    expect(screen.getByTestId('legacy').getAttribute('data-text')).toBe('Four')
    expect(document.querySelector('.ui-toast-stack')?.closest('#overlay-root')).not.toBeNull()
  })

  it('closes notices after 5 s, pauses while hovered, and keeps errors until closed', () => {
    act(() => { api.notice('Saved'); api.error('Could not save') })
    expect(screen.getByRole('alert').textContent).toContain('Could not save')
    expect(screen.getByRole('status').textContent).toContain('Saved')
    const notice = screen.getByRole('status')
    fireEvent.mouseEnter(notice)
    act(() => { vi.advanceTimersByTime(8000) })
    expect(screen.queryByText('Saved')).not.toBeNull()
    fireEvent.mouseLeave(notice)
    act(() => { vi.advanceTimersByTime(5100) })
    expect(screen.queryByText('Saved')).toBeNull()
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(screen.getByText('Could not save')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close message' }))
    expect(screen.queryByText('Could not save')).toBeNull()
    expect(api.toast).toBeNull()
  })

  it('runs the action button once and closes the message', () => {
    const undo = vi.fn()
    act(() => { api.show({ tone: 'success', text: 'Marked as a spare.', action: { label: 'Undo', onClick: undo } }) })
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(undo).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Marked as a spare.')).toBeNull()
  })

  it('dismiss() without an id closes the newest, as before', () => {
    act(() => { api.notice('First'); api.notice('Second') })
    act(() => { api.dismiss() })
    expect(screen.queryByText('Second')).toBeNull()
    expect(screen.getByText('First')).toBeTruthy()
  })
})

describe('useToast outside a provider', () => {
  afterEach(() => { cleanup(); tearDownKitDom() })
  it('keeps its own queue so old test harnesses still read toast.toast.text', () => {
    const container = setUpKitDom()
    render(<Capture />, { container })
    act(() => { api.error('Request failed') })
    expect(screen.getByTestId('legacy').getAttribute('data-text')).toBe('Request failed')
    expect(api.toast?.kind).toBe('error')
  })
})
