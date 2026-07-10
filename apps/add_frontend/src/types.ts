export type DeviceState = 'ONLINE' | 'OFFLINE' | 'DEGRADED' | 'ONBOARDING' | string

export interface ZktDevice {
  id: number
  serial: string | null
  expected_serial: string | null
  ip_address: string | null
  model: string | null
  platform: string | null
  online: boolean
  connection_state: string
  consecutive_failures: number
  consecutive_successes: number
  flap_count_15m: number
  last_transition_at: string | null
  last_online_at: string | null
  offline_since: string | null
  stability_since: string | null
  backoff_until: string | null
  probe_latency_ms: number | null
  certification_state: string
  capabilities: Record<string, boolean | number | string>
  user_count: number | null
  attendance_count: number | null
  device_time: string | null
  device_time_sampled_at: string | null
  drift_seconds: number | null
  last_reconcile_at: string | null
  next_restart_at: string | null
}

export interface Device {
  connector_id: string
  hardware_id: string
  zone_id: string
  zone_name: string
  device_id: string
  display_name: string
  state: DeviceState
  connected: boolean
  firmware_version: string | null
  last_seen_at: string | null
  current_activity: string | null
  last_error_code: string | null
  zkt: ZktDevice | null
  active_command?: Command | null
  active_lease?: Lease | null
}

export interface Overview {
  total: number
  online?: number
  offline?: number
  degraded?: number
  onboarding?: number
  open_alerts: number
  active_leases: number
}

export interface DeviceUser {
  id: number
  uid: string
  user_id: string
  raw_name: string
  display_name: string
  cnic_masked: string | null
  cnic_available: boolean
  shift_worker: boolean
  privilege: number
  present: boolean
  row_version: number
  observed_at: string
}

export interface AttendanceEvent {
  id: number
  event_uid: string
  device_serial: string | null
  uid: string | null
  user_id: string
  display_name: string | null
  cnic_masked: string | null
  device_event_time: string
  captured_at: string
  received_at: string
  source: string
  status: string | null
  punch: string | null
  clock_quality: string
  clock_drift_seconds: number | null
  ords_status: string
}

export interface DeviceLog {
  id: number
  boot_id: string
  sequence: number
  level: string
  subsystem: string
  code: string | null
  message: string
  context: Record<string, unknown>
  device_time: string | null
  received_at: string
}

export interface Alert {
  id: number
  code: string
  severity: string
  state: string
  message: string
  details: Record<string, unknown>
  first_seen_at: string
  last_seen_at: string
  acknowledged_at: string | null
  resolved_at: string | null
}

export interface Lease {
  lease_id: string
  state: string
  requested_at: string
  granted_at: string | null
  expires_at: string | null
  revoked_at: string | null
  last_error: string | null
}

export interface Command {
  command_id: string
  type: string
  status: string
  created_at: string
  expires_at: string | null
  error_code?: string | null
  error_message?: string | null
}

export interface ConnectionEvent {
  id: number
  from_state: string | null
  to_state: string
  reason: string | null
  consecutive_failures: number
  consecutive_successes: number
  flap_count_15m: number
  observed_at: string
}
