import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StatTile } from './StatTile'
import { setUpKitDom, tearDownKitDom } from './testUtils'

describe('StatTile', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('formats numbers the Pakistani way whatever the browser locale', () => {
    render(<StatTile label="Check-ins today" value={158560} />, { container })
    expect(screen.getByText('158,560')).toBeTruthy()
  })

  it('shows a skeleton while loading, never a fake zero', () => {
    render(<StatTile label="Offline" value={0} loading />, { container })
    expect(screen.queryByText('0')).toBeNull()
    expect(document.querySelector('.ui-stat-skeleton')).not.toBeNull()
    expect(screen.getByText('Loading Offline…').className).toBe('sr-only')
  })

  it('always shows a chevron when it opens something, as a link or a button', () => {
    const onClick = vi.fn((event: { preventDefault: () => void }) => event.preventDefault())
    render(<><StatTile label="Offline" value={2} tone="offline" href="/devices?status=offline" onClick={onClick} /><StatTile label="Working" value={20} /></>, { container })
    const link = screen.getByRole('link', { name: /Offline/ })
    expect(link.getAttribute('href')).toBe('/devices?status=offline')
    expect(link.querySelector('.ui-stat-chevron')).not.toBeNull()
    fireEvent.click(link)
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Working').closest('.ui-stat-tile')?.querySelector('.ui-stat-chevron')).toBeNull()
  })

  it('tints briefly when the value changes', () => {
    vi.useFakeTimers()
    const { rerender } = render(<StatTile label="Offline" value={2} />, { container })
    rerender(<StatTile label="Offline" value={3} />)
    expect(document.querySelector('.ui-stat-tile')?.className).toContain('is-changed')
    act(() => { vi.advanceTimersByTime(700) })
    expect(document.querySelector('.ui-stat-tile')?.className).not.toContain('is-changed')
  })
})
