import { lazy, useMemo } from 'react'
import { useAppData } from '../AppData'

const AlertsView = lazy(() => import('../features/Monitoring').then((module) => ({ default: module.AlertsView })))

// Problems (`/problems`). Spares never raise problems.
export default function ProblemsRoute() {
  const app = useAppData()
  const active = useMemo(() => app.devices.filter((device) => !device.is_spare), [app.devices])
  return <AlertsView devices={active} toast={app.toast} revision={app.revisions.alert} />
}
