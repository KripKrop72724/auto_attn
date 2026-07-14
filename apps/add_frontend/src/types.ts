export type DeviceState =
  | 'ONLINE'
  | 'OFFLINE'
  | 'DEGRADED'
  | 'FLAPPING'
  | 'ONBOARDING'
  | 'QUARANTINED_DUPLICATE_SERIAL'
  | string

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
  certification_observations: number
  capabilities: Record<string, boolean | number | string>
  snapshot_complete: boolean
  writes_disabled_reason: string | null
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
  onboarding_generation: number
  last_onboarded_at: string | null
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
  flapping?: number
  onboarding?: number
  quarantined_duplicate_serial?: number
  open_alerts: number
  active_leases: number
  ords_delivery?: {
    backlog: number
    pending: number
    retrying: number
    in_flight: number
    blocked_identity: number
    quarantined: number
    acknowledged: number
    oldest_backlog_at: string | null
    last_attempt_at: string | null
  }
}

export interface DeviceUser {
  id: number
  user_key: string
  uid: string
  user_id: string
  display_name: string
  cnic_masked: string | null
  cnic_available: boolean
  identity_complete: boolean
  identity_conflict_code: string | null
  identity_conflict_members: Array<{ user_id: string; uid: string }>
  identity_conflict_resolved: boolean
  identity_resolution_id: string | null
  shift_worker: boolean
  privilege: 0 | 14
  present: boolean
  lifecycle_state: string
  row_version: number
  observed_at: string
  machine_name_preview: string | null
  current_command_state: string | null
  read_only: boolean
}

export interface IdentityIntegrity {
  source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT' | 'PARTIAL_ZKT_SNAPSHOT'
  total_users: number
  with_cnic: number
  missing_cnic: number
  duplicate_groups: number
  duplicate_users: number
  resolved_duplicate_groups: number
  unresolved_duplicate_groups: number
  unresolved_duplicate_users: number
}

export interface IdentityConflictPunchEvidence {
  captured_count: number
  first_captured_at: string | null
  last_captured_at: string | null
  blocked_identity_count: number
}

export interface IdentityConflictMember {
  user_key: string
  uid: string
  user_id: string
  display_name: string
  row_version: number
  privilege: number
  observed_at: string
  punch_evidence: IdentityConflictPunchEvidence
}

export interface IdentityConflictGroup {
  group_token: string
  cnic_masked: string | null
  classification: 'EXACT_NAME_MATCH' | 'POSSIBLE_NAME_VARIANT' | 'MIXED_NAMES_HIGH_RISK'
  status: 'UNRESOLVED' | 'RESOLVED_SAME_EMPLOYEE'
  resolution_id: string | null
  resolution_created_at: string | null
  resolution_reason: string | null
  recommended_action: 'CONFIRM_SAME_EMPLOYEE' | 'HR_IDENTITY_REVIEW'
  members: IdentityConflictMember[]
}

export interface IdentityConflictReport {
  evidence_scope: {
    snapshot_source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT' | 'PARTIAL_ZKT_SNAPSHOT'
    terminal_attendance_count: number
    add_attendance_count: number
    attendance_coverage_percent: number | null
    attendance_is_immutable: boolean
    terminal_users_are_unchanged: boolean
  }
  raw_duplicate_groups: number
  resolved_groups: number
  unresolved_groups: number
  groups: IdentityConflictGroup[]
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
  identity_resolution_id: number | null
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
  dispatched_at?: string | null
  acknowledged_at?: string | null
  started_at?: string | null
  completed_at?: string | null
  result?: Record<string, unknown>
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

export interface UserCommandResponse {
  user: DeviceUser
  command: Command
}
