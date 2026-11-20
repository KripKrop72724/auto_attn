import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NewVersionNotice } from './NewVersionNotice'
import { PageErrorBoundary } from './PageError'

function Broken({ error }: { error: unknown }): null {
  throw error
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/home')
})

describe('page error card', () => {
  it('reloads once by itself for a missing page file, then shows the plain card', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })
    const error = new TypeError('Failed to fetch dynamically imported module: https://add.test/assets/Attendance-1.js')
    const first = render(<PageErrorBoundary><Broken error={error} /></PageErrorBoundary>)
    expect(reload).toHaveBeenCalledOnce()
    expect(screen.queryByRole('alert')).toBeNull()
    first.unmount()

    render(<PageErrorBoundary><Broken error={error} /></PageErrorBoundary>)
    expect(reload).toHaveBeenCalledOnce()
    expect(screen.getByRole('heading', { name: "This page couldn't open" })).toBeTruthy()
    expect(screen.getByText('The app may have been updated. Reload to continue.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Go to Home' }).getAttribute('href')).toBe('/home')
    // The raw message only under Technical details, closed.
    const details = document.querySelector('details[data-technical]') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.textContent).toContain('Failed to fetch dynamically imported module')
    vi.unstubAllGlobals()
  })

  it('shows any other failure without reloading', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    render(<PageErrorBoundary><Broken error={new Error('Cannot read properties of undefined')} /></PageErrorBoundary>)
    expect(screen.getByRole('heading', { name: "This page couldn't open" })).toBeTruthy()
    expect(screen.queryByText(/Cannot read properties/)?.closest('[data-technical]')).toBeTruthy()
  })
})

describe('new version notice', () => {
  it('stays hidden until a new version is ready and never reloads by itself', () => {
    const reload = vi.fn()
    const { rerender } = render(<NewVersionNotice available={false} onReload={reload} />)
    expect(screen.queryByRole('status')).toBeNull()
    rerender(<NewVersionNotice available onReload={reload} />)
    expect(screen.getByText('A new version of the dashboard is ready.')).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()
    screen.getByRole('button', { name: 'Reload' }).click()
    expect(reload).toHaveBeenCalledOnce()
  })
})
