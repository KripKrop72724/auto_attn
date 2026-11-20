import React, { Component, lazy, Suspense, type ReactNode } from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { queryClient } from './data'
import { isPublicStatusPath } from './publicStatus'
import './styles.css'

// The public status page needs no sign-in, so it never mounts the dashboard.
const PublicStatus = lazy(() => import('./features/PublicStatus'))

// A failed download of the status page code says so instead of a blank page.
class StatusBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    if (!this.state.failed) return this.props.children
    return (
      <main className="boot-screen">
        <p>Device status could not load. Reload the page.</p>
        <button type="button" className="button primary" onClick={() => window.location.reload()}>Reload</button>
      </main>
    )
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {isPublicStatusPath(window.location.pathname)
      ? <StatusBoundary><Suspense fallback={null}><PublicStatus /></Suspense></StatusBoundary>
      : <QueryClientProvider client={queryClient}><App /></QueryClientProvider>}
  </React.StrictMode>,
)
