import { lazy } from 'react'
import { useAppData } from '../AppData'

const ReconciliationView = lazy(() => import('../features/Reconciliation').then((module) => ({ default: module.ReconciliationView })))

// Recover attendance (`/recover?tab=recoveries | unreadable | held`). The page
// throttles recovery events (5 s) and check-in events (15 s) apart (ADV-24).
export default function RecoverRoute() {
  const app = useAppData()
  return <ReconciliationView devices={app.devices} revision={app.revisions.reconciliation} attendanceRevision={app.revisions.attendance} toast={app.toast} />
}
