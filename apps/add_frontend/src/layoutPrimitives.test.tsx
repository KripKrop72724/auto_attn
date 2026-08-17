import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useRef, useState } from 'react'
import { AnchoredLayer } from './AnchoredLayer'
import { Dialog } from './App'

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}

function AnchoredHarness() {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  return <>
    <button ref={triggerRef} type="button" onClick={() => setOpen(true)}>Open actions</button>
    {open && <AnchoredLayer anchorRef={triggerRef} preferredWidth={280} onDismiss={(reason) => { setOpen(false); if (reason === 'escape') triggerRef.current?.focus() }}><div role="menu">Viewport menu</div></AnchoredLayer>}
  </>
}

function DialogHarness() {
  const [open, setOpen] = useState(false)
  return <div className="app-workspace"><button type="button" onClick={() => setOpen(true)}>Open evidence</button>{open && <Dialog titleId="evidence-title" title="Evidence" description="Immutable evidence" onClose={() => setOpen(false)}><div className="dialog-body"><button type="button">Review evidence</button></div></Dialog>}</div>
}

describe('viewport layout primitives', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"><div id="test-app"></div></div><div id="overlay-root"></div>'
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight')
    document.body.innerHTML = ''
  })

  it('portals and flips an anchored layer inside the visual viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 800 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 700 })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.textContent === 'Open actions') return { x: 700, y: 650, top: 650, right: 790, bottom: 690, left: 700, width: 90, height: 40, toJSON: () => ({}) }
      return { x: 0, y: 0, top: 0, right: 280, bottom: 220, left: 0, width: 280, height: 220, toJSON: () => ({}) }
    })
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get(this: HTMLElement) { return this.classList?.contains('anchored-layer') ? 220 : 0 } })

    render(<AnchoredHarness />, { container: document.getElementById('test-app') as HTMLElement })
    fireEvent.click(screen.getByRole('button', { name: 'Open actions' }))
    const layer = await waitFor(() => document.querySelector<HTMLElement>('.anchored-layer') as HTMLElement)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(layer.dataset.placement).toBe('top'))
    expect(layer.parentElement?.id).toBe('overlay-root')
    expect(Number.parseFloat(layer.style.left)).toBeLessThanOrEqual(508)
    expect(Number.parseFloat(layer.style.top)).toBeGreaterThanOrEqual(12)

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('.anchored-layer')).toBeNull())
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open actions' }))
  })

  it('portals dialogs, locks the workspace, and restores focus and scroll', async () => {
    render(<DialogHarness />, { container: document.getElementById('test-app') as HTMLElement })
    const workspace = document.querySelector<HTMLElement>('.app-workspace') as HTMLElement
    workspace.scrollTop = 84
    const trigger = screen.getByRole('button', { name: 'Open evidence' })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: 'Evidence' })
    expect(dialog.closest('#overlay-root')).not.toBeNull()
    expect(document.getElementById('root')?.hasAttribute('inert')).toBe(true)
    expect(document.getElementById('root')?.getAttribute('aria-hidden')).toBe('true')
    expect(workspace.style.overflow).toBe('hidden')
    const close = await screen.findByRole('button', { name: 'Close dialog' })
    await waitFor(() => expect(document.activeElement).toBe(close))

    fireEvent.click(close)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Evidence' })).toBeNull())
    expect(document.getElementById('root')?.hasAttribute('inert')).toBe(false)
    expect(document.getElementById('root')?.hasAttribute('aria-hidden')).toBe(false)
    expect(workspace.style.overflow).toBe('')
    expect(workspace.scrollTop).toBe(84)
    expect(document.activeElement).toBe(trigger)
  })
})
