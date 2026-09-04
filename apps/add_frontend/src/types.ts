export type DeviceState =
  | 'ONLINE'
  | 'OFFLINE'
  | 'DEGRADED'
  | 'FLAPPING'
  | 'ONBOARDING'
  | 'QUARANTINED_DUPLICATE_SERIAL'
  | string

export interface PaginatedResponse<T> {
  rows: T[]
  next_cursor: number | null
}

export type DashboardRoute =
  | 'fleet'
  | 'users'
  | 'attendance'
  | 'reconciliation'
  | 'firmware'
  | 'alerts'

export type FirmwareSection = 'overview' | 'prepare' | 'releases' | 'campaigns'
export type ReconciliationSection = 'jobs' | 'exceptions'
export type ReconciliationStatusGroup = '' | 'ACTIVE' | 'QUEUED_WAITING' | 'PAUSED' | 'ATTENTION' | 'COMPLETED' | 'CANCELLED'

export interface FirmwareScopePreview {
  scope_token: string
  expires_at: string
  release: { release_id: string; version: string; state: string }
  zone_id: string
  counts: { candidates: number; eligible: number; excluded: number; offline: number }
  eligible: Array<Pick<Device, 'connector_id' | 'display_name' | 'zone_id' | 'hardware_id' | 'connected' | 'firmware_version' | 'ota_state'>>
  excluded: Array<Pick<Device, 'connector_id' | 'display_name' | 'zone_id' | 'hardware_id' | 'connected' | 'firmware_version' | 'ota_state'> & { reason: string }>
}

export interface AlertQueueResponse {
  rows: Array<Alert & { device: Pick<Device, 'connector_id' | 'display_name' | 'zone_id' | 'hardware_id'> }>
  next_cursor: string | null
  totals: { all: number; open: number; acknowledged: number; resolved: number }
}

export interface AttendanceQuarantineItem {
  id: number
  receipt_id: string
  connector_id: string
  device_id: string
  display_name: string
  zone_id: string
  item_index: number
  error_code: string | null
  error_path: string | null
  payload_digest: string
  review_state: 'OPEN' | 'REVIEWED'
  reviewed_by: string | null
  review_reason: string | null
  reviewed_at: string | null
  observed_at: string
  evidence_available: boolean
  handling: 'QUARANTINED_NON_BLOCKING'
}

export interface AttendanceQuarantineResponse {
  totals: { all: number; open: number }
  filtered_total: number
  rows: AttendanceQuarantineItem[]
  next_cursor: number | null
}

export interface AttendanceQuarantineReveal {
  id: number
  payload_digest: string
  error_code: string | null
  error_path: string | null
  payload: unknown
}

export interface ZktDevice {
  id: number
  serial: string | null
  expected_serial: string | null
  confirmed_serial?: string | null
  terminal_binding_state?: string
  serial_confirmed_by?: string | null
  serial_confirmed_at?: string | null
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
  identity_snapshot_revision?: number
  identity_snapshot_state_hash?: string | null
  identity_snapshot_observed_at?: string | null
  identity_snapshot_received_at?: string | null
  identity_snapshot_stable?: boolean
  last_identity_change_at?: string | null
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
  comm_key_capable?: boolean
  comm_key_revision?: number
  ota_capable?: boolean
  ota_state?: 'LEGACY_MANUAL_UPDATE' | 'OTA_READY' | 'UPDATING' | 'ROLLBACK_REQUIRED' | 'OTA_BLOCKED'
  ota_partition_layout?: string | null
  ota_running_partition?: string | null
  ota_image_sha256?: string | null
  ota_signing_key_id?: string | null
  onboarding_generation: number
  last_onboarded_at: string | null
  last_seen_at: string | null
  current_activity: string | null
  last_error_code: string | null
  is_spare?: boolean
  zkt: ZktDevice | null
  active_command?: Command | null
  active_lease?: Lease | null
}

export interface CommKeyOperation {
  operation_id: string
  mode: 'ESP_ONLY' | 'ESP_AND_TERMINAL'
  requested_revision: number
  expected_terminal_serial: string
  status: string
  error_code: string | null
  created_at: string
  updated_at: string
  expires_at: string
  completed_at: string | null
}

