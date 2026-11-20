import { useState } from 'react'
import { useAppData } from '../AppData'
import { clock } from '../lib/time'

// A background refresh failed: say so once, in place, instead of a toast on
// every try. The app frame's Live banner replaces this later.
export function FleetRefreshBar() {
  const { devicesError, devicesUpdatedAt, refreshFleet } = useAppData()
  const [trying, setTrying] = useState(false)
  if (!devicesError) return null
  const knownAt = devicesUpdatedAt ? clock(devicesUpdatedAt) : ''
  return (
    <div className="fleet-refresh-bar" role="status">
      <span>{knownAt ? `Could not refresh. Showing what we knew at ${knownAt}. Trying again…` : 'Could not load the devices. Trying again…'}</span>
      <button type="button" className="button secondary small" disabled={trying} aria-busy={trying || undefined} onClick={() => { setTrying(true); void refreshFleet().finally(() => setTrying(false)) }}>
        {trying ? 'Trying…' : 'Try now'}
      </button>
    </div>
  )
}
