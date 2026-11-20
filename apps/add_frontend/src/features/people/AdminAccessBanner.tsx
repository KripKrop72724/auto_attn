// Temporary admin access (07-people P10): who has it and a live countdown,
// with one "End now" button. Ending keeps today's typed "REVOKE ACCESS"
// confirmation until the owner decides (D-19, PR-10).
import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { Command } from '../../types'
import { Dialog } from '../../ui'
import { Icon } from '../../Icon'
import { clock } from '../../lib/time'
import { ADMIN_ACCESS } from '../../words/people'
import { DialogError, peopleErrorText } from './errors'
import type { PeopleLease } from './model'

export const REVOKE_PHRASE = 'REVOKE ACCESS'

const twoDigits = (value: number) => String(value).padStart(2, '0')

/** "07:42" for the time left; "00:00" once it is over. */
export function countdown(expiresAt: string | null | undefined, now: number): string | null {
  if (!expiresAt) return null
  const end = Date.parse(expiresAt)
  if (Number.isNaN(end)) return null
  const left = Math.max(0, Math.round((end - now) / 1000))
  return `${twoDigits(Math.floor(left / 60))}:${twoDigits(left % 60)}`
}

export function AdminAccessBanner({ lease, onEnd }: { lease: PeopleLease; onEnd: () => void }) {
  const [now, setNow] = useState(() => Date.now())
  const state = String(lease.state || '').toUpperCase()
  const running = state === 'ACTIVE' || state === 'REQUESTED' || state === 'GRANTING'
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  const left = countdown(lease.expires_at, now)
  const secondsLeft = lease.expires_at ? Math.max(0, (Date.parse(lease.expires_at) - now) / 1000) : null
  const lastMinute = state === 'ACTIVE' && secondsLeft !== null && secondsLeft <= 60
  const fraction = secondsLeft === null ? 1 : Math.min(1, secondsLeft / 600)
  const who = ADMIN_ACCESS.who(lease.user_display_name, lease.user_id)
  let line: string
  if (state === 'ACTIVE') line = left ? ADMIN_ACCESS.endsIn(left, lease.expires_at ? clock(lease.expires_at) : '') : ADMIN_ACCESS.waitingEnd
  else if (state === 'FAILED') line = ADMIN_ACCESS.states.FAILED
  else line = ADMIN_ACCESS.states[state] || ADMIN_ACCESS.waitingEnd
  const announce = state === 'ACTIVE' && secondsLeft !== null
    ? secondsLeft <= 0 ? ADMIN_ACCESS.states.EXPIRED : Math.ceil(secondsLeft) === 300 ? ADMIN_ACCESS.fiveMinutes : Math.ceil(secondsLeft) === 60 ? ADMIN_ACCESS.oneMinute : ''
    : ''
  const ended = state === 'REVOKED' || state === 'EXPIRED'
  return (
    <section className={`people-admin-access ${lastMinute ? 'is-last-minute' : ''} ${state === 'FAILED' || state === 'OVERDUE' ? 'is-problem' : ''}`} aria-label={ADMIN_ACCESS.title} data-lease-state={state}>
      <span className="people-admin-ring" aria-hidden="true" style={{ ['--ring' as string]: `${Math.round(fraction * 360)}deg` }}><Icon name="shield" size="sm" /></span>
      <div className="people-admin-text">
        <p className="people-admin-title"><strong>{ADMIN_ACCESS.title}</strong>{who && <> · <span data-user-text>{who}</span></>}</p>
        <p className="people-admin-line" data-testid="admin-access-line">{line}</p>
        <span className="sr-only" aria-live="polite">{announce}</span>
      </div>
      {!ended && state !== 'FAILED' && (
        <button type="button" className="button destructive-outline" disabled={state === 'REVOKING'} onClick={onEnd}>{ADMIN_ACCESS.endNow}</button>
      )}
    </section>
  )
}

export function EndAdminAccessDialog({ lease, deviceName, onClose, onCommand, toast }: {
  lease: PeopleLease
  deviceName: string
  onClose: () => void
  onCommand: (command: Command) => void
  toast: { notice: (text: string) => void }
}) {
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ sentence: string; raw: string } | null>(null)
  const name = lease.user_display_name || ADMIN_ACCESS.someone
  const end = async () => {
    if (busy || confirmation !== REVOKE_PHRASE) return
    setBusy(true)
    setError(null)
    try {
      const response = await api<{ command: Command }>(`/api/v1/admin-leases/${lease.lease_id}/revoke`, { method: 'POST', body: '{}' })
      onCommand(response.command)
      toast.notice(ADMIN_ACCESS.ended)
      onClose()
    } catch (reason) {
      setError(peopleErrorText(reason, ADMIN_ACCESS.endFailed))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog titleId="end-admin-access-title" title={ADMIN_ACCESS.endTitle} description={ADMIN_ACCESS.endBody(name, deviceName)} onClose={onClose} size="sm" dirty={Boolean(confirmation)}>
      <form className="dialog-body" onSubmit={(event) => { event.preventDefault(); void end() }}>
        <p>{ADMIN_ACCESS.endCheck}</p>
        <label>
          <span>{ADMIN_ACCESS.typePhrase} <code className="people-typed-phrase" data-typed-phrase>{REVOKE_PHRASE}</code></span>
          <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} />
        </label>
        <DialogError error={error} />
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>{ADMIN_ACCESS.keep}</button>
          <button className="button destructive" type="submit" disabled={busy || confirmation !== REVOKE_PHRASE}>{busy ? ADMIN_ACCESS.ending : ADMIN_ACCESS.endNow}</button>
        </footer>
      </form>
    </Dialog>
  )
}