export interface CommKeyState {
  enabled: boolean
  reveal_enabled: boolean
  management_state: string
  applied_revision: number
  desired_revision: number
  last_verified_at: string | null
  verified_terminal_serial: string | null
  last_error_code: string | null
  managed: boolean
  capabilities: {
    esp_only: boolean
    esp_and_terminal: boolean
    esp_and_terminal_block_reason: string | null
    recovery_staging: boolean
  }
  active_operation: CommKeyOperation | null
}

export interface CommKeyReveal {
  comm_key: string
  applied_revision: number
  verified_terminal_serial: string | null
  last_verified_at: string | null
}

export interface Overview {
  total: number
  spares?: number
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
    acknowledged_add: number
    acknowledged_check: number
    acknowledged_firmware: number
    firmware_unverified: number
    membership_reverify: number
    oldest_backlog_at: string | null
    last_attempt_at: string | null
  }
}

export interface ReconciliationPreflight {
  eligible: boolean
  ready_now: boolean
  hard_blockers: Array<{ code: string; message: string }>
  waitable_blockers: Array<{ code: string; message: string }>
  terminal: null | {
    serial: string | null
    model: string | null
    attendance_count: number | null
    user_count: number | null
    connection_state: string
    range_resume_verified: boolean
  }
  coverage: ReconciliationCoverage | null
}

export interface ReconciliationCoverage {
  coverage_id: string
  certified_source_cursor: number
  source_committed_cursor: number
  source_committed_chain_digest: string
  tail_exception_count: number
  tail_last_committed_at: string | null
  capture_state: string
  oracle_state: string
  active: boolean
  captured_at: string
  oracle_certified_at: string | null
  invalidated_reason: string | null
  source_ledger_count?: number
  source_ledger_complete?: boolean
  terminal_source_parity?: boolean
  chain_continuous?: boolean
}

export interface SourceExceptionReview {
  review_id: string
  state: 'REVIEWED'
  reason: string
  actor: string
  created_at: string
}

export interface SourceException {
  id: number
  connector_id: string | null
  device_id: string | null
  display_name: string | null
  zone_id: string | null
  terminal_serial: string
  terminal_generation: number
  ordinal: number
  source_kind: 'BASELINE' | 'TAIL'
  record_size: number | null
  disposition: 'INVALID_TIME' | 'MALFORMED'
  error_code: string | null
  raw_timestamp: number | null
  observed_uid: string | null
  observed_user_id: string | null
  raw_record_digest: string
  evidence_available: boolean
  terminal_record_key: string
  attendance_event_id: number | null
  observed_at: string
  review_state: 'OPEN' | 'REVIEWED'
  reviewed_at: string | null
  reviewed_by: string | null
  review_reason: string | null
  source_committed_cursor: number
  cursor_advanced: boolean
  oracle_action: 'EXCLUDED_FAIL_CLOSED'
  reviews?: SourceExceptionReview[]
}

export interface SourceExceptionTotals {
  all: number
  open: number
  reviewed: number
  invalid_time: number
  malformed: number
  affected_terminals: number
}

export interface SourceExceptionList {
  totals: SourceExceptionTotals
  filtered_total: number
  rows: SourceException[]
  next_cursor: number | null
  scope?: {
    job_id: string
    device_id: string | null
    terminal_serial: string | null
    cutoff_count: number | null
    source_exception_assurance: SourceExceptionAssurance
  }
}

export interface SourceExceptionAssurance {
  total: number
  reviewed: number
  open: number
  invalid_time: number
  malformed: number
  state: 'NONE' | 'REVIEW_REQUIRED' | 'REVIEWED_EXCLUSIONS' | 'SCOPE_MISMATCH'
  cohort_digest: string | null
}

export interface SourceExceptionReveal {
  id: number
  raw_record_b64: string
  raw_record_hex: string
  raw_record_digest: string
  record_size: number | null
}

export interface ReconciliationDivergenceDetail {
  divergence_id: string
  job_id: string | null
  ordinal: number
  state: string
  old_raw_digest: string
  new_raw_digest: string
  old_disposition: string | null
  new_disposition: string | null
  observations: Array<{
    raw_record_digest: string
    disposition: string
    observed_at: string
    kind: string
  }>
  evidence_available: boolean
  created_at: string
  resolved_at: string | null
}

