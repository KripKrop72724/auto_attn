import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TechnicalDetails, TechnicalDetailsProvider } from './TechnicalDetails'
import { ErrorState } from './ErrorState'
import { EmptyState } from './EmptyState'
import { Callout } from './Callout'
import { setUpKitDom, tearDownKitDom } from './testUtils'

describe('TechnicalDetails', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('is closed by default and marked data-technical', () => {
    render(<TechnicalDetails>MAC ac:27:6e:a3:45:44</TechnicalDetails>, { container })
    const details = document.querySelector('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.hasAttribute('data-technical')).toBe(true)
    expect(screen.getByText('Technical details')).toBeTruthy()
  })

  it('opens when the person turned on Show technical details', () => {
    render(<TechnicalDetailsProvider show><TechnicalDetails>Code HIK_PARSE</TechnicalDetails></TechnicalDetailsProvider>, { container })
    expect((document.querySelector('details') as HTMLDetailsElement).open).toBe(true)
  })

  it('copies the text and says Copied for 1.5 s', async () => {
    vi.useFakeTimers()
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<TechnicalDetails copyText="request 1a2b">Reference 1a2b</TechnicalDetails>, { container })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy' })) })
    expect(writeText).toHaveBeenCalledWith('request 1a2b')
    expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
    act(() => { vi.advanceTimersByTime(1600) })
    expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
  })
})

describe('ErrorState, EmptyState and Callout', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('says it plainly, keeps the raw error in Technical details and retries with a busy button', async () => {
    let finish: () => void = () => undefined
    const onRetry = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    const error = Object.assign(new Error('Request failed (502)'), { status: 502, requestId: 'req-9f2c', plainMessage: 'Something went wrong on our side. Try again in a minute.' })
    render(<ErrorState error={error} onRetry={onRetry} />, { container })
    const alert = screen.getByRole('alert')
    expect(screen.getByRole('heading', { name: "Couldn't load this" })).toBeTruthy()
    expect(screen.getByText('Something went wrong on our side. Try again in a minute.')).toBeTruthy()
    const technical = alert.querySelector('[data-technical]') as HTMLElement
    expect(technical.textContent).toContain('Reference: req-9f2c')
    expect(technical.textContent).toContain('Request failed (502)')
    expect(alert.textContent?.replace(technical.textContent || '', '')).not.toContain('502')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.getByRole('button', { name: 'Trying…' }).getAttribute('aria-busy')).toBe('true')
    await act(async () => { finish() })
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('never shows a raw message when no plain sentence is given', () => {
    render(<ErrorState error={new Error('Failed to fetch')} />, { container })
    const alert = screen.getByRole('alert')
    const visible = alert.textContent?.replace(alert.querySelector('[data-technical]')?.textContent || '', '')
    expect(visible).not.toContain('Failed to fetch')
    expect(screen.getByText('Something went wrong. Try again in a minute.')).toBeTruthy()
  })

  it('has a default empty title and a callout tone', () => {
    render(<><EmptyState>No devices match these filters.</EmptyState><Callout tone="warning" title="Switched off">Setting up new devices is switched off.</Callout></>, { container })
    expect(screen.getByRole('heading', { name: 'Nothing here yet' })).toBeTruthy()
    expect(document.querySelector('.ui-callout')?.getAttribute('data-tone')).toBe('warning')
  })
})
