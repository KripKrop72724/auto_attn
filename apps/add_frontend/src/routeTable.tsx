// One row per page: its module, the words while it opens and its tab title.
// Each page change edits only its own module in src/routes.
import type { ComponentType } from 'react'
import type { DashboardRoute } from './types'
import AttendanceRoute from './routes/AttendanceRoute'
import DevicesRoute from './routes/DevicesRoute'
import HomeRoute from './routes/HomeRoute'
import PeopleRoute from './routes/PeopleRoute'
import ProblemsRoute from './routes/ProblemsRoute'
import RecoverRoute from './routes/RecoverRoute'
import SettingsRoute from './routes/SettingsRoute'
import SetupRoute from './routes/SetupRoute'
import UpdatesRoute from './routes/UpdatesRoute'

export interface RouteEntry {
  route: DashboardRoute
  /** Page name: the menu label and the browser tab title. */
  title: string
  /** Shown while the page's code downloads. */
  loadingLabel: string
  Page: ComponentType
}

export const routeTable: Record<DashboardRoute, RouteEntry> = {
  home: { route: 'home', title: 'Home', loadingLabel: 'Opening Home', Page: HomeRoute },
  devices: { route: 'devices', title: 'Devices', loadingLabel: 'Opening devices', Page: DevicesRoute },
  attendance: { route: 'attendance', title: 'Attendance', loadingLabel: 'Opening attendance', Page: AttendanceRoute },
  problems: { route: 'problems', title: 'Problems', loadingLabel: 'Opening problems', Page: ProblemsRoute },
  people: { route: 'people', title: 'People', loadingLabel: 'Opening people', Page: PeopleRoute },
  recover: { route: 'recover', title: 'Recover attendance', loadingLabel: 'Opening Recover attendance', Page: RecoverRoute },
  updates: { route: 'updates', title: 'Software updates', loadingLabel: 'Opening software updates', Page: UpdatesRoute },
  setup: { route: 'setup', title: 'Set up a device', loadingLabel: 'Opening Set up a device', Page: SetupRoute },
  settings: { route: 'settings', title: 'Settings', loadingLabel: 'Opening settings', Page: SettingsRoute },
}

export const documentTitle = (route: DashboardRoute) => `${routeTable[route].title} · State Life Attendance`