export interface ReconciliationDivergenceReveal {
  divergence_id: string
  raw_record_b64: string
  raw_record_hex: string
  new_raw_digest: string
}

export interface ReconciliationJob {
  job_id: string
  mode: string
  status: string
  phase: string
  wait_reason: string | null
  error_code: string | null
  error_message: string | null
  operator_state?: string
  operator_message?: string
  completion_outcome?: string | null
  review_required?: boolean
  source_exception_assurance: SourceExceptionAssurance
  connector: null | {
    connector_id: string
    device_id: string
    display_name: string
    zone_id: string
    connected: boolean
  }
  terminal: {
    serial: string | null
    generation: number
    cutoff_count: number | null
    latest_count: number | null
    record_size: number | null
    source_total_bytes: number | null
  }
  progress: {
    scanned: number
    remaining: number | null
    add_durable: number
    already_present: number
    terminal_duplicates: number
    blocked_identity: number
    quarantined: number
    oracle_target: number
    oracle_confirmed: number
    oracle_pending: number
    oracle_review_required?: number
    retry_count: number
    auto_retry_count?: number
  }
  checkpoint?: {
    next_ordinal: number
    chain_digest: string | null
    last_progress_at: string | null
  }
  assignment: {
    assignment_id: string | null
    credit_start_ordinal: number | null
    credit_end_ordinal: number | null
    credit_committed_through: number | null
    granted_at: string | null
    expires_at: string | null
    accepted_at: string | null
    heartbeat_at: string | null
  }
  eta: {
    low_seconds: number | null
    high_seconds: number | null
    confidence: string
    unavailable_reason: string | null
  }
  recovery?: {
    operation_id: string
    source_epoch: number
    source_epoch_id: string | null
    divergence: null | {
      divergence_id: string
      ordinal: number
      state: string
      old_raw_digest: string
      new_raw_digest: string
      observation_count: number
      next_probe_at: string | null
    }
  }
  capture_certificate?: Record<string, unknown> | null
  oracle_certificate?: Record<string, unknown> | null
  events?: Array<{
    state: string
    details: Record<string, unknown>
    created_at: string
  }>
  requested_at: string
  started_at: string | null
  capture_certified_at: string | null
  oracle_certified_at: string | null
  completed_at: string | null
  updated_at: string
}

export interface ReconciliationScheduler {
  policy: 'BOUNDED_PARALLEL_PER_DEVICE' | string
  device_concurrency: number
  active_scan_jobs: number
  waiting_scan_jobs: number
  available_scan_slots: number
  history_backlog?: number
  history_backlog_limit?: number
  reserved_credit?: number
  available_credit?: number
}

export interface ReconciliationJobTotals {
  all: number
  active: number
  queued_waiting: number
  paused: number
  attention: number
  completed: number
  cancelled: number
}

export interface ReconciliationListResponse {
  enabled: boolean
  scheduler: ReconciliationScheduler
  rows: ReconciliationJob[]
  next_cursor: number | null
  filtered_total: number
  totals: ReconciliationJobTotals
}

export interface FirmwareRelease {
  release_id: string
  version: string
  git_sha: string
  image_sha256: string
  application_sha256: string | null
  image_size: number
  state: 'AVAILABLE' | 'HIL_ONLY' | 'REVOKED' | string
  partition_layout: string
  signing_key_id: string
  published_at: string
  revoked_at?: string | null
  revoked_by?: string | null
  hil_target_mac: string | null
}

export interface FirmwareCampaign {
  campaign_id: string
  release_id: string | null
  release_state: string | null
  zone_id: string
  zone_name: string | null
  version: string | null
  status: 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'CANCELLED' | string
  eligible: number
  legacy_skipped: number
  counts: Record<string, number>
  pause_reason: string | null
  actor: string
  reason: string
  deployments: FirmwareDeployment[]
  created_at: string
  updated_at: string
}

export interface FirmwareDeploymentEvent {
  state: string
  details: {
    bytes_written?: number
    error_code?: string | null
    [key: string]: unknown
  }
  created_at: string
}

