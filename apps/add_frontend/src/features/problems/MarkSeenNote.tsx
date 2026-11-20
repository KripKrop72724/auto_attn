// "Mark as seen with a note…" (PRB-6). The backend keeps a note only with the
// first mark, so the note is sent with the click. No password: marking as seen
// changes nothing on the device.
import { useId, useState } from 'react'
import { Button, Sheet } from '../../ui'
import { MARK_SEEN, problemWords } from '../../words/problems'
import type { ProblemRow } from './types'

export function MarkSeenNote({ row, place, busy, onSubmit, onClose }: {
  row: ProblemRow
  place?: string
  busy?: boolean
  onSubmit: (note: string) => Promise<unknown> | unknown
  onClose: () => void
}) {
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const [note, setNote] = useState('')
  const title = problemWords(row).title
  return (
    <Sheet
      title={MARK_SEEN.title}
      description={place ? `${title} · ${place}` : title}
      onClose={onClose}
      dirty={Boolean(note.trim())}
      initialFocus="first-field"
      className="mark-seen-note"
      footer={(
        <div className="sheet-actions">
          <Button variant="secondary" onClick={onClose} disabled={busy}>{MARK_SEEN.cancel}</Button>
          <Button variant="primary" icon="eye" busy={busy} busyLabel="Marking…" onClick={() => onSubmit(note)}>{MARK_SEEN.submit}</Button>
        </div>
      )}
    >
      <div className="mark-fixed-field">
        <label className="mark-fixed-label" htmlFor={`note-${id}`}>{MARK_SEEN.note}</label>
        <textarea id={`note-${id}`} rows={3} maxLength={500} value={note} placeholder={MARK_SEEN.placeholder} onChange={(event) => setNote(event.target.value)} />
      </div>
    </Sheet>
  )
}
