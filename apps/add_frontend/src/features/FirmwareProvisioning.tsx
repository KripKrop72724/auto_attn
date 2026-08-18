import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { useBlocker } from 'react-router-dom'
import { z } from 'zod'
import './Firmware.css'
import { api, ApiError } from '../api'
import {
  Dialog,
  PageHeader,
  StatusBadge,
  dateTime,
  idempotency,
  relativeTime,
  type useToast,
} from '../App'
import { Icon } from '../Icon'
import type { FirmwareSection } from '../types'

type Toast = ReturnType<typeof useToast>

const bundleSchema = z.object({
  bundle_id: z.string(),
  hardware_profile: z.string(),
  version: z.string(),
  git_sha: z.string(),
  partition_layout: z.string(),
  manifest_sha256: z.string(),
  signing_key_ids: z.array(z.string()),
  images: z.array(
    z.object({
      name: z.string().nullish(),
      offset: z.number().nullish(),
      size: z.number().nullish(),
      sha256: z.string().nullish(),
    }),
  ),
  state: z.string(),
  published_at: z.string(),
})
const capabilitiesSchema = z.object({
  enabled: z.boolean(),
  supported_platforms: z.array(z.string()),
  hardware_profile: z.string(),
  companion_min_version: z.string(),
  latest_bundle: bundleSchema.nullable(),
  can_start: z.boolean(),
})
const companionSchema = z.object({
  companion_id: z.string(),
  platform: z.string(),
  application_version: z.string(),
  paired: z.boolean(),
  revoked: z.boolean(),
  online: z.boolean(),
  update_required: z.boolean(),
  paired_operator: z.string().nullable(),
  paired_at: z.string().nullable(),
  last_contact_at: z.string().nullable(),
})
const companionReleaseSchema = z.object({
  platform: z.string(),
  version: z.string(),
  filename: z.string(),
  sha256: z.string(),
  size: z.number(),
  git_sha: z.string(),
  download_url: z.string(),
  os_signed: z.boolean(),
})
const eventSchema = z.object({
  sequence: z.number(),
  state: z.string(),
  progress: z.number(),
  source: z.string(),
  details: z.record(z.string(), z.unknown()),
  created_at: z.string(),
})
const sessionSchema = z.object({
  session_id: z.string(),
  operator: z.string(),
  companion_id: z.string().nullable(),
  hardware_mac: z.string().nullable(),
  hardware_classification: z.string().nullable(),
  hardware_evidence: z.record(z.string(), z.unknown()),
  mode: z.string().nullable(),
  bundle: bundleSchema.nullable(),
  zone_id: z.string().nullable(),
  zone_name: z.string().nullable(),
  device_id: z.string().nullable(),
  preferred_ip: z.string().nullable(),
  zkt_port: z.number().nullable(),
  state: z.string(),
  progress: z.number(),
  cancellable: z.boolean(),
  result: z.record(z.string(), z.unknown()),
  expires_at: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  events: z.array(eventSchema).optional(),
  terminal: z
    .object({
      observed_serial: z.string().nullable(),
      model: z.string().nullable(),
      ip_address: z.string().nullable(),
      binding_state: z.string(),
      certification_state: z.string(),
      writes_disabled_reason: z.string().nullable(),
    })
    .nullable()
    .optional(),
})
type ProvisioningSession = z.infer<typeof sessionSchema>

const sessionListSchema = z.object({
  rows: z.array(sessionSchema),
  next_cursor: z.number().nullable().optional(),
  filtered_total: z.number().optional(),
})

export function awaitingTerminalSessionsPath(query = '') {
  const params = new URLSearchParams({
    state: 'WAITING_FOR_TERMINAL_CONFIRMATION',
    limit: '200',
  })
  const normalized = query.trim()
  if (normalized) params.set('q', normalized)
  return `/api/v1/provisioning/sessions?${params.toString()}`
}

const utf8 = (value: string) => new TextEncoder().encode(value).length
const identifier = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
export const provisioningConfigurationSchema = z.object({
  wifi_ssid: z
    .string()
    .refine(
      (value) => utf8(value) >= 1 && utf8(value) <= 32,
      'Use 1–32 UTF-8 bytes.',
    ),
  wifi_password: z
    .string()
    .refine(
      (value) =>
        /^[0-9a-fA-F]{64}$/.test(value) ||
        (utf8(value) >= 8 && utf8(value) <= 63),
      'Use 8–63 UTF-8 bytes or a 64-digit hex PSK.',
    ),
  communication_key: z
    .string()
    .refine((value) => /^\d+$/.test(value), 'Enter an unsigned whole number.')
    .refine(
      (value) => /^\d+$/.test(value) && BigInt(value) <= 4_294_967_295n,
      'Maximum is 4294967295.',
    ),
  zkt_port: z
    .string()
    .regex(/^\d+$/, 'Enter a TCP port.')
    .refine(
      (value) => Number(value) >= 1 && Number(value) <= 65535,
      'Use a port from 1 to 65535.',
    ),
  preferred_ip: z.string().refine((value) => {
    if (value === '0.0.0.0') return true
    const parts = value.split('.').map(Number)
    if (
      parts.length !== 4 ||
      parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)
    )
      return false
    return (
      parts[0] === 10 ||
      (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
      (parts[0] === 192 && parts[1] === 168)
    )
  }, 'Use 0.0.0.0 or an RFC1918 private IPv4 address.'),
  device_id: z
    .string()
    .refine(
      (value) =>
        value.length >= 1 && value.length <= 31 && identifier.test(value),
      'Use 1–31 letters, digits, dots, dashes or underscores.',
    ),
  zone_id: z
    .string()
    .refine(
      (value) =>
        value.length >= 1 && value.length <= 64 && identifier.test(value),
      'Use 1–64 letters, digits, dots, dashes or underscores.',
    ),
  zone_name: z
    .string()
    .refine(
      (value) =>
        value === value.trim() &&
        utf8(value) >= 1 &&
        utf8(value) <= 120 &&
        !/[\u0000-\u001f\u007f]/.test(value),
      'Use 1–120 UTF-8 bytes with no leading, trailing or control characters.',
    ),
})
type Configuration = z.infer<typeof provisioningConfigurationSchema>

