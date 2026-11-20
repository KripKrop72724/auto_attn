import { afterEach, describe, expect, it, vi } from 'vitest'
import { checkForNewVersion, isChunkLoadError, reloadOnceForChunkError, runningStartFile, servedStartFile } from './newVersion'

const page = (file: string) => `<!doctype html><html><head><script type="module" crossorigin src="${file}"></script></head></html>`

afterEach(() => {
  window.history.replaceState(null, '', '/home')
  document.head.querySelectorAll('script[data-test]').forEach((node) => node.remove())
})

describe('new version check', () => {
  it('finds the start file of the running and the served page', () => {
    const script = document.createElement('script')
    script.type = 'module'
    script.src = '/assets/index-AbC123_x.js'
    script.dataset.test = '1'
    document.head.append(script)
    expect(runningStartFile()).toBe('/assets/index-AbC123_x.js')
    expect(servedStartFile(page('/assets/index-Zz9.js'))).toBe('/assets/index-Zz9.js')
    expect(servedStartFile('<html></html>')).toBe('')
  })

  it('says nothing for the same file, new for another one, and fails quietly', async () => {
    const load = (body: string, status = 200) => vi.fn(async () => new Response(body, { status }))
    expect(await checkForNewVersion('/assets/index-A.js', load(page('/assets/index-A.js')))).toBe('same')
    expect(await checkForNewVersion('/assets/index-A.js', load(page('/assets/index-B.js')))).toBe('new')
    expect(await checkForNewVersion('/assets/index-A.js', load('', 502))).toBe('unknown')
    expect(await checkForNewVersion('/assets/index-A.js', vi.fn(async () => { throw new TypeError('offline') }))).toBe('unknown')
    expect(await checkForNewVersion('', load(page('/assets/index-B.js')))).toBe('unknown')
    const calls = load(page('/assets/index-A.js'))
    await checkForNewVersion('/assets/index-A.js', calls)
    expect(calls).toHaveBeenCalledWith('/', expect.objectContaining({ cache: 'no-store' }))
  })
})

describe('missing page file after an update', () => {
  it('recognises the browsers\' chunk errors', () => {
    expect(isChunkLoadError(new TypeError('Failed to fetch dynamically imported module: https://add/assets/Attendance-1.js'))).toBe(true)
    expect(isChunkLoadError(new Error('Importing a module script failed.'))).toBe(true)
    expect(isChunkLoadError(new Error('Loading chunk 7 failed.'))).toBe(true)
    expect(isChunkLoadError(new Error('Request failed (502)'))).toBe(false)
  })

  it('reloads once, keeping the router state, and never again on the same entry', () => {
    window.history.replaceState({ usr: null, key: 'abc', idx: 2 }, '', '/attendance')
    const reload = vi.fn()
    const error = new TypeError('Failed to fetch dynamically imported module')
    expect(reloadOnceForChunkError(error, reload)).toBe(true)
    expect(window.history.state).toEqual({ usr: null, key: 'abc', idx: 2, addReloadedOnce: true })
    expect(reloadOnceForChunkError(error, reload)).toBe(false)
    expect(reload).toHaveBeenCalledOnce()
    expect(reloadOnceForChunkError(new Error('Other'), reload)).toBe(false)
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })
})
