import { useEffect, useMemo, useState } from 'react'
import { z } from 'zod'
import { api, ApiError } from '../api'
import { dayTime } from '../lib/time'
import { Icon } from '../Icon'
import { TechnicalDetails } from '../ui'
import type { Toast } from '../ui/Toast'
import { INSTALLER_CARD, SETUP_OFF_CARD, SETUP_PAGE } from '../words/setup'

// The State Life Device Installer download card on Set up a device
// (GET /api/v1/installer/apps, docs/esp-installer-server.md "App store").
// It is its own lazy chunk. The page may pass the apps it already read.

const appFileSchema = z.object({
  platform: z.string(),
  arch: z.string(),
  kind: z.string(),
  filename: z.string(),
  size: z.number(),
  sha256: z.string(),
  download_path: z.string(),
})
export const installerAppsSchema = z.object({
  latest: z
    .object({
      version: z.string(),
      git_sha: z.string(),
      published_at: z.string(),
      files: z.array(appFileSchema),
    })
    .nullable(),
  min_app_version: z.string(),
})
export type InstallerApps = z.infer<typeof installerAppsSchema>
export type DesktopPlatform = 'windows' | 'macos'
type AppFile = z.infer<typeof appFileSchema> & { platform: DesktopPlatform }

const platformNames: Record<DesktopPlatform, string> = {
  windows: 'Windows',
  macos: 'Mac',
}
const firstRunSteps: Record<DesktopPlatform, string[]> = {
  windows: [
    'For Windows 10 or 11 (64-bit). No administrator password is needed.',
    'Run the Setup file. If Windows says “Windows protected your PC”, click More info, then Run anyway.',
  ],
  macos: [
    'For Macs with Apple silicon (M1 or newer) only.',
    'Open the downloaded file and drag the app into Applications.',
    'Open the app. If the Mac says it was not opened, click Done.',
    'Go to System Settings > Privacy & Security. Scroll down, click Open Anyway and confirm.',
  ],
}

/** A phone or tablet: the app downloads are replaced by a note. */
export function touchDevice(userAgent: string, maxTouchPoints = 0) {
  return /iPhone|iPad|iPod|Android/i.test(userAgent) || (/Macintosh|Mac OS X/i.test(userAgent) && maxTouchPoints > 1)
}

export function visitorPlatform(
  userAgent: string,
  maxTouchPoints = 0,
): DesktopPlatform | null {
  if (/iPhone|iPad|iPod|Android/i.test(userAgent)) return null
  if (/Windows/i.test(userAgent)) return 'windows'
  // iPadOS Safari presents itself as a Mac; only a Mac has no touch screen.
  if (/Macintosh|Mac OS X/i.test(userAgent))
    return maxTouchPoints > 1 ? null : 'macos'
  return null
}

export function fileSize(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; apps: InstallerApps }
  | { kind: 'disabled' }
  | { kind: 'failed' }

function FileFacts({
  file,
  onCopy,
  copied,
}: {
  file: AppFile
  onCopy: (value: string) => void
  copied: boolean
}) {
  return (
    <TechnicalDetails summary={`Technical details (${platformNames[file.platform]} file)`} copyText={file.sha256}>
      <dl className="installer-app-facts">
        <div>
          <dt>File</dt>
          <dd>{file.filename}</dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd>
            {fileSize(file.size)} ({file.size.toLocaleString('en-US')} bytes)
          </dd>
        </div>
        <div>
          <dt>SHA-256</dt>
          <dd className="installer-app-hash">
            <code>{file.sha256}</code>
            <button
              type="button"
              className="icon-button"
              aria-label={`Copy the SHA-256 of the ${platformNames[file.platform]} file`}
              onClick={() => onCopy(file.sha256)}
            >
              <Icon name={copied ? 'check' : 'copy'} />
            </button>
          </dd>
        </div>
      </dl>
      <p>{INSTALLER_CARD.compare}</p>
    </TechnicalDetails>
  )
}

