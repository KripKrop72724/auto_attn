import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Button, IconButton } from './Button'
import { setUpKitDom, tearDownKitDom } from './testUtils'

describe('Button', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('calls onClick exactly once and never adds a confirmation', () => {
    const onClick = vi.fn()
    render(<Button variant="primary" onClick={onClick}>Start</Button>, { container })
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(document.querySelector('input')).toBeNull()
  })

  it('shows the busy text, disables itself, sets aria-busy and keeps its width', () => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(132)
    const { rerender } = render(<Button busyLabel="Saving…">Save</Button>, { container })
    rerender(<Button busy busyLabel="Saving…">Save</Button>)
    const button = screen.getByRole('button', { name: 'Saving…' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(button.style.minWidth).toBe('132px')
    expect(button.querySelector('.ui-spinner')).not.toBeNull()
    rerender(<Button busyLabel="Saving…">Save</Button>)
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('goes busy while a returned promise runs, ignores clicks meanwhile, then shows the done label', async () => {
    vi.useFakeTimers()
    let resolve: () => void = () => undefined
    const onClick = vi.fn(() => new Promise<void>((done) => { resolve = done }))
    render(<Button busyLabel="Sending…" doneLabel="Sent" onClick={onClick}>Send</Button>, { container })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(screen.getByRole('button', { name: 'Sending…' }).getAttribute('aria-busy')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Sending…' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    await act(async () => { resolve() })
    expect(screen.getByRole('button', { name: 'Sent' })).toBeTruthy()
    act(() => { vi.advanceTimersByTime(1300) })
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy()
  })

  it('uses the old button classes so pages keep their look', () => {
    render(<><Button variant="primary">A</Button><Button variant="danger">B</Button><Button variant="secondary" size="sm">C</Button></>, { container })
    expect(screen.getByRole('button', { name: 'A' }).className).toContain('button primary')
    expect(screen.getByRole('button', { name: 'B' }).className).toContain('button destructive')
    expect(screen.getByRole('button', { name: 'C' }).className).toContain('small')
  })

  it('gives an icon button its required accessible name and no title', () => {
    render(<IconButton icon="x" label="Close" />, { container })
    const button = screen.getByRole('button', { name: 'Close' })
    expect(button.getAttribute('title')).toBeNull()
    expect(button.className).toContain('ui-icon-button')
  })
})
