// Refresh people list from the machine (04-device Tools 1, shared with the
// People tab). Sends one request with an idempotency key, can't be sent twice
// while one is on its way, and follows it to Confirmed or Didn't work.
import { useState } from 'react'
import { api } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { ago } from '../../lib/time'
import { isFinalCommand, useCommandTracker } from '../../lib/useCommandTracker'
import type { Command } from '../../types'
import { Button, useToast } from '../../ui'
import { formatNumber } from '../../lib/format'
import { TOOLS_WORDS, type DeviceDetail } from '../../words/device'
import { CommandSteps } from './CommandSteps'

export function RefreshPeopleAction({ device, revision, onRefreshed, showText = true }: {
  device: DeviceDetail
  revision: number
  onRefreshed?: () => Promise<void> | void
  showText?: boolean
}) {
  const toast = useToast()
  const [command, setCommand] = useState<Command | null>(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const tracker = useCommandTracker(command, revision, (final) => {
    if (String(final.status).toUpperCase() !== 'SUCCEEDED') return
    void Promise.resolve(onRefreshed?.()).finally(() => {
      toast.show({ tone: 'success', text: TOOLS_WORDS.refreshed(device.zkt?.user_count != null ? formatNumber(device.zkt.user_count) : null) })
    })
  })
  const inFlight = sending || Boolean(command && !isFinalCommand(tracker.command ?? command))
  const start = async () => {
    if (inFlight) return
    setSending(true)
    setError('')
    try {
      const answer = await api<Command | { command: Command }>(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/users/refresh`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: idempotency('refresh-people') }),
      })
      const next = answer && 'command' in answer && answer.command ? answer.command : answer as Command
      setCommand(next && next.command_id ? next : null)
    } catch {
      setError(TOOLS_WORDS.refreshFailed)
    } finally {
      setSending(false)
    }
  }
  const lastRead = device.zkt?.identity_snapshot_observed_at
  const done = tracker.step === 'confirmed' ? TOOLS_WORDS.refreshed(device.zkt?.user_count != null ? formatNumber(device.zkt.user_count) : null) : undefined
  return (
    <div className="dv-action">
      {showText && <p>{TOOLS_WORDS.refreshText(lastRead ? ago(lastRead) : null)}</p>}
      <div className="dv-tool-actions">
        <Button variant="primary" busy={sending} busyLabel={TOOLS_WORDS.refreshing} disabled={inFlight} onClick={() => void start()}>
          {TOOLS_WORDS.refreshNow}
        </Button>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {command && <CommandSteps
        tracker={tracker}
        done={done}
        onRetry={() => { setCommand(null); void start() }}
        onCancelled={() => toast.show({ tone: 'success', text: TOOLS_WORDS.cancelled })}
        onCancelFailed={() => toast.show({ tone: 'error', text: TOOLS_WORDS.cancelFailed })}
      />}
    </div>
  )
}
