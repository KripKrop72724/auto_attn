import { lazy } from 'react'

const SettingsView = lazy(() => import('../features/Settings').then((module) => ({ default: module.SettingsView })))

// Settings (`/settings`, 01-foundation K, FND-13).
export default function SettingsRoute() {
  return <SettingsView />
}
