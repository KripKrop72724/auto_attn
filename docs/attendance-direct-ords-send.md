# Administrator Oracle send from Attendance

The **Attendance → All events** ledger supports multi-select Oracle status and CNIC-availability filters, a selectable load size, and single or bulk **Send to ORDS**. Filters combine. The page never changes captured punch facts.

For one punch, use **Send to ORDS** in its row. For a batch, check punches in the list (or **Select loaded punches**) and choose **Send selected punches**. One batch contains at most 500 saved punches. The review dialog shows the selection and requires one reason and an administrator password. Approval creates one durable, audited run; it is not an Oracle acknowledgement.

ADD excludes a punch when its current synced terminal user is unknown or that user has no usable CNIC. It also refuses, at approval and again before dispatch, any punch whose terminal clock was `INVALID` or whose time is impossible (before 2010, or more than a day after it was captured, received or now). An approval saved earlier for such a punch is quarantined as `QUARANTINED_INVALID_DEVICE_TIME`, never sent.

If the saved punch has a usable CNIC, the approved Oracle payload uses it. Otherwise ADD compares the CNIC its retained identity history held for that terminal user ID at the punch time with the current CNIC:

- **They match:** the current CNIC and name are used.
- **They differ** (for example a CNIC edited on the terminal after the punch): the dialog shows both masked CNICs and the administrator must choose. The punch-time CNIC is selected by default and is offered only when exactly one retained interval names it.
- **No record, or several/revoked records:** the current CNIC is used only when the administrator ticks **Use today's CNIC**, after HR confirmed the same employee held that user ID at the punch time.

A request that leaves a choice unmade (for example from an older dashboard) skips that punch; ADD never picks the current CNIC by user ID alone. A conflict between a saved CNIC and the current user does not itself prevent this explicit override.

Punches that ADD's own identity proof (and the installed database guard) would release at the next user snapshot, or that arrived after the latest snapshot and are re-checked at the next one, are listed separately. They are skipped unless the administrator ticks **Send now anyway**; the decision records that override. The dialog loads these read-only checks from `POST /api/v2/attendance-direct-ords/preview` before approval is possible.

For a single held punch, `GET /api/v2/attendance/{event_id}/identity-evidence` explains the same facts: the read-only database-guard result, the punch-time and current CNIC (masked), whether a saved terminal record exists yet, and whether an automatic release is expected at the next snapshot. Already Oracle-confirmed punches and punches owned by another delivery are also accounted for without duplicate submission. For the other selected punches, this action overrides ADD's normal delivery holds but preserves the original event UID, time, punch facts and raw evidence. It records the administrator's explicit decision; using the current user does not create historical identity proof or change Oracle data already present. The approved payload and the current-user identity are frozen and rechecked before dispatch; a changed user or CNIC stops the send.

The saved run shows preparing, waiting, confirmed, skipped and attention-needed counts. The run link survives a browser or server restart. A **Forced** pill appears only after a per-punch decision is committed; it opens the administrator reason and audit reference. `ACKED_CHECK` appears only after Oracle's protected content check confirms matching identity and source facts. A successful HTTP response alone is not completion. Rejections and mismatches remain visible for review. If a reply is lost, ADD verifies before retrying; it records send intent first so an uncertain response cannot trigger an uncontrolled repeat.

The existing **Needs review → Force release attendance** workflow remains separate: it checks a fresh terminal user list before approving historical identity use. This All events action is an explicit, broader delivery override for individually selected saved punches.

## Terminal names with a CNIC typo

A terminal name such as `A.Saleem-A4230111028815` (a letter before the 13 digits) or `Ali Khan-42301-1102881-5` (dashes) does not give ADD a CNIC, so every punch by that user stays held. ADD Users marks such a user, the integrity summary counts them, and the terminal raises a `USER_CNIC_FORMAT_SUSPECT` alert (user IDs only). ADD never uses the suspect digits. Confirm the CNIC with HR, then set it in **ADD Users → Edit**: the dialog proposes the name without the suffix, and the server strips it too, so the 24-byte terminal name becomes `A.Saleem-<CNIC>` rather than `A.Saleem-A-<CNIC>`. The alert clears at the next user snapshot. Punches held before the CNIC was set have no punch-time identity record and still need an explicit send with **Use today's CNIC**.
