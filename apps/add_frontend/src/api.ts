export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public fieldErrors: Record<string, string[]> = {},
    public requestId: string | null = null,
    // Structured refusals arrive as {"detail": {"code", "message", ...extra}}.
    public code: string | null = null,
    public detail: Record<string, unknown> | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  get retryable() {
    return this.status === 408 || this.status === 429 || this.status >= 500
  }
}

let csrfToken = ''

export function setCsrfToken(value: string) {
  csrfToken = value
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (init.method && init.method !== 'GET' && csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(path, { ...init, headers, credentials: 'include' })
  if (!response.ok) {
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('add:session-expired'))
    }
    let message = `Request failed (${response.status})`
    let fieldErrors: Record<string, string[]> = {}
    let code: string | null = null
    let detail: Record<string, unknown> | null = null
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail && typeof body.detail === 'object' && !Array.isArray(body.detail)) {
        detail = body.detail
        if (typeof body.detail.code === 'string') code = body.detail.code
        if (typeof body.detail.message === 'string') message = body.detail.message
      } else if (Array.isArray(body.detail)) {
        const grouped: Record<string, string[]> = {}
        body.detail.forEach((item: { loc?: unknown[]; msg?: string }) => {
          const key = Array.isArray(item.loc) ? String(item.loc.at(-1) || 'form') : 'form'
          grouped[key] = [...(grouped[key] || []), item.msg || 'Invalid value']
        })
        fieldErrors = grouped
        message = Object.values(grouped).flat()[0] || message
      }
    } catch {
      // Preserve the status-based message when an upstream proxy returns HTML.
    }
    throw new ApiError(
      response.status,
      message,
      fieldErrors,
      response.headers.get('x-request-id') || response.headers.get('x-correlation-id'),
      code,
      detail,
    )
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export function queryString(values: Record<string, string | number | boolean | null | undefined>) {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  const value = params.toString()
  return value ? `?${value}` : ''
}