export interface FirmwareTransportDiagnostics {
  download_grants: {
    issued_count: number
    reached_count: number
    endpoint_reached: boolean
    first_issued_at: string | null
    latest_issued_at: string | null
    latest_expires_at: string | null
    last_reached_at: string | null
  }
  telemetry: {
    window_started_at: string
    window_ended_at: string
    sample_count: number
    minimum_free_heap: number | null
    weakest_rssi: number | null
    latest: {
      free_heap: number | null
      rssi: number | null
      uptime_seconds: number | null
      outbox_depth: number
      current_activity: string | null
      created_at: string
    } | null
  }
}

export interface FirmwareDeployment {
  deployment_id: string
  connector_id: string | null
  display_name: string | null
  hardware_id: string | null
  zone_id: string | null
  status: string
  previous_version: string | null
  target_version: string
  bytes_written: number
  attempt_count: number
  error_code: string | null
  error_message: string | null
  offered_at: string | null
  completed_at: string | null
  updated_at: string
  transport_diagnostics: FirmwareTransportDiagnostics | null
  events: FirmwareDeploymentEvent[]
}

export interface FirmwareReleaseTotals {
  all: number
  available: number
  hil_only: number
  revoked: number
  [state: string]: number
}

export interface FirmwareCampaignTotals {
  campaigns: Record<string, number>
  deployments: Record<string, number>
}