function FirstRun({ platform }: { platform: DesktopPlatform }) {
  return (
    <div className="installer-app-first-run">
      <strong>{INSTALLER_CARD.firstTime(platform)}</strong>
      <ol>
        {firstRunSteps[platform].map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
    </div>
  )
}

export default function InstallerAppDownload({
  toast,
  refreshKey = 0,
  apps,
}: {
  toast: Pick<Toast, 'notice' | 'error'>
  refreshKey?: number
  /** The apps the page already read; the card then makes no request of its own. */
  apps?: InstallerApps | null
}) {
  const [state, setState] = useState<LoadState>(apps ? { kind: 'ready', apps } : { kind: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const [copied, setCopied] = useState('')
  const platform = useMemo(
    () => visitorPlatform(navigator.userAgent, navigator.maxTouchPoints || 0),
    [],
  )
  const touch = useMemo(() => touchDevice(navigator.userAgent, navigator.maxTouchPoints || 0), [])

  useEffect(() => {
    if (apps) {
      setState({ kind: 'ready', apps })
      return
    }
    let current = true
    api<unknown>('/api/v1/installer/apps')
      .then((raw) => {
        if (current)
          setState({ kind: 'ready', apps: installerAppsSchema.parse(raw) })
      })
      .catch((error) => {
        if (!current) return
        setState(
          error instanceof ApiError && error.code === 'INSTALLER_DISABLED'
            ? { kind: 'disabled' }
            : { kind: 'failed' },
        )
      })
    return () => {
      current = false
    }
  }, [refreshKey, attempt, apps])
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(''), 1500)
    return () => window.clearTimeout(timer)
  }, [copied])

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(value)
      toast.notice(INSTALLER_CARD.copied)
    } catch {
      toast.error(INSTALLER_CARD.copyFailed)
    }
  }

  const latest = state.kind === 'ready' ? state.apps.latest : null
  const minVersion = state.kind === 'ready' ? state.apps.min_app_version : null
  const files = (latest?.files ?? []).filter(
    (file): file is AppFile =>
      file.platform === 'windows' || file.platform === 'macos',
  )
  const primary = files.find((file) => file.platform === platform) ?? files[0]
  const others = files.filter((file) => file !== primary)

  return (
    <section
      className="provisioning-card installer-app-card"
      aria-labelledby="installer-app-title"
      aria-busy={state.kind === 'loading'}
    >
      <div className="installer-app-heading">
        <Icon name="download" />
        <div>
          <h2 id="installer-app-title">{INSTALLER_CARD.title}</h2>
          <p>{INSTALLER_CARD.intro.split(/(ZKTeco|Hikvision)/).map((part, index) => (index % 2 ? <span key={part} data-brand>{part}</span> : part))}</p>
        </div>
        {latest && primary && (
          <span className="installer-app-version">Version {latest.version}</span>
        )}
      </div>
      {state.kind === 'loading' && (
        <p className="installer-app-message" role="status">
          {INSTALLER_CARD.loading}
        </p>
      )}
      {state.kind === 'disabled' && (
        <p className="installer-app-message" role="status">
          {SETUP_OFF_CARD.sentence} {SETUP_OFF_CARD.ask}
        </p>
      )}
      {state.kind === 'failed' && (
        <div className="installer-app-message" role="alert">
          <p>{INSTALLER_CARD.failed}</p>
          <button
            type="button"
            className="button secondary small"
            onClick={() => setAttempt((value) => value + 1)}
          >
            <Icon name="refresh" /> Try again
          </button>
        </div>
      )}
      {state.kind === 'ready' && !primary && (
        <div className="installer-app-message" role="status">
          <strong>{INSTALLER_CARD.none}</strong>
          <p>{INSTALLER_CARD.noneHelp}</p>
        </div>
      )}
      {latest && primary && touch && (
        <p className="installer-app-note">
          {SETUP_PAGE.phoneNote}{' '}
          {files.map((file) => `${platformNames[file.platform]}: ${fileSize(file.size)}`).join(' · ')}
        </p>
      )}
      {latest && primary && !touch && (
        <>
          <div className="installer-app-primary">
            <a
              className="button primary large"
              href={primary.download_path}
              download={primary.filename}
            >
              <Icon name="download" /> Download for {platformNames[primary.platform]}
            </a>
            <span>
              {fileSize(primary.size)} · published {dayTime(latest.published_at)}
            </span>
          </div>
          {platform && primary.platform !== platform && (
            <p className="installer-app-note">
              There is no {platformNames[platform]} version of {latest.version}{' '}
              yet.
            </p>
          )}
          {others.map((file) => (
            <p className="installer-app-other" key={file.filename}>
              {file.platform === 'macos' ? 'Using a Mac?' : 'Using Windows?'}{' '}
              <a href={file.download_path} download={file.filename}>
                Download for {platformNames[file.platform]}
              </a>{' '}
              ({fileSize(file.size)})
            </p>
          ))}
          <FirstRun platform={primary.platform} />
          {others.map((file) => (
            <details className="installer-app-details" key={file.filename}>
              <summary>{platformNames[file.platform]}: first time</summary>
              <FirstRun platform={file.platform} />
              <FileFacts file={file} copied={copied === file.sha256} onCopy={(value) => void copy(value)} />
            </details>
          ))}
          {minVersion && (
            <p className="installer-app-note">{INSTALLER_CARD.tooOld(minVersion)}</p>
          )}
          <FileFacts file={primary} copied={copied === primary.sha256} onCopy={(value) => void copy(value)} />
        </>
      )}
    </section>
  )
}
