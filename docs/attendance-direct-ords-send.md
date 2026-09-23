# Administrator Oracle send from Attendance

The **Attendance → All events** ledger supports multi-select Oracle status and CNIC-availability filters, a selectable load size, and single or bulk **Send to ORDS**. Filters combine. The page never changes captured punch facts.

For one punch, use **Send to ORDS** in its row. For a batch, check punches in the list (or **Select loaded punches**) and choose **Send selected punches**. One batch contains at most 500 saved punches. The review dialog shows the selection and requires one reason and an administrator password. Approval creates one durable, audited run; it is not an Oracle acknowledgement.

ADD excludes a punch when its current terminal user is unknown or a CNIC is missing, unreadable or malformed in either the saved punch or the current user record. A CNIC conflict does not itself prevent this explicit override. Already Oracle-confirmed punches and punches owned by another delivery are also accounted for without duplicate submission. For the other selected punches, this action overrides ADD's normal delivery holds but preserves the original event UID, time, punch facts and saved CNIC. It records the administrator's explicit decision. It does not create historical identity evidence or change Oracle data already present.

The saved run shows preparing, waiting, confirmed, skipped and attention-needed counts. The run link survives a browser or server restart. A **Forced** pill appears only after a per-punch decision is committed; it opens the administrator reason and audit reference. `ACKED_CHECK` appears only after Oracle's protected content check confirms matching identity and source facts. A successful HTTP response alone is not completion. Rejections and mismatches remain visible for review. If a reply is lost, ADD verifies before retrying; it records send intent first so an uncertain response cannot trigger an uncontrolled repeat.

The existing **Needs review → Force release attendance** workflow remains separate: it checks a fresh terminal user list before approving historical identity use. This All events action is an explicit, broader delivery override for individually selected saved punches.
