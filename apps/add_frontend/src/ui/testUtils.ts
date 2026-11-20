import { vi } from 'vitest'

// Shared jsdom set-up for kit tests: the app's #root and #overlay-root, and a
// matchMedia stub whose phone answer the test chooses.
export function setUpKitDom({ phone = false }: { phone?: boolean } = {}) {
  document.body.innerHTML = '<div id="root"><div id="test-app"></div></div><div id="overlay-root"></div>'
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: query.includes('max-width: 760px') ? phone : false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })))
  return document.getElementById('test-app') as HTMLElement
}

export function tearDownKitDom() {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
}
