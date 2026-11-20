import { useEffect, useState } from 'react'
import { api, queryString } from '../api'
import { dateTime } from '../App'

type Coverage = { total: number; confirmed: number; identity_held: number; pending: number; review: number; observed_at: string }
export function SavedAttendanceCoverage({ connectorId }: { connectorId?: string }) {
  const [data, setData] = useState<Coverage | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    setData(null)
    const load = async () => {
      try {
        const fresh = await api<Coverage>('/api/v2/attendance-recovery/coverage' + queryString({ connector_id: connectorId }))
        if (!stopped) { setData(fresh); setError(false) }
      } catch { if (!stopped) setError(true) }
      if (!stopped) timer = setTimeout(load, 15000)
    }
    void load()
    return () => { stopped = true; clearTimeout(timer) }
  }, [connectorId])
  // Older ADD servers and missing fields must not produce reassuring zeroes.
  const available = data && typeof data.total === 'number'
  return <aside className="message pattern-waiting" aria-label="All saved attendance">
    <div><strong>All saved attendance{connectorId ? ' for this device' : ' across Pakistan'}</strong>
      {available ? <p>{data.confirmed.toLocaleString()} confirmed · {data.pending.toLocaleString()} waiting for Oracle · {data.identity_held.toLocaleString()} waiting for identity evidence · {data.review.toLocaleString()} need review. <small>Updated {dateTime(data.observed_at)}{error ? ' · Connection interrupted; these figures may be out of date.' : ''}</small></p> : <p>{error ? 'Current delivery counts are unavailable. Try again when the connection returns.' : 'Loading current delivery counts…'}</p>}
      <p>These figures cover all saved punches. A reconciliation result covers only the records included in that check.</p>
    </div>
  </aside>
}
