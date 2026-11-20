import { lazy } from 'react'

const DevicesView = lazy(() => import('../features/Devices').then((module) => ({ default: module.DevicesView })))

// Devices (`/devices`; `/devices/:id` opens the device panel over it;
// 03-home-devices §2-3). The page reads the app data itself.
export default function DevicesRoute() {
  return <DevicesView />
}
