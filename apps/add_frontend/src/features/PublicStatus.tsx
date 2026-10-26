import { useEffect, useState } from 'react'
import {
  devicesInZone,
  formatClock,
  formatDuration,
  formatSince,
  PUBLIC_STATUS_PATH,
  PUBLIC_STATUS_STREAM_PATH,
  type PublicDevice,
  type PublicStatusSnapshot,
  type PublicZone,
} from '../publicStatus'
import './PublicStatus.css'

type Connection = 'connecting' | 'live' | 'reconnecting'

// A snapshot older than this means updates have stopped arriving.
const STALE_MS = 2 * 60_000
const POLL_MS = 30_000

function usePublicStatus() {
  const [snapshot, setSnapshot] = useState<PublicStatusSnapshot | null>(null)
  const [failed, setFailed] = useState(false)
  const [connection, setConnection] = useState<Connection>('connecting')

  useEffect(() => {
    let cancelled = false
    let live = false
    const accept = (body: PublicStatusSnapshot) => {
      if (cancelled) return
      setSnapshot(body)
      setFailed(false)
    }
    const load = async () => {
      try {
        const response = await fetch(PUBLIC_STATUS_PATH, { cache: 'no-store' })
        if (!response.ok) throw new Error(`Status request failed (${response.status})`)
        accept(await response.json() as PublicStatusSnapshot)
      } catch {
        if (!cancelled) setFailed(true)
      }
    }
    void load()
    const source = typeof EventSource === 'undefined' ? null : new EventSource(PUBLIC_STATUS_STREAM_PATH)
    source?.addEventListener('status', (event) => {
      try {
        accept(JSON.parse((event as MessageEvent<string>).data) as PublicStatusSnapshot)
        live = true
        setConnection('live')
      } catch {
        // A malformed frame is skipped; the next one replaces it.
      }
    })
    if (source) source.onerror = () => {
      live = false
      setConnection('reconnecting')
    }
    // Poll while the live stream is down, for example behind a proxy that blocks it.
    const poll = window.setInterval(() => { if (!live) void load() }, POLL_MS)
    return () => {
      cancelled = true
      source?.close()
      window.clearInterval(poll)
    }
  }, [])

  return { snapshot, failed, connection }
}

function useNow(intervalMs: number) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}

function LiveBadge({ connection, snapshot, now }: { connection: Connection; snapshot: PublicStatusSnapshot | null; now: Date }) {
  const stale = snapshot ? +now - +new Date(snapshot.updated_at) > STALE_MS : false
  const state = connection === 'live' && !stale ? 'live' : stale ? 'delayed' : 'waiting'
  const label = { live: 'Live', delayed: 'Updates delayed', waiting: connection === 'connecting' ? 'Connecting' : 'Reconnecting' }[state]
  return (
    <div className={`ps-live is-${state}`} role="status" aria-live="polite">
      <span className="ps-live-dot" aria-hidden="true" />
      <span className="ps-live-label">{label}</span>
      {snapshot && <span className="ps-live-time">Updated {formatClock(snapshot.updated_at)}</span>}
    </div>
  )
}

function Summary({ snapshot }: { snapshot: PublicStatusSnapshot }) {
  return (
    <section className="ps-summary" aria-label="All devices">
      <div className="ps-tile">
        <span className="ps-tile-label">Devices</span>
        <span className="ps-tile-value">{snapshot.total}</span>
      </div>
      <div className="ps-tile is-online">
        <span className="ps-tile-label">Online</span>
        <span className="ps-tile-value">{snapshot.online}</span>
      </div>
      <div className={`ps-tile ${snapshot.offline ? 'is-offline' : ''}`}>
        <span className="ps-tile-label">Offline</span>
        <span className="ps-tile-value">{snapshot.offline}</span>
      </div>
    </section>
  )
}

function DeviceRow({ device, now }: { device: PublicDevice; now: Date }) {
  const online = device.status === 'online'
  return (
    <li className={`ps-device is-${device.status}`} title={device.code}>
      <span className="ps-dot" aria-hidden="true" />
      <span className="ps-device-text">
        <span className="ps-device-name">{device.name}</span>
        <span className="ps-device-since">
          Since {formatSince(device.since, now)}
          <span className="ps-device-duration"> · {formatDuration(device.since, now)}</span>
        </span>
      </span>
      <span className="ps-pill">{online ? 'Online' : 'Offline'}</span>
    </li>
  )
}

function ZoneCard({ zone, devices, now }: { zone: PublicZone; devices: PublicDevice[]; now: Date }) {
  const percent = zone.total ? Math.round((zone.online / zone.total) * 100) : 0
  const headingId = `ps-zone-${zone.zone}`
  return (
    <section className="ps-zone" aria-labelledby={headingId}>
      <header className="ps-zone-head">
        <div>
          <h2 id={headingId}>{zone.name}</h2>
          <p className="ps-zone-count">{zone.online} of {zone.total} online</p>
        </div>
        {zone.total > 0 && (
          <span className={`ps-zone-flag ${zone.offline ? 'is-offline' : 'is-online'}`}>
            {zone.offline ? `${zone.offline} offline` : 'All online'}
          </span>
        )}
      </header>
      <div className="ps-meter" role="presentation">
        <span className="ps-meter-fill" style={{ width: `${percent}%` }} />
      </div>
      {devices.length
        ? <ul className="ps-list">{devices.map((device, index) => <DeviceRow key={`${device.code}:${index}`} device={device} now={now} />)}</ul>
        : <p className="ps-empty">No devices in this zone.</p>}
    </section>
  )
}

export default function PublicStatus() {
  const { snapshot, failed, connection } = usePublicStatus()
  const now = useNow(15_000)

  useEffect(() => {
    document.title = 'Attendance Device Status · State Life'
  }, [])

  return (
    <div className="public-status">
      <header className="ps-header">
        <div className="ps-header-inner">
          <div className="ps-brand">
            <img src="/state-life-logo.png" alt="" width="44" height="44" />
            <div>
              <p className="ps-org">State Life Insurance Corporation of Pakistan</p>
              <h1>Attendance Device Status</h1>
            </div>
          </div>
          <LiveBadge connection={connection} snapshot={snapshot} now={now} />
        </div>
      </header>
      <main className="ps-main">
        {snapshot ? (
          <>
            <Summary snapshot={snapshot} />
            <div className="ps-zones">
              {snapshot.zones.map((zone) => (
                <ZoneCard key={zone.zone} zone={zone} devices={devicesInZone(snapshot, zone.zone)} now={now} />
              ))}
            </div>
          </>
        ) : (
          <p className="ps-message" role={failed ? 'alert' : undefined}>
            {failed ? 'Device status is unavailable right now. This page will keep trying.' : 'Loading device status…'}
          </p>
        )}
      </main>
      <footer className="ps-footer">
        A device is online when its attendance is reaching the system. Times are Pakistan time.
      </footer>
    </div>
  )
}
