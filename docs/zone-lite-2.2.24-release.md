# Zone Lite 2.2.24 corrective release

## Purpose

Zone Lite 2.2.24 makes the historical attendance cursor advance between
calendar months that actually exist in the terminal attendance table. Older
releases advanced one calendar month per reconciliation, including empty
months. A preserved old or abnormal terminal timestamp could therefore delay
the real history by hundreds of otherwise empty cycles.

The release does not discard, rewrite, or normalize any attendance timestamp.
The earliest plausible terminal month remains the reported coverage start, and
every non-empty month is still processed through the existing identity-complete,
fail-closed ADD-to-Oracle truth path. Empty months are skipped only after the
complete stable terminal dump proves that they contain zero attendance rows.

The historical-state schema is incremented so every updated device starts a
fresh sweep using the corrected cursor.

## Production acceptance gates

1. Publish the exact green main commit as `HIL_ONLY` for `ZONE-SWAT-01`.
2. Confirm the pre-update user and attendance counts are unchanged after boot.
3. Require `HISTORY_EMPTY_MONTHS_SKIPPED` only for ranges proven empty by the
   stable terminal dump.
4. Require every non-empty month from the reported coverage start through the
   current cursor to be delegated durably to ADD or remain explicitly blocked
   by the identity gate.
5. Require the SWAT historical backlog to be empty and every ADD event to be
   Oracle-confirmed before promoting the immutable package.
6. Do not update another zone while SWAT is blocked, degraded, reconnecting, or
   has any attendance row absent from ADD or Oracle.

No attendance data is deleted or modified by this release.
