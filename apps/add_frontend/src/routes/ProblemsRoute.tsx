import { lazy, useMemo } from 'react'
import { useAppData } from '../AppData'

const ProblemsView = lazy(() => import('../features/Problems').then((module) => ({ default: module.ProblemsView })))

// Problems (`/problems`). Spares never raise problems. Heartbeats (device
// revisions) refresh the list at most every 10 s, merged in place.
export default function ProblemsRoute() {
  const app = useAppData()
  const active = useMemo(() => app.devices.filter((device) => !device.is_spare), [app.devices])
  return <ProblemsView devices={active} revision={app.revisions.alert} deviceRevision={app.revisions.device} username={app.username} />
}
