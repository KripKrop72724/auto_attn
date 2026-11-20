import { useAppData } from '../AppData'
import { FleetView } from '../features/Fleet'
import { FleetRefreshBar } from './FleetRefreshBar'

// Devices (`/devices`; `/devices/:id` opens the device panel over it). Shows
// today's fleet list until the Devices page (03-home-devices §2-3) replaces it.
export default function DevicesRoute() {
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
