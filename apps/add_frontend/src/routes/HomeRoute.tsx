import { lazy } from 'react'
import { useAppData } from '../AppData'

const FleetView = lazy(() => import('../features/Fleet').then((module) => ({ default: module.FleetView })))
import { FleetRefreshBar } from './FleetRefreshBar'

// Home (`/home`, the default page). Placeholder: shows today's fleet page until
// the Home page (03-home-devices §1) replaces it in this module.
export default function HomeRoute() {
  const app = useAppData()
  return (
    <>
      <FleetRefreshBar />
      <FleetView
        devices={app.devices}
        overview={app.overview}
        loading={!app.devicesLoaded}
        onInspect={app.inspectDevice}
        onManageUsers={app.manageUsers}
        onNavigateAlerts={() => app.navigate('/problems')}
        onNavigateReconciliation={() => app.navigate('/recover?tab=held')}
        onNavigateUnknownRelease={() => app.navigate('/attendance')}
      />
    </>
  )
}
