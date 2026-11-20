// Fix tools › History (ATT-11): past sends and checks, newest first, with a
// Type filter. "Sent by hand" and "Not-matched check-ins sent" are merged
// from their own lists; the two retired kinds are shown read-only, as today.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api'
import { DivisionTag, StatusChip } from '../../ui'
import { deviceDivision } from '../../lib/division'
import type { Device } from '../../types'
import type { DirectRun } from '../DirectOrdsSend'
import { FORCE_RUN_WORDS, type ForceRun } from '../ManualForceRelease'
import { AttendanceReleaseHistory } from '../AttendanceRelease'
import { SafeAttendanceRepair } from '../SafeAttendanceRepair'
import { checkins, longTime } from './attendanceWords'

type Toast = { notice: (text: string) => void; error: (text: string) => void }
export type HistoryKind = 'all' | 'hand' | 'not-matched' | 'old-release' | 'old-repair'

const KINDS: Array<{ id: HistoryKind; label: string }> = [
  { id: 'all', label: 'All types' },
  { id: 'hand', label: 'Sent by hand' },
  { id: 'not-matched', label: 'Not-matched check-ins sent' },
  { id: 'old-release', label: 'Old release tasks (no longer used)' },
  { id: 'old-repair', label: 'Old repair tasks (no longer used)' },
]

const DIRECT_WORDS: Record<string, string> = {
  RUNNING: 'Getting ready', WAITING_ORACLE: 'Waiting for the HR system', COMPLETED: 'Done', COMPLETED_WITH_REVIEW: 'Done · some need checking',
}
const toneOf = (status: string) => (status === 'COMPLETED' ? 'working' : status.includes('REVIEW') || status === 'STOPPED' || status === 'EXPIRED' ? 'attention' : 'neutral') as 'working' | 'attention' | 'neutral'

type Item = {
  key: string; kind: 'hand' | 'not-matched'; at: string; who: string; sent: number; total: number
  status: string; statusLabel: string; divisions: Array<'life' | 'health'>; places: string; open: string
}

export function AttendanceHistory({ devices, toast, kind, onKind, releaseJobId, revision }: {
  devices: Device[]; toast: Toast; kind: HistoryKind; onKind: (kind: HistoryKind) => void; releaseJobId: string | null; revision: number
}) {
  const navigate = useNavigate()
  const [direct, setDirect] = useState<DirectRun[] | null>(null)
  const [force, setForce] = useState<ForceRun[] | null>(null)
  const [failed, setFailed] = useState(false)
  const load = useCallback(async () => {
    setFailed(false)
    const [hand, held] = await Promise.allSettled([
      api<{ rows: DirectRun[] }>('/api/v2/attendance-direct-ords'),
      api<{ rows: ForceRun[] }>('/api/v2/attendance-force-releases'),
    ])
    setDirect(hand.status === 'fulfilled' && Array.isArray(hand.value?.rows) ? hand.value.rows.filter((row) => row?.job_id) : [])
    setForce(held.status === 'fulfilled' && Array.isArray(held.value?.rows) ? held.value.rows.filter((row) => row?.job_id) : [])
    if (hand.status === 'rejected' && held.status === 'rejected') setFailed(true)
  }, [])
  useEffect(() => { if (kind === 'all' || kind === 'hand' || kind === 'not-matched') void load() }, [kind, load])

  const byId = useMemo(() => new Map(devices.map((device) => [device.connector_id, device])), [devices])
  const items = useMemo<Item[]>(() => [
    ...(direct || []).map((run) => ({
      key: `hand-${run.job_id}`, kind: 'hand' as const, at: run.created_at, who: run.actor, sent: run.confirmed, total: run.selected,
      status: run.status, statusLabel: DIRECT_WORDS[run.status] || 'In progress', divisions: [], places: '',
      open: `/attendance?direct_run=${encodeURIComponent(run.job_id)}`,
    })),
    ...(force || []).map((run) => {
      const divisions = [...new Set(run.devices.map((device) => (byId.get(device.connector_id) ? deviceDivision(byId.get(device.connector_id)!) : null)).filter(Boolean))] as Array<'life' | 'health'>
      return {
        key: `force-${run.job_id}`, kind: 'not-matched' as const, at: run.created_at, who: run.actor, sent: run.counts.confirmed,
        total: run.counts.ready + run.counts.confirmed + run.counts.waiting, status: run.status, statusLabel: FORCE_RUN_WORDS[run.status] || 'In progress',
        divisions, places: `${run.devices.length} ${run.devices.length === 1 ? 'machine' : 'machines'}`,
        open: `/attendance?view=fix-tools&tool=held-staff&force_run=${encodeURIComponent(run.job_id)}`,
      }
    }),
  ].filter((item) => kind === 'all' || item.kind === kind).sort((a, b) => b.at.localeCompare(a.at)), [byId, direct, force, kind])

  return <section className="panel attendance-history" aria-labelledby="attendance-history-title">
    <header className="attendance-history-head">
      <div><h2 id="attendance-history-title">History</h2><p>Past sends and checks, newest first.</p></div>
      <label>Type<select value={kind} onChange={(event) => onKind(event.target.value as HistoryKind)}>{KINDS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
    </header>
    {kind === 'old-release' && <><p className="attendance-history-note">This old tool is no longer used. Use Re-check not-matched check-ins.</p><AttendanceReleaseHistory devices={devices} revision={revision} toast={toast} initialJobId={releaseJobId} /></>}
    {kind === 'old-repair' && <><p className="attendance-history-note">This old tool is no longer used. Use Re-check not-matched check-ins.</p><SafeAttendanceRepair devices={devices} toast={toast} /></>}
    {(kind === 'all' || kind === 'hand' || kind === 'not-matched') && <>
      {failed && <p className="message pattern-blocked" role="alert">Couldn't load this. <button className="text-button" type="button" onClick={() => void load()}>Try again</button></p>}
      {(direct === null || force === null) && !failed && <p role="status" className="attendance-history-note">Loading…</p>}
      {direct !== null && force !== null && !items.length && <p className="attendance-history-note">Nothing sent yet.</p>}
      <ul className="attendance-history-list">{items.map((item) => <li key={item.key}>
        <a href={item.open} className="attendance-history-row" onClick={(event) => { event.preventDefault(); navigate(item.open) }}>
          <span className="attendance-history-main"><strong>{item.kind === 'hand' ? 'Sent by hand' : 'Not-matched check-ins sent'}</strong><small>{longTime(item.at)} · <span data-user-text>{item.who}</span>{item.places ? ` · ${item.places}` : ''}</small></span>
          <span className="attendance-history-tags">{item.divisions.length === 2 ? <span className="attendance-history-both">Life and Health</span> : item.divisions.map((division) => <DivisionTag key={division} division={division} />)}</span>
          <span className="attendance-history-count">{item.sent.toLocaleString('en-PK')} sent of {checkins(item.total)}</span>
          <StatusChip tone={toneOf(item.status)} label={item.statusLabel} code={item.status} />
        </a>
      </li>)}</ul>
    </>}
  </section>
}
