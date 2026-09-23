import { useEffect, useState } from 'react'
import { api, queryString } from '../api'
import { dateTime } from '../App'
import type { Device } from '../types'
import './SafeAttendanceRepair.css'

type Counts = { checked: number; ready: number; waiting: number; confirmed: number; review: number; stopped: number }
export type RepairRun = {
  automatic?: boolean; job_id: string; status: string; actor: string; counts: Counts; check_complete: boolean
  created_at: string; updated_at: string; expires_at: string | null; signature: string | null
  last_error: string | null; execution_enabled: boolean; eligible_at_check: number
  devices: Array<{ connector_id: string; name: string; serial: string | null; checked: number; status: string }>
}

/** Previous policy remains readable, without admission or approval controls. */
export function SafeAttendanceRepair(_props: { devices: Device[]; toast: { notice: (text: string) => void; error: (text: string) => void } }) {
  const [rows, setRows] = useState<RepairRun[]>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [run, setRun] = useState<RepairRun | null>(null)
  const [items, setItems] = useState<Array<{ id: number; name: string; reason: string; time: string }>>([])
  const [itemCursor, setItemCursor] = useState<number | null>(null)
  const load = async (before?: number) => {
    const page = await api<{ rows: RepairRun[]; next_cursor: number | null }>('/api/v2/attendance-recovery/checks' + queryString({ before }))
    setRows(old => before ? [...old, ...page.rows] : page.rows); setCursor(page.next_cursor)
  }
  useEffect(() => { void load().catch(err => setError(String(err))) }, [])
  const show = async (value: RepairRun, after?: number) => {
    setRun(value)
    try {
      const page = await api<{ rows: typeof items; next_cursor: number | null }>(`/api/v2/attendance-recovery/jobs/${value.job_id}/items` + queryString({ cursor: after, limit: 25 }))
      setItems(old => after ? [...old, ...page.rows] : page.rows); setItemCursor(page.next_cursor)
    } catch (err) { setError(String(err)) }
  }
  return <section className="panel safe-repair" aria-label="Previous repair history"><header className="safe-repair-header"><div><h2>Previous repair history</h2><p>This policy is retired. New releases use Force release attendance.</p></div></header><div className="safe-repair-body">{error && <p role="alert">{error}</p>}<ul className="safe-repair-history">{rows.map(row => <li key={row.job_id}><button className="button secondary" onClick={() => void show(row)}>{dateTime(row.created_at)} · {row.status.replaceAll('_', ' ')} · {row.counts.confirmed} confirmed</button></li>)}</ul>{cursor && <button className="button secondary" onClick={() => void load(cursor).catch(err => setError(String(err)))}>Older history</button>}{run && <div><h3>{dateTime(run.created_at)} · {run.actor}</h3><p>{run.counts.confirmed} confirmed · {run.counts.waiting} waiting · {run.counts.review} need review</p><ul className="safe-repair-items">{items.map(item => <li key={item.id}><span>{item.name} · {dateTime(item.time)}</span><span>{item.reason}</span></li>)}</ul>{itemCursor && <button className="button secondary" onClick={() => void show(run, itemCursor)}>More records</button>}</div>}</div></section>
}
