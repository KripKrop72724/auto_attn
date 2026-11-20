// Use as a spare / Put back in use (04-device Advanced tools 2): one click,
// "Saving…", then a toast with Undo (8 s) that sends the opposite change.
import { useCallback, useState } from 'react'
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
  const name = friendlyName(device)
  const change = useCallback(async (spare: boolean, offerUndo: boolean) => {
    setBusy(true)
    setError('')
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
      await onInventoryChanged()
    } catch {
      setError(ADVANCED_WORDS.spareFailed)
    } finally {
      setBusy(false)
    }
  }, [device.connector_id, name, onInventoryChanged, toast])
  const toggle = useCallback(() => change(!device.is_spare, true), [change, device.is_spare])
  return { busy, error, toggle }
}

export function SpareTool({ device, onInventoryChanged }: { device: Device; onInventoryChanged: () => Promise<void> }) {
  const { busy, error, toggle } = useSpareToggle(device, onInventoryChanged)
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
      {error && <p className="field-error" role="alert">{error}</p>}
    </section>
  )
}
