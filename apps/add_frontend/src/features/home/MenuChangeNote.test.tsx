import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { getPrefs, resetPrefs } from '../../lib/prefs'
import { expectPlainText } from '../../words/testing'
import { MENU_NOTE_UNTIL, MenuChangeNote, menuNoteVisible } from './MenuChangeNote'

describe('menu-change note', () => {
  afterEach(() => resetPrefs())
  const before = Date.parse(MENU_NOTE_UNTIL) - 86_400_000

  it('shows until the end date or until Got it', () => {
    expect(menuNoteVisible(false, before)).toBe(true)
    expect(menuNoteVisible(true, before)).toBe(false)
    expect(menuNoteVisible(false, Date.parse(MENU_NOTE_UNTIL) + 1)).toBe(false)
  })

  it('names the old menu words on purpose and remembers Got it', () => {
    const { container } = render(<MenuChangeNote now={before} />)
    expect(screen.getByText('The menu has new names.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /What moved/ }))
    const old = Array.from(container.querySelectorAll('[data-old-name]')).map((node) => node.textContent)
    expect(old).toEqual(['Fleet', 'Alerts', 'Users', 'Reconciliation', 'Firmware'])
    expectPlainText(container, { skip: '[data-old-name]' })
    fireEvent.click(screen.getByRole('button', { name: 'Got it' }))
    expect(getPrefs().menuNoteSeen).toBe(true)
    expect(screen.queryByText('The menu has new names.')).toBeNull()
  })
})
