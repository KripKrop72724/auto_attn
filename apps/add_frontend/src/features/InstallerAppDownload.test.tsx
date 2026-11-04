import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import InstallerAppDownload, { fileSize, visitorPlatform } from './InstallerAppDownload'

const WINDOWS = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36'
const MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15'
const LINUX = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36'

const windowsFile = {
  platform: 'windows', arch: 'x64', kind: 'setup',
  filename: 'StateLife-Device-Installer-Setup-1.1.0.exe', size: 86_123_456, sha256: 'a'.repeat(64),
  download_path: '/api/v1/installer/apps/1.1.0/StateLife-Device-Installer-Setup-1.1.0.exe',
}
const macFile = {
  platform: 'macos', arch: 'arm64', kind: 'dmg',
  filename: 'StateLife-Device-Installer-1.1.0-macOS-arm64.dmg', size: 99_614_720, sha256: 'b'.repeat(64),
  download_path: '/api/v1/installer/apps/1.1.0/StateLife-Device-Installer-1.1.0-macOS-arm64.dmg',
}
const published = (files: unknown[]) => ({
  latest: { version: '1.1.0', git_sha: 'c'.repeat(40), published_at: '2026-11-04T09:30:00Z', files },
  min_app_version: '1.0.0',
})

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }

function renderAs(userAgent: string, body: unknown, status = 200, maxTouchPoints = 0) {
  vi.stubGlobal('navigator', { ...navigator, userAgent, maxTouchPoints, clipboard: { writeText: vi.fn(async () => undefined) } })
  const fetch = vi.fn(async () => response(body, status))
  vi.stubGlobal('fetch', fetch)
  render(<InstallerAppDownload toast={toast} />)
  return fetch
}

describe('State Life Device Installer download card', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('offers Windows first on a Windows computer, with the Mac as a smaller link', async () => {
    const fetch = renderAs(WINDOWS, published([windowsFile, macFile]))
    const primary = await screen.findByRole('link', { name: 'Download for Windows' })
    expect(fetch).toHaveBeenCalledWith('/api/v1/installer/apps', expect.objectContaining({ credentials: 'include' }))
    expect(primary.getAttribute('href')).toBe(windowsFile.download_path)
    expect(primary.getAttribute('download')).toBe(windowsFile.filename)
    expect(primary.className).toContain('primary')
    const other = screen.getByRole('link', { name: 'Download for Mac' })
    expect(other.getAttribute('href')).toBe(macFile.download_path)
    expect(other.className).not.toContain('button')
    expect(screen.getByText('Version 1.1.0')).toBeTruthy()
    expect(screen.getAllByText('a'.repeat(64))).toHaveLength(1)
    expect(screen.getByText('82.1 MB (86,123,456 bytes)')).toBeTruthy()
    expect(screen.getByText(/Windows protected your PC/)).toBeTruthy()
    expect(screen.getByText(/Copies older than version 1\.0\.0 cannot set up boards/)).toBeTruthy()
    // The Mac file's checksum and first-run steps wait behind a disclosure.
    const details = screen.getByText('Mac file and first run').closest('details') as HTMLElement
    expect(within(details).getByText('b'.repeat(64))).toBeTruthy()
    expect(within(details).getByText(/Privacy & Security/)).toBeTruthy()
    expect(within(details).getByText(/Apple silicon \(M1 or newer\) only/)).toBeTruthy()
  })

  it('offers the Mac first on a Mac', async () => {
    renderAs(MAC, published([windowsFile, macFile]))
    const primary = await screen.findByRole('link', { name: 'Download for Mac' })
    expect(primary.className).toContain('primary')
    expect(screen.getByText(/Using Windows\?/)).toBeTruthy()
    expect(screen.getByText(/Open Anyway/)).toBeTruthy()
  })

  it('copies the SHA-256', async () => {
    renderAs(WINDOWS, published([windowsFile, macFile]))
    fireEvent.click(await screen.findByRole('button', { name: 'Copy the SHA-256 of the Windows file' }))
    await vi.waitFor(() => expect(toast.notice).toHaveBeenCalledWith('SHA-256 copied.'))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('a'.repeat(64))
  })

  it('says when the visitor’s platform has no file yet', async () => {
    renderAs(MAC, published([windowsFile]))
    const primary = await screen.findByRole('link', { name: 'Download for Windows' })
    expect(primary.className).toContain('primary')
    expect(screen.getByText(/There is no Mac version of 1\.1\.0 yet/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Download for Mac' })).toBeNull()
  })

  it('offers both on a computer it does not recognise', async () => {
    renderAs(LINUX, published([windowsFile, macFile]))
    expect(await screen.findByRole('link', { name: 'Download for Windows' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Download for Mac' })).toBeTruthy()
    expect(screen.queryByText(/There is no/)).toBeNull()
  })

  it('shows an empty state when nothing is published', async () => {
    renderAs(WINDOWS, { latest: null, min_app_version: '1.0.0' })
    expect(await screen.findByText('No version of the app is published yet.')).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('says when the installer is switched off', async () => {
    renderAs(WINDOWS, { code: 'INSTALLER_DISABLED', message: 'Off.', detail: { code: 'INSTALLER_DISABLED', message: 'Off.' } }, 409)
    expect(await screen.findByText(/switched off on this server/)).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('offers to try again when the list cannot be loaded', async () => {
    const fetch = renderAs(WINDOWS, { rows: [] })
    const retry = await screen.findByRole('button', { name: 'Try again' })
    expect(screen.getByRole('alert').textContent).toContain('could not be loaded')
    fetch.mockImplementation(async () => response(published([windowsFile])))
    fireEvent.click(retry)
    expect(await screen.findByRole('link', { name: 'Download for Windows' })).toBeTruthy()
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})

describe('visitorPlatform and fileSize', () => {
  it('recognises Windows and Mac computers only', () => {
    expect(visitorPlatform(WINDOWS)).toBe('windows')
    expect(visitorPlatform(MAC)).toBe('macos')
    expect(visitorPlatform(MAC, 5)).toBeNull() // an iPad presenting itself as a Mac
    expect(visitorPlatform(LINUX)).toBeNull()
    expect(visitorPlatform('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)')).toBeNull()
    expect(visitorPlatform('Mozilla/5.0 (Linux; Android 15; Pixel 9)')).toBeNull()
  })

  it('formats sizes like the computer does', () => {
    expect(fileSize(512)).toBe('1 KB')
    expect(fileSize(300 * 1024)).toBe('300 KB')
    expect(fileSize(86_123_456)).toBe('82.1 MB')
  })
})
