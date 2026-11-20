import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Steps, commandStepState } from './Steps'
import { Timeline } from './Timeline'
import { PageHeader } from './PageHeader'
import { setUpKitDom, tearDownKitDom } from './testUtils'

class ResizeObserverStub { observe() {} disconnect() {} }

describe('Steps and Timeline', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('maps command statuses to the three steps', () => {
    expect(commandStepState('PENDING')).toBe('waiting')
    expect(commandStepState('WAITING_FOR_ZKT')).toBe('waiting')
    expect(commandStepState('DISPATCHED')).toBe('sent')
    expect(commandStepState('RUNNING')).toBe('sent')
    expect(commandStepState('SUCCEEDED')).toBe('confirmed')
    expect(commandStepState('FAILED')).toBe('failed')
    expect(commandStepState('CANCEL_REQUESTED')).toBe('cancelling')
    expect(commandStepState('CANCELLED')).toBe('cancelled')
    expect(commandStepState('EXPIRED')).toBe('timed-out')
    expect(commandStepState(undefined)).toBe('waiting')
  })

  it('marks the current step with aria-current="step"', () => {
    const { rerender } = render(<Steps state="waiting" />, { container })
    expect(screen.getByText('Waiting').closest('li')?.getAttribute('aria-current')).toBe('step')
    expect(screen.getByText('Sent to machine')).toBeTruthy()
    expect(screen.getByText('Confirmed')).toBeTruthy()
    rerender(<Steps state="sent" />)
    expect(screen.getByText('Sent to machine').closest('li')?.getAttribute('aria-current')).toBe('step')
    rerender(<Steps state="failed" failedReason="the machine is full" />)
    const failed = screen.getByText('Failed: the machine is full').closest('li') as HTMLElement
    expect(failed.getAttribute('aria-current')).toBe('step')
    expect(failed.className).toContain('is-failed')
    rerender(<Steps state="timed-out" />)
    expect(screen.getByText('Timed out: the machine did not answer')).toBeTruthy()
    rerender(<Steps state="cancelling" />)
    expect(screen.getByRole('status').textContent).toBe('Cancelling…')
  })

  it('lists dated items with optional technical details', () => {
    render(<Timeline label="Activity" items={[{ id: '1', time: 'Today, 3:04 pm', title: 'Link box restarted', technical: 'restart id 77' }, { id: '2', time: 'Yesterday, 1:04 pm', title: 'Machine connected', tone: 'working' }]} />, { container })
    const list = screen.getByRole('list', { name: 'Activity' })
    expect(list.querySelectorAll('li')).toHaveLength(2)
    expect(list.querySelectorAll('[data-technical]')).toHaveLength(1)
    expect(screen.getByText('Today, 3:04 pm').tagName).toBe('TIME')
  })
})

describe('PageHeader', () => {
  let container: HTMLElement
  beforeEach(() => {
    container = setUpKitDom()
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('has a focusable h1, one primary action and a More actions menu', () => {
    const onExport = vi.fn()
    render(<PageHeader title="Devices" description="Every device and whether it is working." primaryAction={<button type="button">Add</button>} moreActions={[{ id: 'export', label: 'Download list', onSelect: onExport }]} />, { container })
    const heading = screen.getByRole('heading', { level: 1, name: 'Devices' })
    expect(heading.tabIndex).toBe(-1)
    expect(screen.getByText('Every device and whether it is working.').className).toBe('page-header-description')
    const more = screen.getByRole('button', { name: 'More actions' })
    expect(more.getAttribute('aria-haspopup')).toBe('menu')
    fireEvent.click(more)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Download list' }))
    expect(onExport).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).toBeNull()
  })
})
