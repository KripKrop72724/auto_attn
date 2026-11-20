import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { Dialog } from './Dialog'
import { setUpKitDom, tearDownKitDom } from './testUtils'

function FormDialog({ onClose = () => undefined, dirtyWhenTyped = true, size }: { onClose?: () => void; dirtyWhenTyped?: boolean; size?: 'sm' | 'md' | 'lg' }) {
  const [open, setOpen] = useState(true)
  const [note, setNote] = useState('')
  return open
    ? <Dialog titleId="note-title" title="Add a note" size={size} dirty={dirtyWhenTyped && note.length > 0} initialFocus="first-field" onClose={() => { setOpen(false); onClose() }}>
        <div className="dialog-body"><label>Note<input value={note} onChange={(event) => setNote(event.target.value)} /></label></div>
      </Dialog>
    : <p>Closed</p>
}

function NestedDialogs() {
  const [outer, setOuter] = useState(true)
  const [inner, setInner] = useState(false)
  return <>
    {outer && <Dialog titleId="outer" title="Outer" onClose={() => setOuter(false)}><div className="dialog-body"><button type="button" onClick={() => setInner(true)}>Open inner</button></div></Dialog>}
    {inner && <Dialog titleId="inner" title="Inner" onClose={() => setInner(false)}><div className="dialog-body"><button type="button">Inner action</button></div></Dialog>}
  </>
}

describe('Dialog', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('ignores a click on the backdrop by default, but Escape and Close still work', async () => {
    const onClose = vi.fn()
    render(<Dialog titleId="t" title="Details" onClose={onClose}><div className="dialog-body">Body</div></Dialog>, { container })
    const backdrop = document.querySelector('.dialog-backdrop') as HTMLElement
    fireEvent.mouseDown(backdrop)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('closes on the backdrop only when asked to', () => {
    const onClose = vi.fn()
    render(<Dialog titleId="t" title="Details" dismissOnBackdrop onClose={onClose}><div className="dialog-body">Body</div></Dialog>, { container })
    fireEvent.mouseDown(document.querySelector('.dialog-backdrop') as HTMLElement)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('focuses the Close button first by default and portals into #overlay-root', async () => {
    render(<Dialog titleId="t" title="Details" onClose={() => undefined}><div className="dialog-body"><button type="button">Act</button></div></Dialog>, { container })
    const close = screen.getByRole('button', { name: 'Close' })
    await waitFor(() => expect(document.activeElement).toBe(close))
    expect(screen.getByRole('dialog', { name: 'Details' }).closest('#overlay-root')).not.toBeNull()
    expect(document.getElementById('root')?.hasAttribute('inert')).toBe(true)
  })

  it('focuses the first field when initialFocus is first-field, and applies the size class', async () => {
    render(<FormDialog size="sm" />, { container })
    const input = screen.getByRole('textbox', { name: 'Note' })
    await waitFor(() => expect(document.activeElement).toBe(input))
    expect(document.querySelector('.dialog-panel')?.className).toContain('dialog-size-sm')
  })

  it('asks before throwing away typed text, and keeps it after Keep editing', async () => {
    const onClose = vi.fn()
    render(<FormDialog onClose={onClose} />, { container })
    fireEvent.change(screen.getByRole('textbox', { name: 'Note' }), { target: { value: 'Swapped the cable' } })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByRole('alertdialog', { name: 'Discard your changes?' })).toBeTruthy()
    expect(screen.getByText('What you typed will be lost.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect((screen.getByRole('textbox', { name: 'Note' }) as HTMLInputElement).value).toBe('Swapped the cable')
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Closed')).toBeTruthy()
  })

  it('lets only the innermost dialog own Escape', async () => {
    render(<NestedDialogs />, { container })
    fireEvent.click(screen.getByRole('button', { name: 'Open inner' }))
    expect(screen.getByRole('dialog', { name: 'Inner' })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Inner' })).toBeNull()
    expect(screen.getByRole('dialog', { name: 'Outer' })).toBeTruthy()
    expect(document.getElementById('root')?.hasAttribute('inert')).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Outer' })).toBeNull()
    expect(document.getElementById('root')?.hasAttribute('inert')).toBe(false)
  })
})
