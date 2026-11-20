import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRef, useState } from 'react'
import { Sheet } from './Sheet'
import { setUpKitDom, tearDownKitDom } from './testUtils'

function SheetHarness({ dirty = false }: { dirty?: boolean }) {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)
  return <div className="app-workspace">
    <button ref={trigger} type="button" onClick={() => setOpen(true)}>Open details</button>
    {open && <Sheet title="Building 9 · Device 3" dirty={dirty && note.length > 0} onClose={() => setOpen(false)} footer={<button type="button">Save</button>}>
      <label>Note<textarea value={note} onChange={(event) => setNote(event.target.value)} /></label>
    </Sheet>}
  </div>
}

describe('Sheet', () => {
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('is a side panel from 761 px and a bottom sheet with a handle up to 760 px', () => {
    let container = setUpKitDom({ phone: false })
    const { unmount } = render(<SheetHarness />, { container })
    fireEvent.click(screen.getByRole('button', { name: 'Open details' }))
    expect(screen.getByRole('dialog', { name: 'Building 9 · Device 3' }).getAttribute('data-placement')).toBe('side')
    expect(document.querySelector('.ui-sheet-handle')).toBeNull()
    unmount()
    tearDownKitDom()

    container = setUpKitDom({ phone: true })
    render(<SheetHarness />, { container })
    fireEvent.click(screen.getByRole('button', { name: 'Open details' }))
    expect(screen.getByRole('dialog', { name: 'Building 9 · Device 3' }).getAttribute('data-placement')).toBe('bottom')
    expect(document.querySelector('.ui-sheet-handle')).not.toBeNull()
    expect(document.querySelector('.ui-sheet-footer')?.textContent).toBe('Save')
  })

  it('closes on a backdrop tap and gives focus back to the opener', async () => {
    const container = setUpKitDom()
    render(<SheetHarness />, { container })
    const trigger = screen.getByRole('button', { name: 'Open details' })
    trigger.focus()
    fireEvent.click(trigger)
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' })))
    fireEvent.mouseDown(document.querySelector('.ui-sheet-backdrop') as HTMLElement)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(document.getElementById('root')?.hasAttribute('inert')).toBe(false)
  })

  it('keeps typed text: a backdrop tap is ignored while dirty and Escape asks first', () => {
    const container = setUpKitDom({ phone: true })
    render(<SheetHarness dirty />, { container })
    fireEvent.click(screen.getByRole('button', { name: 'Open details' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Note' }), { target: { value: 'Cable loose' } })
    fireEvent.mouseDown(document.querySelector('.ui-sheet-backdrop') as HTMLElement)
    expect(screen.getByRole('dialog', { name: 'Building 9 · Device 3' })).toBeTruthy()
    expect((screen.getByRole('textbox', { name: 'Note' }) as HTMLTextAreaElement).value).toBe('Cable loose')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByRole('alertdialog', { name: 'Discard your changes?' })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect((screen.getByRole('textbox', { name: 'Note' }) as HTMLTextAreaElement).value).toBe('Cable loose')
  })

  it('closes when the handle is swiped down far enough', () => {
    const container = setUpKitDom({ phone: true })
    const onClose = vi.fn()
    render(<Sheet title="More" onClose={onClose}>Body</Sheet>, { container })
    const handle = document.querySelector('.ui-sheet-handle') as HTMLElement
    fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientY: 140, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientY: 140, pointerId: 1 })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientY: 220, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientY: 220, pointerId: 1 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
