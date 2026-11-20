import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, queryString } from '../api'
import { Dialog, idempotency, Metric } from '../App'
import { Icon } from '../Icon'
import type { Device } from '../types'
import { Disclosure } from '../ui'
import { deviceDivision } from '../lib/division'
import { deviceName } from '../lib/deviceNames'
import { plainReason } from '../words/attendanceReasons'
import { longTime, rowTime } from './attendance/attendanceWords'
import './SafeAttendanceRepair.css'
import './IdentityBacklogRelease.css'
import { plainErrorText } from '../lib/plainApiError'
import { ERROR_FALLBACK } from '../words/errors'
import { STEP_UP } from '../words/glossary'

type Group = { connector_id: string; source: string; age: string; held: number; provable: number; next_snapshot: number }
type Scope = { zone_id: string; as_of: string; until_id: number; window_hours: number; connectors: string[] }
type Approval = { scope: Scope; expires_at: string; signature: string | null }
type PreviewPage = {
  zone_id: string; scanned: number; next_after_id: number | null; groups: Group[]; as_of: string; until_id: number
  window_hours: number; release_enabled: boolean; automatic_zone_enabled: boolean
  devices: Array<{ connector_id: string; name: string | null }>; held_total?: number; reheld_outside_check?: number
  approval?: Approval
}
type Check = {
  zone: string; groups: Group[]; windowHours: number; approval: Approval; releaseEnabled: boolean
  automatic: boolean; names: Record<string, string>; reheld: number
}
export type BacklogRun = {
  job_id: string; zone_id: string; status: string; paused: boolean; waiting_reason: string | null; actor: string
  reason: string; connectors: string[]; checked_at: string | null; approved: number; released: number; delivered: number
  waiting_oracle: number; kept_held: number; left_to_snapshot: number; no_longer_held?: number; needs_review: number; created_at: string
  updated_at: string; completed_at: string | null
}
type Toast = { notice: (text: string) => void; error: (text: string) => void }

