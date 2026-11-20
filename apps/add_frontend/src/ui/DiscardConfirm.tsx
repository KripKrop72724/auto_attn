import { useEffect, useId, useRef } from 'react'
import { UI_TEXT } from './uiText'

// Asked before a dialog or sheet with typed input closes: "Discard your
// changes?" [Keep editing] [Discard]. Shown inside the layer, which goes inert
// behind it, so the typed text is still there after "Keep editing".
export function DiscardConfirm({ onKeep, onDiscard }: { onKeep: () => void; onDiscard: () => void }) {
  const id = useId()
  const keepRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { keepRef.current?.focus() }, [])
  return (
    <div className="ui-discard-backdrop">
      <div className="ui-discard" role="alertdialog" aria-modal="true" aria-labelledby={`${id}-title`} aria-describedby={`${id}-text`}>
        <h2 id={`${id}-title`}>{UI_TEXT.discardTitle}</h2>
        <p id={`${id}-text`}>{UI_TEXT.discardBody}</p>
        <div className="ui-discard-actions">
          <button ref={keepRef} type="button" className="button secondary" onClick={onKeep}>{UI_TEXT.keepEditing}</button>
          <button type="button" className="button destructive" onClick={onDiscard}>{UI_TEXT.discard}</button>
        </div>
      </div>
    </div>
  )
}
