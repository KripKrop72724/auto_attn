import { lazy } from 'react'
import { useAppData } from '../AppData'

const AttendanceView = lazy(() => import('../features/Attendance').then((module) => ({ default: module.AttendanceView })))

// Attendance (`/attendance`).
export default function AttendanceRoute() {
  const app = useAppData()
  return <AttendanceView devices={app.devices} revision={app.revisions.attendance} realtimeState={app.realtime.state} realtimeLastSyncAt={app.realtime.lastSyncAt} toast={app.toast} />
}