const terminalStates = new Set([
  'VERIFIED_ONLINE',
  'SITE_VALIDATION_PENDING',
  'RECOVERY_REQUIRED',
  'FAILED',
  'CANCELLED',
  'EXPIRED',
])
const writingStates = new Set([
  'EFUSE_BURNING',
  'EFUSE_VERIFIED',
  'FLASHING',
  'READBACK_VERIFYING',
])
const steps = [
  'Environment',
  'Connect',
  'Configure',
  'Authorize',
  'Flash & verify',
  'Complete',
] as const
const elapsed = (seconds: number) =>
  `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
const humanizeProvisioningState = (state: string) =>
  state
    .replaceAll('_', ' ')
    .toLowerCase()
    .replace(/^./, (letter) => letter.toUpperCase())
const relativeProvisioningTime = relativeTime

export function provisioningActiveStep(state?: string) {
  if (!state || state === 'WAITING_FOR_COMPANION') return 0
  if (['WAITING_FOR_DEVICE', 'INSPECTING'].includes(state)) return 1
  if (['CONFIGURING', 'PREFLIGHT_READY'].includes(state)) return 2
  if (state === 'AWAITING_AUTHORIZATION') return 3
  if (terminalStates.has(state)) return 5
  return 4
}

function platformChoice() {
  const ua = navigator.userAgent
  if (/Windows/i.test(ua)) return 'windows-x64'
  if (/Macintosh/i.test(ua) && !/iPhone|iPad/i.test(ua)) return 'macos-arm64'
  return null
}

export default function FirmwareProvisioning({
  revision,
  toast,
  username,
  onSection,
}: {
  revision: number
  toast: Toast
  username: string
  onSection: (section: FirmwareSection) => void
}) {
  const [capabilities, setCapabilities] = useState<z.infer<
    typeof capabilitiesSchema
  > | null>(null)
  const [companions, setCompanions] = useState<
    z.infer<typeof companionSchema>[]
  >([])
  const [companionRelease, setCompanionRelease] = useState<z.infer<
    typeof companionReleaseSchema
  > | null>(null)
  const [sessions, setSessions] = useState<ProvisioningSession[]>([])
  const [sessionCursor, setSessionCursor] = useState<number | null>(null)
  const [sessionTotal, setSessionTotal] = useState(0)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [awaitingSessions, setAwaitingSessions] = useState<ProvisioningSession[]>([])
  const [awaitingTotal, setAwaitingTotal] = useState(0)
  const [awaitingQuery, setAwaitingQuery] = useState('')
  const [awaitingLoading, setAwaitingLoading] = useState(false)
  const [zoneSuggestions, setZoneSuggestions] = useState<
    { zone_id: string; zone_name: string }[]
  >([])
  const [current, setCurrent] = useState<ProvisioningSession | null>(null)
  const [newSessionRequested, setNewSessionRequested] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [liveRefreshFailed, setLiveRefreshFailed] = useState(false)
  const [clock, setClock] = useState(() => Date.now())
  const [pairingCode, setPairingCode] = useState('')
  const [pairingPassword, setPairingPassword] = useState('')
  const [selectedCompanionId, setSelectedCompanionId] = useState('')
  const [companionQuery, setCompanionQuery] = useState('')
  const [revokeCompanionId, setRevokeCompanionId] = useState<string | null>(
    null,
  )
  const [revokeCompanionPassword, setRevokeCompanionPassword] = useState('')
  const [adminPassword, setAdminPassword] = useState('')
  const [terminalPassword, setTerminalPassword] = useState('')
  const [typedMac, setTypedMac] = useState('')
  const [labelAcknowledged, setLabelAcknowledged] = useState(false)
  const [showWifi, setShowWifi] = useState(false)
  const [showCommKey, setShowCommKey] = useState(false)
  const [ipMode, setIpMode] = useState<'automatic' | 'fixed'>('automatic')
  const [draft, setDraft] = useState<Configuration>({
    wifi_ssid: '',
    wifi_password: '',
    communication_key: '',
    zkt_port: '4370',
    preferred_ip: '0.0.0.0',
    device_id: '',
    zone_id: '',
    zone_name: '',
  })
  const [errors, setErrors] = useState<Record<string, string>>({})
  const headingRef = useRef<HTMLHeadingElement>(null)
  const platform = useMemo(platformChoice, [])
  const physicalUnsupported =
    platform === null || /Mobi|iPhone|iPad|Android/i.test(navigator.userAgent)
  const navigationBlocker = useBlocker(
    Boolean(current && writingStates.has(current.state)),
  )

  const load = useCallback(async () => {
    try {
      const [rawCapabilities, rawCompanions, rawSessions, rawAwaitingSessions, rawDevices] =
        await Promise.all([
          api<unknown>('/api/v1/provisioning/capabilities'),
          api<unknown>('/api/v1/provisioning/companions'),
          api<unknown>('/api/v1/provisioning/sessions?mine_only=true&limit=25'),
          api<unknown>(awaitingTerminalSessionsPath()),
          api<unknown>('/api/v1/devices?limit=200'),
        ])
      const nextCapabilities = capabilitiesSchema.parse(rawCapabilities)
      const nextCompanions = z
        .object({ rows: z.array(companionSchema) })
        .parse(rawCompanions).rows
      const sessionResponse = sessionListSchema.parse(rawSessions)
      const awaitingResponse = sessionListSchema.parse(rawAwaitingSessions)
      const nextSessions = sessionResponse.rows
      setCapabilities(nextCapabilities)
      setLiveRefreshFailed(false)
      setCompanions(nextCompanions)
      setSessions(nextSessions)
      setSessionCursor(sessionResponse.next_cursor ?? null)
      setSessionTotal(sessionResponse.filtered_total ?? nextSessions.length)
      setAwaitingSessions(awaitingResponse.rows)
      setAwaitingTotal(awaitingResponse.filtered_total ?? awaitingResponse.rows.length)
      const deviceRows = z
        .object({
          rows: z.array(
            z.object({ zone_id: z.string(), zone_name: z.string() }),
          ),
        })
        .parse(rawDevices).rows
      setZoneSuggestions(
        Array.from(
          new Map(deviceRows.map((item) => [item.zone_id, item])).values(),
        ),
      )
      if (platform && nextCapabilities.enabled) {
        try {
          setCompanionRelease(
            companionReleaseSchema.parse(
              await api<unknown>(
                `/api/v1/provisioning/companion-releases/latest?platform=${platform}`,
              ),
            ),
          )
        } catch {
          setCompanionRelease(null)
        }
      }
      const active = nextSessions.find(
        (row) => row.operator === username && !terminalStates.has(row.state),
      )
      if (active && !newSessionRequested) {
        const detail = sessionSchema.parse(
          await api<unknown>(
            `/api/v1/provisioning/sessions/${active.session_id}`,
          ),
        )
        setCurrent(detail)
        if (detail.companion_id) setSelectedCompanionId(detail.companion_id)
      }
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Provisioning status could not be loaded.',
      )
    } finally {
      setLoading(false)
    }
  }, [current, newSessionRequested, platform, toast, username])

  const refreshCurrent = useCallback(async () => {
    if (!current) return
    try {
      const [rawCompanions, rawSession] = await Promise.all([
        api<unknown>('/api/v1/provisioning/companions'),
        api<unknown>(`/api/v1/provisioning/sessions/${current.session_id}`),
      ])
      setCompanions(
        z.object({ rows: z.array(companionSchema) }).parse(rawCompanions).rows,
      )
      setCurrent(sessionSchema.parse(rawSession))
      setLiveRefreshFailed(false)
    } catch {
      setLiveRefreshFailed(true)
    }
  }, [current?.session_id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void load()
  }, [revision]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!current || terminalStates.has(current.state)) return
    // SSE drives normal updates. This narrow fallback polls only live companion
    // and session state, avoiding repeated firmware-catalog hashing and fleet reads.
    const timer = window.setInterval(() => void refreshCurrent(), 5_000)
    return () => window.clearInterval(timer)
  }, [current?.session_id, current?.state, refreshCurrent])
  useEffect(() => {
    headingRef.current?.focus()
  }, [provisioningActiveStep(current?.state)])
  useEffect(() => {
    const critical = Boolean(current && writingStates.has(current.state))
    if (!critical) return
    const protect = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', protect)
    return () => window.removeEventListener('beforeunload', protect)
  }, [current?.state])
  useEffect(() => {
    if (!current || provisioningActiveStep(current.state) !== 4) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [current?.session_id, current?.state])
  useEffect(
    () => () => {
      setDraft((value) => ({
        ...value,
        wifi_password: '',
        communication_key: '',
      }))
      setAdminPassword('')
      setPairingPassword('')
      setTerminalPassword('')
    },
    [],
  )

  const readyCompanions = companions.filter(
    (item) =>
      item.paired &&
      item.online &&
      !item.revoked &&
      !item.update_required &&
      item.paired_operator === username,
  )
  const shownCompanions = companions.filter((item) =>
    `${item.platform} ${item.application_version} ${item.companion_id} ${item.paired_operator || ''}`
      .toLowerCase()
      .includes(companionQuery.trim().toLowerCase()),
  )
  const online =
    readyCompanions.find((item) => item.companion_id === selectedCompanionId) ||
    null
  useEffect(() => {
    if (current?.companion_id) {
      setSelectedCompanionId(current.companion_id)
      return
    }
    if (!selectedCompanionId && readyCompanions.length === 1)
      setSelectedCompanionId(readyCompanions[0].companion_id)
  }, [current?.companion_id, readyCompanions, selectedCompanionId])
  const start = async () => {
    if (!online) return
    setBusy(true)
    try {
      const result = sessionSchema.parse(
        await api<unknown>('/api/v1/provisioning/sessions', {
          method: 'POST',
          body: JSON.stringify({
            companion_id: online.companion_id,
            idempotency_key: idempotency('provisioning-session'),
          }),
        }),
      )
      setNewSessionRequested(false)
      setCurrent(result)
      toast.notice('Companion is inspecting the connected ESP32-S3.')
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Session could not start.',
      )
    } finally {
      setBusy(false)
    }
  }

  const pair = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    try {
      await api('/api/v1/provisioning/pairings/approve', {
        method: 'POST',
        body: JSON.stringify({ code: pairingCode, password: pairingPassword }),
      })
      setPairingCode('')
      setPairingPassword('')
      toast.notice('Companion paired. It will connect automatically.')
      await load()
    } catch (error) {
      setPairingPassword('')
      toast.error(error instanceof Error ? error.message : 'Pairing failed.')
    } finally {
      setBusy(false)
    }
  }

  const configure = async (event: FormEvent) => {
    event.preventDefault()
    const parsed = provisioningConfigurationSchema.safeParse({
      ...draft,
      preferred_ip: ipMode === 'automatic' ? '0.0.0.0' : draft.preferred_ip,
    })
    if (!parsed.success) {
      const next: Record<string, string> = {}
      parsed.error.issues.forEach((issue) => {
        next[String(issue.path[0])] ??= issue.message
      })
      setErrors(next)
      document.getElementById(`provision-${Object.keys(next)[0]}`)?.focus()
      return
    }
    if (!current) return
    setBusy(true)
    setErrors({})
    try {
      const result = sessionSchema.parse(
        await api<unknown>(
          `/api/v1/provisioning/sessions/${current.session_id}/preflight`,
          {
            method: 'POST',
            body: JSON.stringify({
              ...parsed.data,
              communication_key: Number(parsed.data.communication_key),
              zkt_port: Number(parsed.data.zkt_port),
            }),
          },
        ),
      )
      setDraft((value) => ({
        ...value,
        wifi_password: '',
        communication_key: '',
      }))
      setCurrent(result)
    } catch (error) {
      if (error instanceof ApiError && Object.keys(error.fieldErrors).length)
        setErrors(
          Object.fromEntries(
            Object.entries(error.fieldErrors).map(([key, value]) => [
              key,
              value[0],
            ]),
          ),
        )
      toast.error(error instanceof Error ? error.message : 'Preflight failed.')
    } finally {
      setBusy(false)
    }
  }

  const authorize = async (event: FormEvent) => {
    event.preventDefault()
    if (!current) return
    setBusy(true)
    try {
      const result = sessionSchema.parse(
        await api<unknown>(
          `/api/v1/provisioning/sessions/${current.session_id}/authorize`,
          {
            method: 'POST',
            body: JSON.stringify({
              password: adminPassword,
              typed_mac: typedMac,
              physical_label_acknowledged: labelAcknowledged,
            }),
          },
        ),
      )
      setCurrent(result)
      setAdminPassword('')
      setTypedMac('')
      setLabelAcknowledged(false)
    } catch (error) {
      setAdminPassword('')
      toast.error(
        error instanceof Error ? error.message : 'Authorization failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  const cancel = async () => {
    if (!current?.cancellable) return
    setBusy(true)
    try {
      setCurrent(
        sessionSchema.parse(
          await api<unknown>(
            `/api/v1/provisioning/sessions/${current.session_id}/cancel`,
            { method: 'POST', body: '{}' },
          ),
        ),
      )
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Session could not be cancelled.',
      )
    } finally {
      setBusy(false)
    }
  }

  const revokeCompanion = async () => {
    if (!revokeCompanionId || !revokeCompanionPassword) return
    setBusy(true)
    try {
      await api(`/api/v1/provisioning/companions/${revokeCompanionId}/revoke`, {
        method: 'POST',
        body: JSON.stringify({ password: revokeCompanionPassword }),
      })
      setRevokeCompanionPassword('')
      setRevokeCompanionId(null)
      if (selectedCompanionId === revokeCompanionId) setSelectedCompanionId('')
      toast.notice('Provisioning companion revoked with an audit entry.')
      await load()
    } catch (error) {
      setRevokeCompanionPassword('')
      toast.error(
        error instanceof Error
          ? error.message
          : 'Companion could not be revoked.',
      )
    } finally {
      setBusy(false)
    }
  }

  const openHistory = async (sessionId: string) => {
    setHistoryLoading(true)
    try {
      setCurrent(
        sessionSchema.parse(
          await api<unknown>(`/api/v1/provisioning/sessions/${sessionId}`),
        ),
      )
      setNewSessionRequested(false)
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Provisioning receipt could not be loaded.',
      )
    } finally {
      setHistoryLoading(false)
    }
  }

  const loadOlderHistory = async () => {
    if (!sessionCursor) return
    setHistoryLoading(true)
    try {
      const response = z
        .object({
          rows: z.array(sessionSchema),
          next_cursor: z.number().nullable().optional(),
          filtered_total: z.number().optional(),
        })
        .parse(
          await api<unknown>(
            `/api/v1/provisioning/sessions?mine_only=true&cursor=${sessionCursor}&limit=25`,
          ),
        )
      setSessions((rows) => [
        ...new Map(
          [...rows, ...response.rows].map((row) => [row.session_id, row]),
        ).values(),
      ])
      setSessionCursor(response.next_cursor ?? null)
      setSessionTotal(response.filtered_total ?? sessionTotal)
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Older provisioning history could not be loaded.',
      )
    } finally {
      setHistoryLoading(false)
    }
  }

  const searchAwaitingSessions = async (event: FormEvent) => {
    event.preventDefault()
    setAwaitingLoading(true)
    try {
      const response = sessionListSchema.parse(
        await api<unknown>(awaitingTerminalSessionsPath(awaitingQuery)),
      )
      setAwaitingSessions(response.rows)
      setAwaitingTotal(response.filtered_total ?? response.rows.length)
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : 'Awaiting terminal confirmations could not be searched.',
      )
    } finally {
      setAwaitingLoading(false)
    }
  }

  const confirmTerminal = async (event: FormEvent) => {
    event.preventDefault()
    if (!current?.terminal?.observed_serial) return
    setBusy(true)
    try {
      await api(
        `/api/v1/provisioning/sessions/${current.session_id}/terminal-binding/confirm`,
        {
          method: 'POST',
          body: JSON.stringify({
            observed_serial: current.terminal.observed_serial,
            password: terminalPassword,
          }),
        },
      )
      setTerminalPassword('')
      toast.notice(
        'Terminal serial pinning was sent to the ESP32. Writes remain read-only until heartbeat verification.',
      )
      await load()
    } catch (error) {
      setTerminalPassword('')
      toast.error(
        error instanceof Error
          ? error.message
          : 'Terminal serial could not be confirmed.',
      )
    } finally {
      setBusy(false)
    }
  }

  const step = provisioningActiveStep(current?.state)
  const irreversible =
    current?.hardware_classification === 'BLANK_NEW' ||
    current?.hardware_classification === 'KNOWN_LEGACY'
  const currentAssignment = current?.hardware_evidence.current_assignment as
    | { zone_id?: string; device_id?: string; zone_name?: string }
    | undefined
  const latestTransfer = [...(current?.events || [])]
    .reverse()
    .find(
      (event) =>
        typeof event.details.bytes_completed === 'number' &&
        typeof event.details.bytes_total === 'number',
    )
  const operationStartedAt = (current?.events || []).find(
    (event) => event.state === 'PACKAGE_PREPARING',
  )?.created_at
  const elapsedSeconds = operationStartedAt
    ? Math.max(0, Math.floor((clock - Date.parse(operationStartedAt)) / 1_000))
    : 0
  const updateDraft = (key: keyof Configuration, value: string) =>
    setDraft((currentValue) => ({ ...currentValue, [key]: value }))
  const fieldError = (key: keyof Configuration) =>
    errors[key] ? (
      <small id={`provision-${key}-error`} className="field-error">
        {errors[key]}
      </small>
    ) : null
  const beginAnother = () => {
    setNewSessionRequested(true)
    setCurrent(null)
    setErrors({})
    setTypedMac('')
    setAdminPassword('')
    setTerminalPassword('')
    setDraft((value) => ({
      ...value,
      wifi_password: '',
      communication_key: '',
    }))
  }

  return (
    <div className="firmware-workspace firmware-provisioning-workspace">
      <PageHeader
        eyebrow="SECURE USB PROVISIONING"
        title="Prepare a Zone Lite ESP32"
        description="Connect one ESP32-S3. ADD will apply the latest approved signed firmware, provision encrypted site settings, verify every written range, and onboard it automatically."
        action={
          <button className="button secondary" onClick={() => void load()}>
            <Icon name="refresh" /> Refresh
          </button>
        }
      />
      <nav
        className="firmware-workspace-tabs"
        role="tablist"
        aria-label="Firmware sections"
      >
        {(['overview', 'prepare', 'releases', 'campaigns'] as const).map(
          (value) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={value === 'prepare'}
              tabIndex={value === 'prepare' ? 0 : -1}
              className={value === 'prepare' ? 'active' : ''}
              onClick={() => onSection(value)}
            >
              {value === 'prepare'
                ? 'Prepare device'
                : value === 'overview'
                  ? 'Overview'
                  : value === 'releases'
                    ? 'Signed releases'
                    : 'Campaigns'}
            </button>
          ),
        )}
      </nav>
      <section
        className="firmware-provisioning-readiness"
        aria-label="Provisioning readiness"
      >
        <article
          className={`pattern-${capabilities?.enabled ? 'confirmed' : 'blocked'}`}
        >
          <Icon name="power" />
          <span>
            <strong>Provisioning gate</strong>
            <small>
              {capabilities?.enabled
                ? 'Enabled for guarded physical work'
                : 'Disabled by production policy'}
            </small>
          </span>
          <StatusBadge state={capabilities?.enabled ? 'ENABLED' : 'DISABLED'} />
        </article>
        <article
          className={`pattern-${physicalUnsupported ? 'waiting' : 'confirmed'}`}
        >
          <Icon name="terminal" />
          <span>
            <strong>Operator platform</strong>
            <small>
              {physicalUnsupported
                ? 'Use Windows x64 or Apple-Silicon macOS'
                : platform === 'windows-x64'
                  ? 'Windows 10/11 x64 supported'
                  : 'Apple-Silicon macOS supported'}
            </small>
          </span>
          <StatusBadge
            state={physicalUnsupported ? 'VIEW ONLY' : 'SUPPORTED'}
          />
        </article>
        <article
          className={`pattern-${capabilities?.latest_bundle ? 'confirmed' : 'blocked'}`}
        >
          <Icon name="shield" />
          <span>
            <strong>Approved factory bundle</strong>
            <small>
              {capabilities?.latest_bundle
                ? `Zone Lite ${capabilities.latest_bundle.version} · signed manifest`
                : 'No available signed bundle'}
            </small>
          </span>
          <StatusBadge
            state={capabilities?.latest_bundle?.state || 'UNAVAILABLE'}
          />
        </article>
        <article
          className={`pattern-${online ? 'confirmed' : readyCompanions.length ? 'waiting' : 'blocked'}`}
        >
          <Icon name="wifi" />
          <span>
            <strong>Provisioning companion</strong>
            <small>
              {online
                ? `${online.platform} ${online.application_version} selected`
                : readyCompanions.length
                  ? 'Choose an online paired companion'
                  : 'No operator-owned companion is ready'}
            </small>
          </span>
          <StatusBadge state={online ? 'READY' : 'NOT READY'} />
        </article>
      </section>
      <p className="sr-only" aria-live="polite">
        {current
          ? `Provisioning state ${humanizeProvisioningState(current.state)}, ${current.progress} percent complete.`
          : 'No active provisioning session.'}
      </p>
      {liveRefreshFailed && (
        <div className="info-copy pattern-waiting" role="status">
          <Icon name="alert" />
          <div>
            <strong>Live ADD updates are temporarily unavailable</strong>
            <p>
              No new write will start without ADD. An already authorized local
              write can finish its safety checks; server verification resumes
              after reconnect.
            </p>
          </div>
        </div>
      )}
      {current?.state === 'AWAITING_AUTHORIZATION' && current.bundle && (
        <details className="panel provisioning-bundle-evidence">
          <summary>Review signed file hashes and flash ranges</summary>
          <ul>
            {current.bundle.images.map((image) => (
              <li key={image.name || image.sha256}>
                <strong>{image.name}</strong>
                <code>{image.sha256}</code>
                <span>
                  {image.offset == null
                    ? 'No offset'
                    : `0x${image.offset.toString(16)}`}{' '}
                  · {image.size ?? '—'} bytes
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="provisioning-layout" aria-busy={loading || busy}>
        <ol
          className="provisioning-stepper"
          aria-label="Provisioning progress"
          tabIndex={0}
        >
          {steps.map((label, index) => (
            <li
              key={label}
              className={index < step ? 'done' : index === step ? 'active' : ''}
              aria-current={index === step ? 'step' : undefined}
            >
              <span>{index < step ? <Icon name="check" /> : index + 1}</span>
              <strong>{label}</strong>
            </li>
          ))}
        </ol>
        <main className="provisioning-main">
          {!capabilities?.enabled && (
            <section className="provisioning-state pattern-blocked">
              <Icon name="shield" />
              <div>
                <p className="eyebrow">CONTROLLED ROLLOUT</p>
                <h2 tabIndex={-1} ref={headingRef}>
                  Physical provisioning is disabled
                </h2>
                <p>
                  Existing OTA and fleet operations remain available. Enable
                  provisioning only after companion packages and an immutable
                  factory bundle pass HIL.
                </p>
              </div>
            </section>
          )}
          {capabilities?.enabled && physicalUnsupported && (
            <section className="provisioning-state pattern-waiting">
              <Icon name="terminal" />
              <div>
                <p className="eyebrow">DESKTOP COMPANION REQUIRED</p>
                <h2 tabIndex={-1} ref={headingRef}>
                  Open ADD on a supported computer
                </h2>
                <p>
                  Physical flashing supports Windows 10/11 x64 and Apple-Silicon
                  macOS. This screen remains available for history and receipts.
                </p>
              </div>
            </section>
          )}
          {capabilities?.enabled &&
            !physicalUnsupported &&
            !current &&
            companions.length > 0 && (
              <section className="provisioning-card firmware-companion-picker">
                <div className="panel-header">
                  <div>
                    <p className="eyebrow">OPERATOR-OWNED USB BRIDGE</p>
                    <h2>Select a provisioning companion</h2>
                    <p>
                      Only an online, current, non-revoked companion paired to{' '}
                      {username} can start a session.
                    </p>
                  </div>
                  <StatusBadge state={`${readyCompanions.length} READY`} />
                </div>
                <label className="search-field">
                  <span className="sr-only">
                    Search provisioning companions
                  </span>
                  <Icon name="search" />
                  <input
                    value={companionQuery}
                    onChange={(event) => setCompanionQuery(event.target.value)}
                    placeholder="Search platform, version, ID, or operator"
                  />
                </label>
                <div role="listbox" aria-label="Provisioning companions">
                  {shownCompanions.map((companion) => {
                    const ready =
                      companion.paired &&
                      companion.online &&
                      !companion.revoked &&
                      !companion.update_required &&
                      companion.paired_operator === username
                    return (
                      <article
                        key={companion.companion_id}
                        className={`pattern-${ready ? 'confirmed' : companion.revoked ? 'blocked' : 'waiting'}`}
                      >
                        <button
                          type="button"
                          role="option"
                          aria-selected={
                            companion.companion_id === selectedCompanionId
                          }
                          disabled={!ready}
                          onClick={() =>
                            setSelectedCompanionId(companion.companion_id)
                          }
                        >
                          <Icon name="terminal" />
                          <span>
                            <strong>
                              {companion.platform} ·{' '}
                              {companion.application_version}
                            </strong>
                            <small>
                              {companion.companion_id} ·{' '}
                              {companion.paired_operator || 'unpaired'} ·{' '}
                              {companion.online
                                ? 'online'
                                : `last contact ${relativeProvisioningTime(companion.last_contact_at)}`}
                            </small>
                          </span>
                          <StatusBadge
                            state={
                              companion.revoked
                                ? 'REVOKED'
                                : companion.update_required
                                  ? 'UPDATE REQUIRED'
                                  : companion.online
                                    ? 'ONLINE'
                                    : 'OFFLINE'
                            }
                          />
                        </button>
                        {companion.paired_operator === username &&
                          !companion.revoked && (
                            <button
                              type="button"
                              className="button text-button"
                              onClick={() =>
                                setRevokeCompanionId(companion.companion_id)
                              }
                            >
                              Revoke
                            </button>
                          )}
                      </article>
                    )
                  })}
                  {!shownCompanions.length && (
                    <div className="empty-state compact">
                      <Icon name="search" />
                      <p>No companions match this search.</p>
                    </div>
                  )}
                </div>
              </section>
            )}
          {capabilities?.enabled &&
            !physicalUnsupported &&
            readyCompanions.length === 0 && (
              <section className="provisioning-card">
                <p className="eyebrow">STEP 1 · ENVIRONMENT</p>
                <h2 tabIndex={-1} ref={headingRef}>
                  Connect the provisioning companion
                </h2>
                <p>
                  The native companion owns USB and irreversible eFuse
                  operations. It never receives fleet-root or ORDS credentials.
                </p>
                <div className="companion-install">
                  <Icon name="terminal" />
                  <div>
                    <strong>
                      {platform === 'windows-x64'
                        ? 'Windows 10/11 x64'
                        : 'macOS Apple Silicon'}
                      {companionRelease ? ` · ${companionRelease.version}` : ''}
                    </strong>
                    <p>
                      The package is not OS code-signed. ADD verifies its
                      Ed25519 release manifest and publishes the exact SHA-256.
                    </p>
                    {companionRelease && <code>{companionRelease.sha256}</code>}
                  </div>
                  {companionRelease ? (
                    <a
                      className="button primary"
                      href={companionRelease.download_url}
                    >
                      <Icon name="power" /> Download verified package
                    </a>
                  ) : (
                    <button className="button primary" disabled>
                      No verified package
                    </button>
                  )}
                </div>
                <details>
                  <summary>Unsigned installation instructions</summary>
                  <p>
                    {platform === 'windows-x64'
                      ? 'In SmartScreen, choose More info, verify the publisher warning and SHA-256 shown by ADD, then choose Run anyway.'
                      : 'Move the app to Applications. Control-click it, choose Open, compare the SHA-256 shown by ADD, then confirm Open.'}
                  </p>
                </details>
                <form
                  className="pairing-form"
                  onSubmit={(event) => void pair(event)}
                >
                  <label>
                    Six-digit pairing code
                    <input
                      inputMode="numeric"
                      pattern="[0-9]{6}"
                      maxLength={6}
                      value={pairingCode}
                      onChange={(event) =>
                        setPairingCode(event.target.value.replace(/\D/g, ''))
                      }
                    />
                  </label>
                  <label>
                    Administrator password
                    <input
                      type="password"
                      autoComplete="current-password"
                      value={pairingPassword}
                      onChange={(event) =>
                        setPairingPassword(event.target.value)
                      }
                    />
                  </label>
                  <button
                    className="button primary"
                    disabled={
                      pairingCode.length !== 6 || !pairingPassword || busy
                    }
                  >
                    Pair companion
                  </button>
                </form>
              </section>
            )}
          {capabilities?.enabled &&
            !physicalUnsupported &&
            readyCompanions.length > 0 &&
            !current && (
              <section className="provisioning-card connect-card">
                <div className="board-illustration">
                  <Icon name="terminal" />
                  <span className="usb-line" />
                </div>
                <p className="eyebrow">STEP 2 · CONNECT</p>
                <h2 tabIndex={-1} ref={headingRef}>
                  {online
                    ? 'Plug in one ESP32-S3'
                    : 'Choose a ready companion above'}
                </h2>
                <p>
                  Use a data-capable USB cable. Close ESP-IDF, Arduino IDE, and
                  every serial monitor.
                </p>
                <ul>
                  <li>The selected companion inspects one USB device.</li>
                  <li>
                    Wrong chips and hardware profiles are rejected before
                    writing.
                  </li>
                  <li>The approved bundle is selected server-side.</li>
                </ul>
                <button
                  className="button primary large"
                  disabled={!online || !capabilities.can_start || busy}
                  onClick={() => void start()}
                >
                  <Icon name="power" />{' '}
                  {online
                    ? `Connect through ${online.platform}`
                    : 'Select a companion first'}
                </button>
              </section>
            )}
          {current &&
            [
              'WAITING_FOR_COMPANION',
              'WAITING_FOR_DEVICE',
              'INSPECTING',
            ].includes(current.state) && (
              <section className="provisioning-card">
                <p className="eyebrow">DEVICE INSPECTION</p>
                <h2 tabIndex={-1} ref={headingRef}>
                  {current.state === 'WAITING_FOR_COMPANION'
                    ? 'Reconnect the paired companion'
                    : 'Looking for one ESP32-S3…'}
                </h2>
                <p>
                  ADD is reading the chip identity, Wi-Fi MAC, flash size, eFuse
                  purposes and Secure Boot evidence. Nothing is written during
                  inspection.
                </p>
                <div className="calm-loader">
                  <span />
                  <span />
                  <span />
                </div>
                {current.cancellable && (
                  <button
                    className="button secondary"
                    disabled={busy}
                    onClick={() => void cancel()}
                  >
                    Cancel safely
                  </button>
                )}
              </section>
            )}
          {current?.state === 'CONFIGURING' && (
            <form
              className="provisioning-form"
              onSubmit={(event) => void configure(event)}
              noValidate
            >
              <p className="eyebrow">STEP 3 · CONFIGURE</p>
              <h2 tabIndex={-1} ref={headingRef}>
                Set the destination and terminal identity
              </h2>
              {Object.keys(errors).length > 0 && (
                <div className="form-error-summary" role="alert">
                  <strong>
                    Check {Object.keys(errors).length} field
                    {Object.keys(errors).length === 1 ? '' : 's'}.
                  </strong>
                  <ul>
                    {Object.entries(errors).map(([key, value]) => (
                      <li key={key}>
                        <a href={`#provision-${key}`}>{value}</a>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <fieldset>
                <legend>Destination Wi-Fi</legend>
                <label>
                  Wi-Fi network (SSID)
                  <input
                    id="provision-wifi_ssid"
                    autoComplete="off"
                    value={draft.wifi_ssid}
                    onChange={(event) =>
                      updateDraft('wifi_ssid', event.target.value)
                    }
                    aria-invalid={Boolean(errors.wifi_ssid)}
                    aria-describedby={
                      errors.wifi_ssid ? 'provision-wifi_ssid-error' : undefined
                    }
                  />
                  <small>
                    Preserved exactly · {utf8(draft.wifi_ssid)}/32 bytes
                  </small>
                  {fieldError('wifi_ssid')}
                </label>
                <label>
                  Wi-Fi password
                  <span className="secret-input">
                    <input
                      id="provision-wifi_password"
                      type={showWifi ? 'text' : 'password'}
                      autoComplete="new-password"
                      value={draft.wifi_password}
                      onChange={(event) =>
                        updateDraft('wifi_password', event.target.value)
                      }
                      aria-invalid={Boolean(errors.wifi_password)}
                      aria-describedby={
                        errors.wifi_password
                          ? 'provision-wifi_password-error'
                          : undefined
                      }
                    />
                    <button
                      type="button"
                      aria-pressed={showWifi}
                      aria-label={`${showWifi ? 'Hide' : 'Show'} Wi-Fi password`}
                      onClick={() => setShowWifi((value) => !value)}
                    >
                      {showWifi ? 'Hide' : 'Show'}
                    </button>
                  </span>
                  {fieldError('wifi_password')}
                </label>
              </fieldset>
              <fieldset>
                <legend>ZKT terminal</legend>
                <label>
                  Communication key
                  <span className="secret-input">
                    <input
                      id="provision-communication_key"
                      type={showCommKey ? 'text' : 'password'}
                      inputMode="numeric"
                      autoComplete="off"
                      value={draft.communication_key}
                      onChange={(event) =>
                        updateDraft(
                          'communication_key',
                          event.target.value.replace(/\D/g, ''),
                        )
                      }
                      aria-invalid={Boolean(errors.communication_key)}
                      aria-describedby={
                        errors.communication_key
                          ? 'provision-communication_key-error'
                          : undefined
                      }
                    />
                    <button
                      type="button"
                      aria-pressed={showCommKey}
                      aria-label={`${showCommKey ? 'Hide' : 'Show'} communication key`}
                      onClick={() => setShowCommKey((value) => !value)}
                    >
                      {showCommKey ? 'Hide' : 'Show'}
                    </button>
                  </span>
                  {fieldError('communication_key')}
                </label>
                <label>
                  ZKT TCP port
                  <input
                    id="provision-zkt_port"
                    inputMode="numeric"
                    value={draft.zkt_port}
                    onChange={(event) =>
                      updateDraft(
                        'zkt_port',
                        event.target.value.replace(/\D/g, ''),
                      )
                    }
                    aria-invalid={Boolean(errors.zkt_port)}
                    aria-describedby={
                      errors.zkt_port ? 'provision-zkt_port-error' : undefined
                    }
                  />
                  {fieldError('zkt_port')}
                </label>
                <div className="ip-choice">
                  <label>
                    <input
                      type="radio"
                      checked={ipMode === 'automatic'}
                      onChange={() => {
                        setIpMode('automatic')
                        updateDraft('preferred_ip', '0.0.0.0')
                      }}
                    />{' '}
                    Automatic discovery <small>0.0.0.0 · recommended</small>
                  </label>
                  <label>
                    <input
                      type="radio"
                      checked={ipMode === 'fixed'}
                      onChange={() => setIpMode('fixed')}
                    />{' '}
                    Known private IPv4
                  </label>
                </div>
                {ipMode === 'fixed' && (
                  <label>
                    Preferred private IPv4
                    <input
                      id="provision-preferred_ip"
                      value={
                        draft.preferred_ip === '0.0.0.0'
                          ? ''
                          : draft.preferred_ip
                      }
                      onChange={(event) =>
                        updateDraft('preferred_ip', event.target.value)
                      }
                      aria-invalid={Boolean(errors.preferred_ip)}
                      aria-describedby={
                        errors.preferred_ip
                          ? 'provision-preferred_ip-error'
                          : undefined
                      }
                    />
                    {fieldError('preferred_ip')}
                  </label>
                )}
              </fieldset>
              <fieldset>
                <legend>Site identity</legend>
                <div className="form-grid">
                  <label>
                    Device ID
                    <input
                      id="provision-device_id"
                      value={draft.device_id}
                      onChange={(event) =>
                        updateDraft('device_id', event.target.value)
                      }
                      aria-invalid={Boolean(errors.device_id)}
                      aria-describedby={
                        errors.device_id
                          ? 'provision-device_id-error'
                          : undefined
                      }
                    />
                    {fieldError('device_id')}
                  </label>
                  <label>
                    Zone ID
                    <input
                      id="provision-zone_id"
                      list="provision-zone-options"
                      value={draft.zone_id}
                      onChange={(event) => {
                        const value = event.target.value
                        const match = zoneSuggestions.find(
                          (item) => item.zone_id === value,
                        )
                        setDraft((currentValue) => ({
                          ...currentValue,
                          zone_id: value,
                          zone_name: match?.zone_name || '',
                        }))
                      }}
                      aria-invalid={Boolean(errors.zone_id)}
                      aria-describedby={
                        errors.zone_id ? 'provision-zone_id-error' : undefined
                      }
                    />
                    <datalist id="provision-zone-options">
                      {zoneSuggestions.map((item) => (
                        <option key={item.zone_id} value={item.zone_id}>
                          {item.zone_name}
                        </option>
                      ))}
                    </datalist>
                    {fieldError('zone_id')}
                  </label>
                  <label className="wide">
                    Zone name
                    <input
                      id="provision-zone_name"
                      list="provision-zone-name-options"
                      value={draft.zone_name}
                      onChange={(event) =>
                        updateDraft('zone_name', event.target.value)
                      }
                      aria-invalid={Boolean(errors.zone_name)}
                      aria-describedby={
                        errors.zone_name
                          ? 'provision-zone_name-error'
                          : undefined
                      }
                    />
                    <datalist id="provision-zone-name-options">
                      {zoneSuggestions.map((item) => (
                        <option key={item.zone_id} value={item.zone_name}>
                          {item.zone_id}
                        </option>
                      ))}
                    </datalist>
                    <small>{utf8(draft.zone_name)}/120 bytes</small>
                    {fieldError('zone_name')}
                  </label>
                </div>
              </fieldset>
              <details>
                <summary>Managed defaults</summary>
                <p>
                  ADD supplies the signed onboarding endpoint, ORDS credentials,
                  encrypted-NVS and Secure Boot policy. ZKT recovery is
                  disabled. Secret values are never displayed here.
                </p>
              </details>
              <div className="provisioning-actions">
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy}
                  onClick={() => void cancel()}
                >
                  Cancel
                </button>
                <button className="button primary" disabled={busy}>
                  Review secure preparation
                </button>
              </div>
            </form>
          )}
          {current?.state === 'AWAITING_AUTHORIZATION' && (
            <form
              className="provisioning-card review-card"
              onSubmit={(event) => void authorize(event)}
            >
              <p className="eyebrow">STEP 4 · REVIEW AND AUTHORIZE</p>
              <h2 tabIndex={-1} ref={headingRef}>
                {irreversible
                  ? `Authorize security fuses for ${current.hardware_mac}`
                  : `Flash and provision ${current.hardware_mac}`}
              </h2>
              {irreversible && (
                <div className="irreversible-warning pattern-blocked">
                  <Icon name="alert" />
                  <div>
                    <strong>Irreversible security step</strong>
                    <p>
                      This one-time operation installs the device-bound HMAC
                      protection and signed-boot trust. It cannot be undone.
                    </p>
                  </div>
                </div>
              )}
              {currentAssignment &&
                (currentAssignment.zone_id !== current.zone_id ||
                  currentAssignment.device_id !== current.device_id) && (
                  <div className="info-copy pattern-waiting">
                    <Icon name="alert" />
                    <div>
                      <strong>Managed transfer</strong>
                      <p>
                        This MAC is currently assigned to{' '}
                        {currentAssignment.zone_name ||
                          currentAssignment.zone_id}{' '}
                        / {currentAssignment.device_id}. Authorization will move
                        it to the assignment below.
                      </p>
                    </div>
                  </div>
                )}
              <dl className="review-facts">
                <div>
                  <dt>Detected MAC</dt>
                  <dd>
                    <code>{current.hardware_mac}</code>
                  </dd>
                </div>
                <div>
                  <dt>Mode</dt>
                  <dd>{current.mode?.replaceAll('_', ' ')}</dd>
                </div>
                <div>
                  <dt>Firmware</dt>
                  <dd>Zone Lite {current.bundle?.version}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>
                    <code>{current.bundle?.git_sha}</code>
                  </dd>
                </div>
                <div>
                  <dt>Assignment</dt>
                  <dd>
                    {current.zone_id} / {current.device_id}
                  </dd>
                </div>
                <div>
                  <dt>Preferred IP</dt>
                  <dd>{current.preferred_ip}</dd>
                </div>
                <div>
                  <dt>Wi-Fi password</dt>
                  <dd>••••••••</dd>
                </div>
                <div>
                  <dt>Communication key</dt>
                  <dd>••••••••</dd>
                </div>
              </dl>
              {irreversible && (
                <>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={labelAcknowledged}
                      onChange={(event) =>
                        setLabelAcknowledged(event.target.checked)
                      }
                    />{' '}
                    I verified the MAC on the physical device label.
                  </label>
                  <label>
                    Type the exact detected MAC
                    <input
                      autoComplete="off"
                      value={typedMac}
                      onChange={(event) => setTypedMac(event.target.value)}
                      placeholder={current.hardware_mac || ''}
                    />
                  </label>
                </>
              )}
              <label>
                Administrator password
                <input
                  type="password"
                  autoComplete="current-password"
                  value={adminPassword}
                  onChange={(event) => setAdminPassword(event.target.value)}
                />
              </label>
              <div className="provisioning-actions">
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy}
                  onClick={() => void cancel()}
                >
                  Cancel
                </button>
                <button
                  className="button primary"
                  disabled={
                    busy ||
                    !adminPassword ||
                    Boolean(
                      irreversible &&
                        (!labelAcknowledged ||
                          typedMac.toLowerCase() !== current.hardware_mac),
                    )
                  }
                >
                  {irreversible
                    ? 'Authorize security fuses and flash'
                    : 'Flash and provision device'}
                </button>
              </div>
            </form>
          )}
          {current?.state === 'WAITING_FOR_TERMINAL_CONFIRMATION' && (
            <form
              className="provisioning-card terminal-confirmation"
              onSubmit={(event) => void confirmTerminal(event)}
            >
              <p className="eyebrow">AUTHENTICATED TERMINAL DISCOVERED</p>
              <h2 tabIndex={-1} ref={headingRef}>
                Confirm the physical ZKT terminal
              </h2>
              <p>
                The ESP32 remains read-only until you confirm and it persists
                this exact authenticated serial in encrypted NVS.
              </p>
              {current.terminal?.observed_serial ? (
                <dl className="review-facts">
                  <div>
                    <dt>Model</dt>
                    <dd>{current.terminal.model || 'Unknown model'}</dd>
                  </div>
                  <div>
                    <dt>Observed serial</dt>
                    <dd>
                      <code>{current.terminal.observed_serial}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>IP address</dt>
                    <dd>{current.terminal.ip_address || 'Not reported'}</dd>
                  </div>
                  <div>
                    <dt>Write policy</dt>
                    <dd>Read-only until confirmed</dd>
                  </div>
                </dl>
              ) : (
                <div className="info-copy pattern-waiting">
                  <Icon name="refresh" />
                  <div>
                    <strong>Waiting for authenticated ZKT discovery</strong>
                    <p>
                      ADD will show the observed model, serial and IP after the
                      device reaches its destination network.
                    </p>
                  </div>
                </div>
              )}
              {current.terminal?.observed_serial && (
                <>
                  <label>
                    Administrator password
                    <input
                      type="password"
                      autoComplete="current-password"
                      value={terminalPassword}
                      onChange={(event) =>
                        setTerminalPassword(event.target.value)
                      }
                    />
                  </label>
                  <button
                    className="button primary"
                    disabled={!terminalPassword || busy}
                  >
                    Confirm and pin serial {current.terminal.observed_serial}
                  </button>
                </>
              )}
            </form>
          )}
          {current &&
            step === 4 &&
            current.state !== 'WAITING_FOR_TERMINAL_CONFIRMATION' && (
              <section className="provisioning-card flashing-card">
                <p className="eyebrow">AUTOMATIC SECURE PREPARATION</p>
                <h2 tabIndex={-1} ref={headingRef}>
                  {current.state
                    .replaceAll('_', ' ')
                    .toLowerCase()
                    .replace(/^./, (value) => value.toUpperCase())}
                </h2>
                <p>Keep this window open and the USB cable connected.</p>
                <div
                  className="progress-track"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={current.progress}
                  aria-label="Provisioning progress"
                >
                  <span style={{ width: `${current.progress}%` }} />
                </div>
                <strong className="progress-value">{current.progress}%</strong>
                <p className="transfer-progress">
                  Elapsed {elapsed(elapsedSeconds)}
                  {latestTransfer && (
                    <>
                      {' '}
                      ·{' '}
                      {Math.round(
                        Number(latestTransfer.details.bytes_completed) / 1024,
                      ).toLocaleString()}{' '}
                      KiB of{' '}
                      {Math.round(
                        Number(latestTransfer.details.bytes_total) / 1024,
                      ).toLocaleString()}{' '}
                      KiB flash work complete
                    </>
                  )}
                </p>
                <ol className="event-timeline">
                  {(current.events || []).slice(-8).map((event) => (
                    <li
                      key={event.sequence}
                      className={
                        event.state === current.state ? 'active' : 'done'
                      }
                    >
                      <Icon
                        name={
                          event.state === current.state ? 'refresh' : 'check'
                        }
                      />
                      <span>
                        <strong>{event.state.replaceAll('_', ' ')}</strong>
                        <small>{dateTime(event.created_at)}</small>
                      </span>
                    </li>
                  ))}
                </ol>
                <details>
                  <summary>View redacted technical details</summary>
                  <pre>
                    {JSON.stringify(
                      (current.events || [])
                        .slice(-5)
                        .map(
                          ({ sequence, state, progress, source, details }) => ({
                            sequence,
                            state,
                            progress,
                            source,
                            transfer:
                              typeof details.bytes_completed === 'number'
                                ? {
                                    bytes_completed: details.bytes_completed,
                                    bytes_total: details.bytes_total,
                                  }
                                : undefined,
                          }),
                        ),
                      null,
                      2,
                    )}
                  </pre>
                </details>
              </section>
            )}
          {current && step === 5 && (
            <section
              className={`provisioning-card completion-card pattern-${current.state === 'VERIFIED_ONLINE' ? 'confirmed' : current.state === 'SITE_VALIDATION_PENDING' ? 'waiting' : 'blocked'}`}
            >
              <Icon
                name={
                  current.state === 'VERIFIED_ONLINE'
                    ? 'check'
                    : current.state === 'SITE_VALIDATION_PENDING'
                      ? 'clock'
                      : 'alert'
                }
              />
              <p className="eyebrow">PROVISIONING RECEIPT</p>
              <h2 tabIndex={-1} ref={headingRef}>
                {current.state === 'VERIFIED_ONLINE'
                  ? 'ESP32 online and verified'
                  : current.state === 'SITE_VALIDATION_PENDING'
                    ? 'Flash verified — site validation pending'
                    : current.state.replaceAll('_', ' ')}
              </h2>
              <p>
                {current.state === 'VERIFIED_ONLINE'
                  ? 'Signed onboarding, heartbeat, terminal serial binding, authentication and stability checks passed.'
                  : current.state === 'SITE_VALIDATION_PENDING'
                    ? 'The firmware and encrypted settings are verified. ADD will complete onboarding automatically when the destination network is available.'
                    : 'The device is not approved for deployment. Review the redacted evidence before retrying.'}
              </p>
              <div className="provisioning-actions">
                {current.state === 'VERIFIED_ONLINE' && (
                  <a
                    className="button primary"
                    href={`/fleet/${current.result.connector_id || ''}`}
                  >
                    View device
                  </a>
                )}
                <a
                  className="button secondary"
                  href={`/api/v1/provisioning/sessions/${current.session_id}/receipt`}
                  download
                >
                  Download redacted receipt
                </a>
                <button className="button secondary" onClick={beginAnother}>
                  Flash another ESP
                </button>
              </div>
            </section>
          )}
        </main>
        <aside className="provisioning-evidence">
          <p className="eyebrow">LIVE EVIDENCE</p>
          <h3>{current?.hardware_mac || 'No device selected'}</h3>
          <StatusBadge
            state={
              current?.hardware_classification ||
              (online ? 'COMPANION READY' : 'COMPANION OFFLINE')
            }
          />
          <dl>
            <div>
              <dt>Companion</dt>
              <dd>
                {online
                  ? `${online.platform} · ${online.application_version}`
                  : 'Not connected'}
              </dd>
            </div>
            <div>
              <dt>Approved release</dt>
              <dd>
                {current?.bundle?.version ||
                  capabilities?.latest_bundle?.version ||
                  'Unavailable'}
              </dd>
            </div>
            <div>
              <dt>Git SHA</dt>
              <dd>
                <code>
                  {current?.bundle?.git_sha ||
                    capabilities?.latest_bundle?.git_sha ||
                    '—'}
                </code>
              </dd>
            </div>
            <div>
              <dt>Manifest</dt>
              <dd>
                <code>
                  {current?.bundle?.manifest_sha256 ||
                    capabilities?.latest_bundle?.manifest_sha256 ||
                    '—'}
                </code>
              </dd>
            </div>
            <div>
              <dt>Session</dt>
              <dd>
                <code>{current?.session_id || '—'}</code>
              </dd>
            </div>
          </dl>
        </aside>
      </div>
      <section className="panel provisioning-history awaiting-terminal-confirmations">
        <div className="panel-header">
          <div>
            <h2>Awaiting terminal confirmation</h2>
            <p>
              Any ADD administrator can locate a device and confirm its authenticated ZKT serial with administrator password step-up.
            </p>
          </div>
          <StatusBadge state={`${awaitingTotal} WAITING`} />
        </div>
        <form className="provisioning-history-search" onSubmit={(event) => void searchAwaitingSessions(event)}>
          <label className="search-field">
            <span className="sr-only">Search awaiting sessions by device, zone, MAC, or session</span>
            <Icon name="search" />
            <input
              value={awaitingQuery}
              onChange={(event) => setAwaitingQuery(event.target.value)}
              placeholder="Search device, zone, MAC, or session"
            />
          </label>
          <button className="button secondary" type="submit" disabled={awaitingLoading}>
            {awaitingLoading ? 'Searching…' : 'Search'}
          </button>
        </form>
        {awaitingSessions.map((row) => (
          <button
            key={row.session_id}
            disabled={historyLoading || awaitingLoading}
            onClick={() => void openHistory(row.session_id)}
          >
            <span>
              <strong>{row.zone_id || row.device_id || row.hardware_mac || 'Awaiting device identity'}</strong>
              <small>
                {row.device_id || 'No device ID'} · {row.hardware_mac || 'MAC pending'} · Operator {row.operator}
              </small>
            </span>
            <StatusBadge state={row.state} />
          </button>
        ))}
        {!awaitingLoading && awaitingSessions.length === 0 && (
          <div className="empty-state compact">
            <Icon name="check" />
            <p>{awaitingQuery.trim() ? 'No awaiting terminal confirmations match this search.' : 'No terminals are awaiting serial confirmation.'}</p>
          </div>
        )}
      </section>
      {sessions.length > 0 && (
        <section className="panel provisioning-history">
          <div className="panel-header">
            <div>
              <h2>Provisioning history</h2>
              <p>
                {sessionTotal.toLocaleString()} durable redacted receipt
                {sessionTotal === 1 ? '' : 's'}; credentials are never retained.
              </p>
            </div>
            <StatusBadge state={`${sessions.length} LOADED`} />
          </div>
          {sessions.map((row) => (
            <button
              key={row.session_id}
              disabled={historyLoading}
              onClick={() => void openHistory(row.session_id)}
            >
              <span>
                <strong>{row.hardware_mac || 'Awaiting device'}</strong>
                <small>
                  {row.zone_id || 'No assignment'} · {dateTime(row.created_at)}{' '}
                  ·{' '}
                  {row.operator === username
                    ? 'Your session'
                    : `Operator ${row.operator}`}
                </small>
              </span>
              <StatusBadge state={row.state} />
            </button>
          ))}
          {sessionCursor && (
            <div className="load-more">
              <button
                className="button secondary"
                disabled={historyLoading}
                onClick={() => void loadOlderHistory()}
              >
                {historyLoading
                  ? 'Loading older sessions…'
                  : 'Load older sessions'}
              </button>
              <small>
                {sessions.length.toLocaleString()} of{' '}
                {sessionTotal.toLocaleString()} sessions loaded
              </small>
            </div>
          )}
        </section>
      )}
      {revokeCompanionId && (
        <Dialog
          titleId="revoke-provisioning-companion-title"
          title="Revoke provisioning companion"
          description="End this companion's ability to start or continue ADD provisioning work."
          onClose={() => {
            setRevokeCompanionId(null)
            setRevokeCompanionPassword('')
          }}
        >
          <div className="dialog-body">
            <div className="destructive-copy pattern-blocked">
              <Icon name="alert" />
              <div>
                <h3>Revoke {revokeCompanionId}</h3>
                <p>
                  The native application must be paired again before it can
                  operate USB through ADD.
                </p>
              </div>
            </div>
            <label>
              Administrator password
              <input
                type="password"
                autoComplete="current-password"
                value={revokeCompanionPassword}
                onChange={(event) =>
                  setRevokeCompanionPassword(event.target.value)
                }
              />
            </label>
            <div className="dialog-actions">
              <button
                className="button secondary"
                onClick={() => setRevokeCompanionId(null)}
              >
                Keep companion
              </button>
              <button
                className="button destructive"
                disabled={!revokeCompanionPassword || busy}
                onClick={() => void revokeCompanion()}
              >
                {busy ? 'Revoking…' : 'Revoke companion'}
              </button>
            </div>
          </div>
        </Dialog>
      )}
      {navigationBlocker.state === 'blocked' && (
        <Dialog
          titleId="provisioning-navigation-block-title"
          title="Secure write is still in progress"
          description="Leaving now hides live status while the authorized local safety operation continues."
          onClose={() => navigationBlocker.reset?.()}
        >
          <div className="dialog-body">
            <div className="info-copy pattern-blocked">
              <Icon name="alert" />
              <div>
                <h3>
                  {current
                    ? humanizeProvisioningState(current.state)
                    : 'Irreversible work active'}
                </h3>
                <p>
                  Keep this page open until flash and readback verification
                  reach a safe terminal state.
                </p>
              </div>
            </div>
            <div className="dialog-actions">
              <button
                className="button primary"
                onClick={() => navigationBlocker.reset?.()}
              >
                Stay with secure write
              </button>
              <button
                className="button destructive"
                onClick={() => navigationBlocker.proceed?.()}
              >
                Leave and continue locally
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  )
}