export interface FirmwareListResponse<T, TTotals> {
  rows: T[]
  enabled: boolean
  hil_enabled: boolean
  next_cursor: number | null
  filtered_total: number
  totals: TTotals
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

export interface AttendanceRepairCohortCandidate {
  cohort_token: string
  evidence_classification: 'CURRENT_USER_LINEAGE' | 'EXACT_TOMBSTONE' | string
  selectable: boolean
  exclusion_code: string | null
  source_device_user_key: string
  source_uid: string | null
  source_user_id: string | null
  first_event_at: string
  last_event_at: string
  event_count: number
  membership_digest: string
  masked_identity: {
    variants: Array<{
      display_name_masked: string | null
      cnic_masked: string | null
    }>
    variant_count: number
    truncated: boolean
  }
  source_evidence: {
    terminal_manifest_events: number
    exact_tombstone: boolean
    source_types: string[]
  }
}

export interface AttendanceRepairCandidateTarget {
  user_key: string
  row_version: number
  display_name: string
  cnic_masked: string | null
  eligible: boolean
  exclusion_code: string | null
  cohorts: AttendanceRepairCohortCandidate[]
}

export interface AttendanceRepairCandidates {
  connector_id: string
  device_id: string
  source_current: boolean
  source_certificate: Record<string, unknown>
  date_scope: {
    timezone: 'Asia/Karachi'
    start_utc: string | null
    end_utc_exclusive: string | null
  }
  targets: AttendanceRepairCandidateTarget[]
}

export interface AttendanceRepairPreflight {
  preview_enabled: boolean
  execution_enabled: boolean
  eligible: boolean
  ready_now: boolean
  requires_source_reconciliation: boolean
  hard_blockers: Array<{ code: string; message: string }>
  waitable_blockers: Array<{ code: string; message: string }>
  limits: { employees: number; events: number; oracle_batch: number }
  source_certificate: Record<string, unknown>
  terminal: {
    serial: string | null
    snapshot_complete: boolean
    snapshot_stable: boolean
    snapshot_revision: number
    attendance_count: number | null
  } | null
  oracle: {
    available: boolean
    capabilities?: Record<string, unknown>
    error_code?: string
    message?: string
  }
  worker: {
    active_jobs: number
    review_items: number
    stale_leases: number
    oldest_job_age_seconds: number
    retrying_items?: number
    unknown_outcome_items?: number
    waiting_downstream_items?: number
    oldest_downstream_lag_seconds?: number
    leased_oracle_slots?: number
    active_worker_count?: number
    heartbeat?: {
      state: string
      updated_at: string
      last_started_at: string | null
      last_completed_at: string | null
      last_error_at: string | null
      last_error_code: string | null
    } | null
    release_v2?: {
      queue_oldest_age_seconds: number
      preparing_oldest_age_seconds: number
      awaiting_approval_oldest_age_seconds: number
      execution_oldest_age_seconds: number
      exclusions_by_code: Record<string, number>
      retry_exhausted_jobs: number
      reuse_attribution_failures: Record<string, number>
    }
  }
}

export interface AttendanceRepairTargetOutcome {
  user_key: string
  display_name: string
  cnic_masked: string | null
  expected_row_version: number
  desired_identity_digest: string
  status: string
  event_count: number
  completed_event_count: number
  attention_event_count: number
}

export interface AttendanceRepairItemOutcome {
  event_uid: string
  user_key: string | null
  event_time: string | null
  punch?: string | null
  capture_source?: string | null
  source_ords_status?: string | null
  risk_class?: string
  selection_origin?: string
  state: string
  oracle_classification: string
  outcome: string | null
  attempt_count: number
  oracle_attempt_count: number
  downstream_attempt_count: number
  next_attempt_at: string | null
  error_code: string | null
  error_message?: string | null
  operation_id?: string
  oracle_receipt_id?: string | null
  oracle_verified_at?: string | null
  downstream_status?: string | null
  downstream_verified_at?: string | null
  effective_identity_activated_at?: string | null
}

export interface AttendanceRepairJob {
  job_id: string
  connector_id: string
  device_id: string
  actor: string
  status: string
  release_state?: string
  phase: string
  workflow_version?: string
  selection_mode?: string
  selection_manifest_digest?: string | null
  selection_filters?: {
    date_from: string | null
    date_to: string | null
    hold_statuses: string[]
    punch: string | null
    source: string | null
  } | null
  selection_exclusion_manifest_digest?: string | null
  candidate_membership_digest?: string | null
  release_target_user_id?: string | null
  date_scope: {
    timezone: 'Asia/Karachi'
    start_utc: string | null
    end_utc_exclusive: string | null
  }
  request_digest: string
  preview_digest: string | null
  preview_expires_at: string | null
  source_dependency_job_id: string | null
  totals: {
    employees: number
    events: number
    selected?: number
    safe?: number
    ordinary?: number
    reuse?: number
    safe_reuse?: number
    operator_excluded?: number
    excluded: number
    completed_employees: number
    completed_events: number
    attention_events: number
  }
  wait_reason: string | null
  error_code: string | null
  error_message: string | null
  cancellation_requested: boolean
  preparation_attempt_count: number
  next_attempt_at: string | null
  created_at: string
  approved_at: string | null
  started_at: string | null
  completed_at: string | null
  reason?: string | null
  typed_confirmation?: string
  reuse_attestation?: {
    attestation_id: string
    evidence_type: string
    event_count: number
    event_membership_digest: string
    actor: string
    created_at: string
  } | null
  downstream_impact?: {
    timezone: 'Asia/Karachi'
    calendar_days: number
    employee_days: number
    before_identity_day_groups: number
    desired_identity_day_groups: number
    first_date: string | null
    last_date: string | null
  }
  targets: AttendanceRepairTargetOutcome[]
  items?: AttendanceRepairItemOutcome[]
  items_next_cursor?: number | null
}

export interface AttendanceRepairListResponse {
  preview_enabled: boolean
  execution_enabled: boolean
  rows: AttendanceRepairJob[]
  next_cursor: number | null
  totals: { all: number; active: number; attention: number }
  worker: AttendanceRepairPreflight['worker']
}

export interface AttendanceReleaseQueueRow {
  connector_id: string | null
  device_id: string | null
  device_name: string
  user_key: string | null
  row_version: number | null
  display_name: string
  user_id: string
  uid: string | null
  cnic_masked: string | null
  eligible: boolean
  lock_reason: string | null
  lock_reasons: string[]
  source_current: boolean
  active_release_job_id: string | null
  counts: {
    ordinary_blocked: number
    identity_reuse: number
    eligible: number
    locked: number
    in_progress: number
  }
  first_event_at: string
  last_event_at: string
}

export interface AttendanceReleaseQueueResponse {
  preview_enabled: boolean
  execution_enabled: boolean
  totals: {
    employees: number
    events: number
    eligible: number
    locked: number
  }
  rows: AttendanceReleaseQueueRow[]
  next_cursor: number | null
}

export interface AttendanceReleaseCandidate {
  event_token: string | null
  event_uid: string
  device_event_time: string
  punch: string | null
  status: string | null
  source: string
  device_serial: string | null
  uid: string | null
  user_id: string | null
  display_name: string | null
  clock_quality: string
  source_ords_status: 'BLOCKED_IDENTITY' | 'QUARANTINED_IDENTITY_REUSE'
  risk_class: 'ORDINARY_BLOCKED' | 'IDENTITY_REUSE'
  evidence_classification: string | null
  eligible: boolean
  lock_reason: string | null
}

export interface AttendanceReleaseCandidates {
  candidate_set_token: string
  expires_at: string
  source_current: boolean
  source_certificate: Record<string, unknown>
  target: {
    user_key: string
    row_version: number
    display_name: string
    user_id: string
    uid: string | null
    cnic_masked: string | null
    eligible: boolean
    lock_reason: string | null
  }
  filters: {
    date_from: string | null
    date_to: string | null
    hold_statuses: string[]
    punch: string | null
    source: string | null
  }
  totals: {
    all: number
    eligible: number
    locked: number
    ordinary_blocked: number
    identity_reuse: number
  }
  rows: AttendanceReleaseCandidate[]
  next_cursor: number | null
}

export interface UserDeletionJobItem {
  user_key: string
  uid: string
  user_id: string
  display_name: string
  status: string
  error_code: string | null
  error_message: string | null
  result: Record<string, unknown>
}

export interface UserDeletionJob {
  job_id: string
  connector_id: string
  status: string
  reason: string
  counts: {
    requested: number
    succeeded: number
    failed: number
    canceled: number
    expired: number
    pending: number
  }
  created_at: string
  expires_at: string
  started_at: string | null
  completed_at: string | null
  items: UserDeletionJobItem[]
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

export interface HistoricalIdentityCandidate {
  source_user_key: string | null
  source_kind?: 'DELETED_USER' | 'EVENT_GROUP'
  group_token?: string
  active_user_key?: string | null
  active_user_row_version?: number | null
  uid: string
  user_id: string
  display_name: string
  row_version: number | null
  observed_at: string | null
  deleted_at: string | null
  cnic_available: boolean
  identity_conflict_code: string | null
  event_count: number
  blocked_count: number
  quarantined_count: number
  first_event_at: string | null
  last_event_at: string | null
  resolution_path:
    | 'HR_DIRECTORY_EVIDENCE'
    | 'HR_DIRECTORY_EVENT_GROUP'
    | 'ACTIVE_USER_ENRICHMENT'
    | 'CURRENT_IDENTITY_EVIDENCE'
    | 'VERIFIED_TOMBSTONE_REPAIR'
    | 'IDENTITY_CONFLICT_REVIEW'
    | 'IDENTITY_REUSE_REVIEW'
  operator_actionable: boolean
}

export interface HistoricalIdentityReport {
  device_serial: string | null
  snapshot_revision: number
  totals: {
    unresolved_events: number
    blocked_identity: number
    quarantined_identity_reuse: number
    attributed_to_deleted_users: number
    unassigned_events: number
    actionable_event_groups?: number
    candidate_users: number
  }
  rows: HistoricalIdentityCandidate[]
  unassigned_groups?: HistoricalIdentityCandidate[]
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
  oracle_confirmed_at: string | null
  oracle_confirmation_path: string | null
  identity_resolution_id: number | null
  identity_resolution_status?: string
  identity_snapshot_id?: number | null
  identity_repaired_at?: string | null
  identity_content_status?: string
  identity_content_confirmed_at?: string | null
  identity_downstream_confirmed_at?: string | null
  release_state?: string
  release_state_label?: string
  effective_identity_confirmed_at?: string | null
  effective_identity_downstream_confirmed_at?: string | null
  active_release_job_id?: string | null
  latest_release_job_id?: string | null
  release_target_user_key?: string | null
  release_connector_id?: string | null
  release_lock_reason?: string | null
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
