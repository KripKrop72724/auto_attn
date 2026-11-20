import { Component, type ErrorInfo, type ReactNode } from 'react'
import { useRouteError } from 'react-router-dom'
import { isChunkLoadError, reloadOnceForChunkError } from '../lib/newVersion'

// The card shown when a page could not open. A page file missing after an
// update reloads the page once by itself before this card shows.
export function PageErrorCard({ error, fullPage = false }: { error: unknown; fullPage?: boolean }) {
  const message = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  const card = (
    <section className="page-error-card" role="alert">
      <h1>This page couldn't open</h1>
      <p>{isChunkLoadError(error) ? 'The app may have been updated. Reload to continue.' : 'Something went wrong while opening this page. Reload to continue.'}</p>
      <div className="page-error-actions">
        <button type="button" className="button primary" onClick={() => window.location.reload()}>Reload</button>
        <a className="button secondary" href="/home">Go to Home</a>
      </div>
      {message && <details className="technical-details" data-technical="true"><summary>Technical details</summary><p>{message}</p></details>}
    </section>
  )
  return fullPage
    ? <main className="boot-screen route-error"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" />{card}</main>
    : card
}

/** React Router's errorElement: the whole app could not render. */
export function RouteErrorBoundary() {
  const error = useRouteError()
  const reloading = isChunkLoadError(error) && reloadOnceForChunkError(error)
  if (reloading) return null
  return <PageErrorCard error={error} fullPage />
}

type BoundaryState = { error: unknown; reloading: boolean }

/** Keeps the frame (menu, header) working when one page fails. Reset it with `key` per page. */
export class PageErrorBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null, reloading: false }
  static getDerivedStateFromError(error: unknown): Partial<BoundaryState> {
    return { error }
  }
  componentDidCatch(error: unknown, _info: ErrorInfo) {
    if (reloadOnceForChunkError(error)) this.setState({ reloading: true })
  }
  render() {
    if (this.state.reloading) return null
    if (this.state.error) return <PageErrorCard error={this.state.error} />
    return this.props.children
  }
}