const root = '/api/v2/attendance-identity-backlog'
const pageSize = 200
const busyRetries = 20
const sources: Record<string, string> = { DUMP_RECONNECT: 'After the machine came back online', DUMP_STARTUP: 'After the machine restarted' }
const ageOrder = ['AUTOMATIC_WINDOW', 'UP_TO_7_DAYS', 'UP_TO_30_DAYS', 'OLDER']
const ageLabel = (age: string, hours: number, automatic: boolean) => ({
  AUTOMATIC_WINDOW: `Under ${hours} hours${automatic ? ' (often sent by themselves)' : ''}`,
  UP_TO_7_DAYS: 'Up to 7 days', UP_TO_30_DAYS: '7 to 30 days', OLDER: 'Over 30 days',
}[age] || age)
const runLabel: Record<string, string> = {
  RELEASING: 'Sending a few at a time', STOPPED_FOLLOWING: 'Stopped: following the sent check-ins to the HR system',
  COMPLETED: 'Done', STOPPED: 'Stopped',
}
const active = new Set(['RELEASING', 'STOPPED_FOLLOWING'])
const punches = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'check-in' : 'check-ins'}`
// A plain sentence only; the backend text never shows (writing rule 6).
const message = (error: unknown) => plainErrorText(error, ERROR_FALLBACK)
const placeOf = (device: Pick<Device, 'friendly_name' | 'display_name' | 'zone_id'>) => {
  if (device.friendly_name) return device.friendly_name
  const name = deviceName(device.display_name || device.zone_id)
  return name.unit ? `${name.location} · ${name.unit}` : name.location
}
/** "Karachi" for an area code: the location part of its devices' friendly names. */
const areaName = (zoneId: string, zoneName: string) => {
  const parsed = deviceName(zoneName || zoneId)
  return parsed.location || zoneName || zoneId
}
const groupKey = (group: Pick<Group, 'connector_id' | 'source' | 'age'>) => `${group.connector_id}|${group.source}|${group.age}`
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export function IdentityBacklogRelease({ devices, toast }: { devices: Device[]; toast: Toast }) {
  // Areas grouped Life then Health; the division comes from the area's devices.
  const zones = useMemo(() => [...new Map(
    devices.filter((device) => device.firmware_family !== 'hikvision' && !device.is_spare)
      .map((device) => [device.zone_id, { id: device.zone_id, name: areaName(device.zone_id, device.zone_name), division: deviceDivision(device) }] as const),
  ).values()].sort((a, b) => a.name.localeCompare(b.name, 'en', { numeric: true })), [devices])
  const byId = useMemo(() => new Map(devices.map((device) => [device.connector_id, device])), [devices])
  const [zone, setZone] = useState('')
  const [checking, setChecking] = useState(false)
  const [progress, setProgress] = useState({ scanned: 0, total: 0 })
  const [result, setResult] = useState<Check | null>(null)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [run, setRun] = useState<BacklogRun | null>(null)
  const [dialog, setDialog] = useState<'START' | 'STOP' | null>(null)
  const [reason, setReason] = useState('')
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const stopChecking = useRef(false)
  const approvalKey = useRef(idempotency('identity-backlog'))
  const confirmation = `RELEASE ${zone}`

  const loadRun = useCallback(async (zoneId: string) => {
    const response = await api<{ rows: BacklogRun[] }>(`${root}/releases${queryString({ zone_id: zoneId, limit: 1 })}`)
    setRun(response.rows[0] || null)
  }, [])
  useEffect(() => {
    setResult(null); setRun(null); setError(''); setChosen(new Set())
    if (zone) void loadRun(zone).catch((failure) => setError(message(failure)))
  }, [zone, loadRun])
  useEffect(() => {
    if (!run || !active.has(run.status)) return
    let stopped = false
    const timer = setInterval(() => {
      void api<BacklogRun>(`${root}/releases/${encodeURIComponent(run.job_id)}`)
        .then((value) => { if (!stopped) setRun(value) }).catch(() => undefined)
    }, 3000)
    return () => { stopped = true; clearInterval(timer) }
  }, [run?.job_id, run?.status])

  const page = async (params: Record<string, string | number>) => {
    // One zone check page runs at a time across ADD; wait for another to finish.
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await api<PreviewPage>(`${root}/preview${queryString({ zone_id: zone, limit: pageSize, ...params })}`)
      } catch (failure) {
        if (!(failure instanceof ApiError && failure.code === 'PREVIEW_BUSY') || attempt >= busyRetries) throw failure
        await pause(1500)
      }
    }
  }

  const check = async () => {
    if (!zone || checking) return
    setChecking(true); setError(''); setResult(null); setChosen(new Set()); stopChecking.current = false
    const totals = new Map<string, Group>()
    let after = 0; let scanned = 0; let total = 0
    try {
      const first = await page({ after_id: 0 })
      const frozen = { as_of: first.as_of, until_id: first.until_id, window_hours: first.window_hours }
      for (let current = first; ;) {
        if (typeof current.held_total === 'number') total = current.held_total
        current.groups.forEach((group) => {
          const old = totals.get(groupKey(group)) || { ...group, held: 0, provable: 0, next_snapshot: 0 }
          totals.set(groupKey(group), {
            ...old, held: old.held + group.held, provable: old.provable + group.provable,
            next_snapshot: old.next_snapshot + (group.next_snapshot || 0),
          })
        })
        scanned += current.scanned
        setProgress({ scanned, total })
        if (current.next_after_id === null || stopChecking.current) break
        after = current.next_after_id
        current = await page({ after_id: after, ...frozen })
      }
      if (!stopChecking.current && first.approval) {
        const names = Object.fromEntries(first.devices.map((device) => [device.connector_id, device.name || device.connector_id]))
        const groups = [...totals.values()].filter((group) => group.held > 0).sort((a, b) => (names[a.connector_id] || '').localeCompare(names[b.connector_id] || '')
          || a.source.localeCompare(b.source) || ageOrder.indexOf(a.age) - ageOrder.indexOf(b.age))
        setResult({
          zone, groups, windowHours: first.window_hours, approval: first.approval, releaseEnabled: first.release_enabled,
          automatic: first.automatic_zone_enabled, names, reheld: first.reheld_outside_check || 0,
        })
        setChosen(new Set(groups.filter((group) => group.provable > 0).map(groupKey)))
      }
    } catch (failure) { setError(message(failure)) } finally { setChecking(false) }
  }

  const held = result?.groups.reduce((sum, group) => sum + group.held, 0) ?? 0
  const provable = result?.groups.reduce((sum, group) => sum + group.provable, 0) ?? 0
  const nextSnapshot = result?.groups.reduce((sum, group) => sum + group.next_snapshot, 0) ?? 0
  const selected = result?.groups.filter((group) => chosen.has(groupKey(group))) ?? []
  const selectedHeld = selected.reduce((sum, group) => sum + group.held, 0)
  const selectedProvable = selected.reduce((sum, group) => sum + group.provable, 0)
  const releasing = run?.status === 'RELEASING'
  const running = Boolean(run && active.has(run.status))
  const canApprove = Boolean(result?.releaseEnabled && result.approval.signature && selectedProvable && !running)
  const closeDialog = () => { if (!busy) { setDialog(null); setPassword(''); setTyped(''); setError('') } }
  const toggle = (key: string) => setChosen((old) => {
    const next = new Set(old)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !dialog) return
    setBusy(true); setError('')
    try {
      const value = dialog === 'START' && result
        ? await api<BacklogRun>(`${root}/releases`, { method: 'POST', body: JSON.stringify({
          zone_id: zone, reason: reason.trim(), typed_confirmation: typed.trim(), password,
          idempotency_key: approvalKey.current, scope: result.approval.scope, expires_at: result.approval.expires_at,
          signature: result.approval.signature,
          groups: selected.map(({ connector_id, source, age }) => ({ connector_id, source, age })),
          approved_count: selectedProvable, previewed_held: selectedHeld,
        }) })
        : await api<BacklogRun>(`${root}/releases/${encodeURIComponent(run?.job_id || '')}/stop`, {
          method: 'POST', body: JSON.stringify({ password }),
        })
      setRun(value); setDialog(null); setPassword(''); setTyped(''); setReason('')
      approvalKey.current = idempotency('identity-backlog')
      toast.notice(dialog === 'START'
        ? 'Approval saved. The check-ins are being sent a few at a time.'
        : 'Stopped. No more check-ins are sent; the ones already sent are still followed to the HR system.')
    } catch (failure) { setError(message(failure)) } finally { setBusy(false) }
  }

  const zoneLabel = zones.find((item) => item.id === zone)?.name || ''
  // The friendly name, else the server's saved device name read as a place.
  const placeLabel = (connectorId: string) => {
    const known = byId.get(connectorId)
    if (known?.friendly_name) return known.friendly_name
    const saved = result?.names[connectorId]
    if (saved && saved !== connectorId) return placeOf({ friendly_name: null, display_name: saved, zone_id: saved })
    return known ? placeOf(known) : 'Place not known'
  }
  return <section className="panel safe-repair identity-backlog" aria-label="Send older not-matched check-ins (one area)">
    <header className="safe-repair-header"><div>
      <h2>Send older not-matched check-ins (one area)</h2>
      <p>Check-ins collected after a machine came back online, before its people list was refreshed. Recent ones are sent by themselves; older ones need your approval.</p>
    </div><span className="badge"><Icon name="shield" /> Needs your approval</span></header>
    <div className="safe-repair-body">
      <p>We only send a check-in when the person's CNIC at that time is still their CNIC today.</p>
      <Disclosure summary="How this works"><p>The check-in's Employee ID, machine record and fingerprint must match one saved CNIC history for that person at the check-in time, and that CNIC must still be the person's CNIC today. Check-ins that the next read of the people list can match are left for it. Every other check-in is not sent.</p></Disclosure>
      <div className="identity-backlog-controls">
        <label>Area<select value={zone} onChange={(event) => setZone(event.target.value)}>
          <option value="">Choose an area</option>
          {(['life', 'health'] as const).map((division) => {
            const items = zones.filter((item) => item.division === division)
            return items.length ? <optgroup key={division} label={division === 'life' ? 'Life' : 'Health'}>{items.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</optgroup> : null
          })}
        </select></label>
        <button className="button primary" type="button" disabled={!zone || checking} onClick={() => void check()}>
          {checking ? `Checking ${progress.scanned.toLocaleString()} of ${punches(progress.total)}` : 'Check area'}
        </button>
        {checking && <button className="button secondary" type="button" onClick={() => { stopChecking.current = true }}>Stop</button>}
      </div>
      {error && !dialog && <p className="message pattern-blocked" role="alert">{error}</p>}
      {result && result.zone === zone && <>
        <div className="safe-repair-counts">
          <Metric label="Not matched" value={held} icon="list" detail={`Checked ${rowTime(result.approval.scope.as_of)}`} />
          <Metric label="Can be sent" value={provable} icon="shield" detail="CNIC then is still the CNIC today" />
          {nextSnapshot > 0 && <Metric label="Next read of the people list" value={nextSnapshot} icon="list" detail="These are matched by it" />}
          <Metric label="Not sent" value={held - provable - nextSnapshot} icon="alert" detail="Need more checks" tone={held - provable - nextSnapshot ? 'warning' : 'neutral'} />
        </div>
        {result.reheld > 0 && <p>{punches(result.reheld)} sent earlier and set back are not part of this check. They are never sent from here.</p>}
        {!result.releaseEnabled && <p className="message pattern-blocked" role="alert">This tool is switched off for now, so nothing can be sent.</p>}
        {result.groups.length > 0 ? <ul className="identity-backlog-groups" aria-label="Choose the groups to send. Counts are fixed at the check.">
          {result.groups.map((group) => {
            const label = `${placeLabel(group.connector_id)}, ${sources[group.source] || 'Collected later'}, ${ageLabel(group.age, result.windowHours, result.automatic)}`
            return <li key={groupKey(group)} className="identity-backlog-group">
              <label className="identity-backlog-pick"><input type="checkbox" aria-label={`Send ${label}`} checked={chosen.has(groupKey(group))} disabled={!group.provable} onChange={() => toggle(groupKey(group))} />
                <span><strong>{placeLabel(group.connector_id)}</strong><small>{sources[group.source] || 'Collected later'} · {ageLabel(group.age, result.windowHours, result.automatic)}</small></span></label>
              <dl><div><dt>Not matched</dt><dd>{group.held.toLocaleString()}</dd></div><div><dt>Can be sent</dt><dd>{group.provable.toLocaleString()}</dd></div><div><dt>Next read</dt><dd>{group.next_snapshot.toLocaleString()}</dd></div></dl>
            </li>
          })}
        </ul> : <p>No not-matched check-ins of this kind in this area.</p>}
        <button className="button primary" type="button" disabled={!canApprove} onClick={() => { setDialog('START'); setError('') }}>
          Send {punches(selectedProvable)}
        </button>
        {running && <p>A sending task for this area is still running or following its check-ins to the HR system.</p>}
      </>}
      {run && <div className="safe-repair-run" role="status" aria-live="polite">
        <div className="safe-repair-heading"><h3>{run.paused ? 'Paused: this tool was switched off' : runLabel[run.status] || 'In progress'}</h3><span>Approved by {run.actor} · {longTime(run.created_at)}</span></div>
        <p>{run.delivered.toLocaleString()} sent · {run.waiting_oracle.toLocaleString()} waiting for the HR system · {run.released.toLocaleString()} of {run.approved.toLocaleString()} approved started · {run.kept_held.toLocaleString()} not sent{run.left_to_snapshot ? ` · ${run.left_to_snapshot.toLocaleString()} left to the next read of the people list` : ''}{run.no_longer_held ? ` · ${run.no_longer_held.toLocaleString()} already matched when reached` : ''}{run.needs_review ? ` · ${run.needs_review.toLocaleString()} need checking` : ''}</p>
        {run.waiting_reason && <p>{plainReason('areaWait', run.waiting_reason).sentence}</p>}
        <p>Reason: <span data-user-text>{run.reason}</span></p>
        {releasing && <div className="safe-repair-actions"><button className="button secondary" type="button" onClick={() => { setDialog('STOP'); setError('') }}>Stop</button><span>You can close this page. Sending goes on.</span></div>}
      </div>}
    </div>
    {dialog && <Dialog
      titleId="identity-backlog-title"
      title={dialog === 'START' ? `Send up to ${punches(selectedProvable)}` : 'Stop this task'}
      description={dialog === 'START'
        ? `Only check-ins of the ${selected.length === 1 ? 'chosen group' : `${selected.length} chosen groups`} that are still not matched when you approve, and no more than ${punches(selectedProvable)}. Each one is checked again, the same way, as it is sent. One that no longer matches is not sent.`
        : 'No more check-ins are sent. The ones already sent stay sent and are followed to the HR system. The rest are not sent.'}
      onClose={closeDialog}
    ><form className="dialog-body" onSubmit={submit}>
      {dialog === 'START' && <>
        <label>{STEP_UP.why}<textarea required minLength={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        <small className="step-up-help">{STEP_UP.whyHelp(3)}</small>
        <label>Type <span data-device-code>{confirmation}</span> to confirm{zoneLabel ? ` (the code of ${zoneLabel})` : ''}<input required value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" /></label>
      </>}
      <label>{STEP_UP.password}<input type="password" required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
      <small className="step-up-help">{STEP_UP.passwordHelp}</small>
      {error && <p className="message pattern-blocked" role="alert">{error}</p>}
      <footer className="dialog-actions">
        <button className="button secondary" type="button" disabled={busy} onClick={closeDialog}>Cancel</button>
        <button className="button primary" disabled={busy || !password || (dialog === 'START' && (reason.trim().length < 3 || typed.trim() !== confirmation))}>
          {busy ? 'Saving…' : dialog === 'START' ? `Send ${punches(selectedProvable)}` : 'Stop'}
        </button>
      </footer>
    </form></Dialog>}
  </section>
}
