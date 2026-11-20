// Use as a spare / Put back in use (04-device Advanced tools 2): one click,
// "Saving…", then a toast with Undo (8 s) that sends the opposite change.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import { deviceSnapshots } from '../../deviceData'
import type { Device } from '../../types'
import { Button, useToast } from '../../ui'
import { ADVANCED_WORDS } from '../../words/device'
import { friendlyName } from './deviceNaming'

export function useSpareToggle(device: Device, onInventoryChanged: () => Promise<void>) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // The same Undo next to the button for 8 s: a toast outside the focus-trapped
  // device view can't be reached with the keyboard.
  const [undo, setUndo] = useState<{ text: string; spare: boolean } | null>(null)
  const undoTimer = useRef<number | null>(null)
  useEffect(() => () => { if (undoTimer.current) window.clearTimeout(undoTimer.current) }, [])
  const name = friendlyName(device)
  const change = useCallback(async (spare: boolean, offerUndo: boolean) => {
    setBusy(true)
    setError('')
    setUndo(null)
    try {
      const updated = await api<Device>(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/spare`, {
        method: 'PATCH',
        body: JSON.stringify({ spare }),
      })
      if (updated && typeof updated === 'object' && 'connector_id' in updated) deviceSnapshots.put([updated])
      toast.show({
        tone: 'success',
        text: spare ? ADVANCED_WORDS.movedToSpare(name) : ADVANCED_WORDS.backInUse(name),
        duration: 8_000,
        ...(offerUndo ? { action: { label: ADVANCED_WORDS.undo, onClick: () => void change(!spare, false) } } : {}),
      })
      if (offerUndo) {
        setUndo({ text: spare ? ADVANCED_WORDS.movedToSpare(name) : ADVANCED_WORDS.backInUse(name), spare: !spare })
        if (undoTimer.current) window.clearTimeout(undoTimer.current)
        undoTimer.current = window.setTimeout(() => setUndo(null), 8_000)
      }
      await onInventoryChanged()
    } catch {
      setError(ADVANCED_WORDS.spareFailed)
    } finally {
      setBusy(false)
    }
  }, [device.connector_id, name, onInventoryChanged, toast])
  const toggle = useCallback(() => change(!device.is_spare, true), [change, device.is_spare])
  const undoNow = useCallback(() => { if (undo) void change(undo.spare, false) }, [change, undo])
  return { busy, error, toggle, undo: undo?.text ?? null, undoNow }
}

export function SpareTool({ device, onInventoryChanged }: { device: Device; onInventoryChanged: () => Promise<void> }) {
  const { busy, error, toggle, undo, undoNow } = useSpareToggle(device, onInventoryChanged)
  const spare = Boolean(device.is_spare)
  return (
    <section className="dv-tool-card" aria-labelledby="dv-spare-title">
      <h4 id="dv-spare-title">{spare ? ADVANCED_WORDS.spareTitleSpare : ADVANCED_WORDS.spareTitleInUse}</h4>
      <p>{spare ? ADVANCED_WORDS.spareText : ADVANCED_WORDS.inUse}</p>
      <div className="dv-tool-actions">
        <Button busy={busy} busyLabel={ADVANCED_WORDS.saving} onClick={() => void toggle()}>
          {spare ? ADVANCED_WORDS.putBack : ADVANCED_WORDS.useAsSpare}
        </Button>
      </div>
      {undo && <p className="dv-spare-undo">{undo} <button type="button" className="text-button" onClick={undoNow}>{ADVANCED_WORDS.undo}</button></p>}
      {error && <p className="field-error" role="alert">{error}</p>}
    </section>
  )
}
