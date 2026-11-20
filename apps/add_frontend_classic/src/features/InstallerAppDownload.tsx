import { useEffect, useMemo, useState } from 'react'
import { z } from 'zod'
import { api, ApiError } from '../api'
import { dateTime, type useToast } from '../App'
import { Icon } from '../Icon'

// The State Life Device Installer download card on the Prepare page
// (GET /api/v1/installer/apps, docs/esp-installer-server.md "App store").
// It is its own lazy chunk, so the Prepare page's bundle budget is unchanged.

type Toast = ReturnType<typeof useToast>

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
type InstallerApps = z.infer<typeof installerAppsSchema>
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
}: {
  file: AppFile
  onCopy: (value: string) => void
}) {
  return (
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
            <Icon name="copy" />
          </button>
        </dd>
      </div>
    </dl>
  )
}

function FirstRun({ platform }: { platform: DesktopPlatform }) {
  return (
    <div className="installer-app-first-run">
      <strong>First run on {platform === 'windows' ? 'Windows' : 'a Mac'}</strong>
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
}: {
  toast: Toast
  refreshKey?: number
}) {
  const [state, setState] = useState<LoadState>({ kind: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const platform = useMemo(
    () => visitorPlatform(navigator.userAgent, navigator.maxTouchPoints || 0),
    [],
  )

  useEffect(() => {
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
  }, [refreshKey, attempt])

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      toast.notice('SHA-256 copied.')
    } catch {
      toast.error('The SHA-256 could not be copied. Select it and copy it instead.')
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
          <p className="eyebrow">APP FOR SITE STAFF</p>
          <h2 id="installer-app-title">State Life Device Installer</h2>
          <p>
            Sets up a board for a ZKTeco or Hikvision attendance machine. Staff
            sign in with the dashboard's username and password.
          </p>
        </div>
        {latest && primary && (
          <span className="installer-app-version">Version {latest.version}</span>
        )}
      </div>
      {state.kind === 'loading' && (
        <p className="installer-app-message" role="status">
          Looking for the latest app…
        </p>
      )}
      {state.kind === 'disabled' && (
        <p className="installer-app-message" role="status">
          The installer is switched off on this server, so the app cannot be
          downloaded here.
        </p>
      )}
      {state.kind === 'failed' && (
        <div className="installer-app-message" role="alert">
          <p>The app downloads could not be loaded.</p>
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
          <strong>No version of the app is published yet.</strong>
          <p>
            The Windows and Mac downloads appear here once a release build is
            published to this server.
          </p>
        </div>
      )}
      {latest && primary && (
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
              {fileSize(primary.size)} · published {dateTime(latest.published_at)}
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
          <FileFacts file={primary} onCopy={(value) => void copy(value)} />
          <FirstRun platform={primary.platform} />
          {others.map((file) => (
            <details className="installer-app-details" key={file.filename}>
              <summary>{platformNames[file.platform]} file and first run</summary>
              <FileFacts file={file} onCopy={(value) => void copy(value)} />
              <FirstRun platform={file.platform} />
            </details>
          ))}
          <p className="installer-app-note">
            Compare the SHA-256 to check that a copy is unchanged.
            {minVersion &&
              ` Copies older than version ${minVersion} cannot set up boards.`}
          </p>
        </>
      )}
    </section>
  )
}
