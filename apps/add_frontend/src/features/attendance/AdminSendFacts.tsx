// "Sent by an admin" in the check-in sheet (replaces the old "Forced" pill).
// Its own module so the Check-ins page does not load the Fix tools code.
import { TechnicalDetails } from '../../ui'
import type { AttendanceEvent } from '../../types'
import { longTime } from './attendanceWords'

/** The sheet block for a check-in an admin sent (replaces the old "Forced" pill). */
export function AdminSendFacts({ evidence }: { evidence: NonNullable<AttendanceEvent['force_release']> }) {
  const direct = evidence.policy === 'manual-direct-ords-v1'
  const href = direct ? `/attendance?direct_run=${encodeURIComponent(evidence.run_id)}` : `/attendance?view=fix-tools&tool=held-staff&force_run=${encodeURIComponent(evidence.run_id)}`
  return <section className="attendance-sheet-block" aria-labelledby={`admin-send-${evidence.run_id}`}>
    <h3 id={`admin-send-${evidence.run_id}`}>Sent by an admin</h3>
    <p>Sent by <span data-user-text>{evidence.administrator}</span> on {longTime(evidence.approved_at)}</p>
    {evidence.reason && <p>Reason: <span data-user-text>{evidence.reason}</span></p>}
    <a href={href}>Open the sending task</a>
    <TechnicalDetails summary="Technical details">
      <dl className="attendance-tech-list">
        {evidence.audit_id != null && <div><dt>Record no.</dt><dd>{evidence.audit_id}</dd></div>}
        <div><dt>Task</dt><dd>{evidence.run_id}</dd></div>
        {evidence.policy && <div><dt>Policy</dt><dd>{evidence.policy}</dd></div>}
        {evidence.snapshot_id != null && <div><dt>Saved people list</dt><dd>{evidence.snapshot_id}</dd></div>}
        {evidence.sync_command_id != null && <div><dt>Machine request</dt><dd>{evidence.sync_command_id}</dd></div>}
      </dl>
    </TechnicalDetails>
  </section>
}
