// "The menu has new names" (09-delivery D, DLV-4). Shown on Home until a fixed
// end date or until "Got it"; the dismissal is the `menuNoteSeen` choice in
// add.prefs (D-25). Old names sit in <s data-old-name> on purpose. Removed in PR-9.
import { useState } from 'react'
import { Icon } from '../../Icon'
import { usePrefs } from '../../lib/prefs'
import { MENU_NOTE_WORDS } from '../../words/home'

/** 14 days after the new menu goes live. */
export const MENU_NOTE_UNTIL = '2026-12-15T00:00:00+05:00'

export function menuNoteVisible(seen: boolean, now: number = Date.now()) {
  return !seen && now < Date.parse(MENU_NOTE_UNTIL)
}

export function MenuChangeNote({ now }: { now?: number }) {
  const [prefs, setPrefs] = usePrefs()
  const [open, setOpen] = useState(false)
  if (!menuNoteVisible(prefs.menuNoteSeen, now)) return null
  const dismiss = () => setPrefs({ menuNoteSeen: true })
  return (
    <section className="menu-note" aria-labelledby="menu-note-title">
      <div className="menu-note-line">
        <Icon name="info" size="sm" />
        <p>
          <strong id="menu-note-title">{MENU_NOTE_WORDS.title}.</strong>{' '}
          <span className="menu-note-body">{MENU_NOTE_WORDS.body}</span>
        </p>
        <button type="button" className="text-button menu-note-toggle" aria-expanded={open} aria-controls="menu-note-moves" onClick={() => setOpen((value) => !value)}>
          {MENU_NOTE_WORDS.whatMoved} <Icon name={open ? 'chevronDown' : 'chevron'} size="sm" />
        </button>
        <button type="button" className="icon-button menu-note-close" aria-label={MENU_NOTE_WORDS.close} onClick={dismiss}><Icon name="x" size="sm" /></button>
      </div>
      {open && (
        <div id="menu-note-moves" className="menu-note-moves">
          <ul>
            {MENU_NOTE_WORDS.moves.map((move) => (
              <li key={move.old}><s data-old-name>{move.old}</s> <span aria-hidden="true">→</span><span className="sr-only">is now</span> {move.now}</li>
            ))}
          </ul>
          <button type="button" className="button secondary small" onClick={dismiss}>{MENU_NOTE_WORDS.gotIt}</button>
        </div>
      )}
    </section>
  )
}
