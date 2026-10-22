"""Durable, explicitly approved delivery of selected saved punches to Oracle.

This operator action overrides ADD's delivery holds. It never invents a user,
CNIC or source fact, and the ordinary Oracle content check still decides ACKED.
It refuses impossible punch times. When no CNIC was saved with the punch, it
reads what ADD retained about who held the terminal user ID at the punch time
(see ``attendance_release_safety``): another CNIC or person there refuses the
send, one other CNIC asks the administrator which to use, and today's CNIC is
used without asking only when the punch-time records show nothing else.
Punches ADD's own proof is about to release need an explicit "send now" too.

Every CNIC choice echoes the digest of the evidence the administrator saw in
the read-only preview; a choice made against evidence that has since changed
is skipped, never applied to the new evidence. Sending a punch-time CNIC that
another active terminal user holds today needs its own explicit confirmation.

A direct approval that stopped (its evidence changed) hands the punch back to
ADD's own release only if nothing under it can be in Oracle with another
CNIC: ordinary delivery treats Oracle's "already stored" reply as success
without a content check, so a punch whose stopped approval may already have
POSTed a different CNIC is held for review instead (PREVIOUS_POST_UNCERTAIN).
"""

from dataclasses import dataclass, replace
from uuid import uuid4

from sqlalchemy import func, select, tuple_

from zk_add import source_newest_slot as newest_slot
from zk_add.attendance_force_release import _lock_scheduler
from zk_add.attendance_legacy_uid import potentially_recoverable
from zk_add.attendance_manual_guard import decision_for
from zk_add.attendance_recovery import RecoveryError, _digest
from zk_add.attendance_release_safety import (
    AMBIGUOUS,
    CHANGED_NEAR_PUNCH,
    CONFLICT,
    DIFFERS,
    IDENTITY_CONFLICT_UNRESOLVED,
    MATCHES_CURRENT,
    NAME_DIFFERS,
    NONE,
    PUNCH_TIME_CNIC_HELD_BY_OTHER,
    RECORDS_TRUNCATED,
    PunchTimeIdentity,
    approval_tokens,
    auto_release_preview,
    classify,
    load_punch_evidence,
    other_cnics_masked,
    punch_cnics_held_elsewhere,
    safe_cnic,
    safe_text,
    single_event_identity,
    time_refusal,
)
from zk_add.attendance_repair import _immutable_facts
from zk_add.audit import append_audit
from zk_add.crypto import (
    cnic_lookup, decrypt_cnic, decrypt_json, encrypt_json, encrypt_text, mask_cnic, normalize_cnic,
)
from zk_add.db import session_scope
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision as Decision,
    AttendanceIdentityHistory,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    Connector,
    DeviceUser,
    OrdsOutbox,
    ZKTDevice,
)
from zk_add.ords_states import ORDS_ACKNOWLEDGED_STATUSES
from zk_add.service import oracle_payload
from zk_add.time_utils import ensure_utc, utc_now


ACTION = "MANUAL_DIRECT_ORDS"
POLICY = "manual-direct-ords-v1"
BATCH = 100
ACTIVE = {"RUNNING", "WAITING_ORACLE"}

PUNCH_TIME = "PUNCH_TIME"
CURRENT = "CURRENT"
PUNCH_TIME_SOURCE = "PUNCH_TIME_HISTORY"
# A saved direct approval that no longer validates may be replaced by a new
# explicit approval while ADD still holds or is about to retry the punch.
REPLACEABLE_STATUSES = frozenset({
    "BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT", "PENDING", "FAILED_RETRYABLE", "RETRYING",
})

REASONS = {
    "CLOCK_INVALID": "The terminal clock was invalid for this punch. It is never sent to Oracle.",
    "TIME_IMPOSSIBLE": (
        "The punch time is impossible (before 2010 or later than its capture). "
        "It is never sent to Oracle."
    ),
    "CNIC_CHOICE_REQUIRED": (
        "ADD's records do not show today's CNIC for this user ID at punch time. "
        "Choose which CNIC to send."
    ),
    "CNIC_CONFLICT": (
        "ADD's records show another CNIC or another person for this terminal user ID at or "
        "near the punch time, and nothing close enough to the punch says which. It is never "
        "sent by hand; confirm with HR who punched. The punch stays saved and held in ADD."
    ),
    "IDENTITY_CONFLICT_UNRESOLVED": (
        "Two active terminal users have this CNIC and the duplicate is not resolved. Resolve "
        "it in ADD Users first; the punch stays saved and held in ADD."
    ),
    "PUNCH_TIME_RECORDS_TOO_MANY": (
        "ADD holds too many identity records for this user ID at the punch time to check "
        "them all. No Oracle send was made; the punch stays saved and held in ADD."
    ),
    "PUNCH_TIME_CNIC_UNREADABLE": (
        "The CNIC ADD retained for the punch time cannot be read. No Oracle send was made."
    ),
    "PUNCH_TIME_NAME_MISSING": (
        "ADD has no employee name for the punch-time CNIC. No Oracle send was made."
    ),
    "PUNCH_TIME_CNIC_DIFFERS": (
        "After this approval ADD found another CNIC or person for this user ID at punch "
        "time, or the approved punch-time CNIC is no longer on record. The approval was stopped."
    ),
    "AUTO_RELEASE_PENDING": (
        "ADD releases this punch automatically at the next user snapshot. "
        "Confirm sending it now to override that."
    ),
    "SUPERSEDED": "A later administrator approval replaced this one.",
    "AUTO_RELEASED": (
        "ADD's own identity proof released this punch after the approval stopped; "
        "Oracle confirmed it through normal delivery."
    ),
    "CNIC_CHOICE_STALE": (
        "ADD's records for this punch changed after the checks were loaded. "
        "Reload the checks and choose again. No Oracle send was made."
    ),
    "PUNCH_TIME_CNIC_HELD_BY_OTHER": (
        "Another active terminal user has the punch-time CNIC today. Confirm that "
        "explicitly, after HR confirms who punched, to send it."
    ),
    "PREVIOUS_POST_UNCERTAIN": (
        "An earlier approval for this punch may already have reached Oracle with another "
        "identity. It cannot be replaced by hand or released automatically; review the "
        "Oracle record first."
    ),
}
# A stopped approval may have POSTed another CNIC; ADD's own release is held.
TAKEOVER = "TAKEOVER"
TAKEOVER_UNCERTAIN = "PREVIOUS_POST_UNCERTAIN"


@dataclass(frozen=True)
class Resolution:
    """The identity a direct approval would send, and why."""

    user: DeviceUser | None = None
    cnic: str | None = None
    source: str | None = None  # SAVED_PUNCH, SYNCED_USER or PUNCH_TIME_HISTORY
    current_cnic: str | None = None
    error: str | None = None
    punch_time: PunchTimeIdentity | None = None
    choice: str | None = None
    choices: tuple[str, ...] = ()
    interval: AttendanceIdentityHistory | None = None  # names the punch-time CNIC
    held_elsewhere: bool = False  # another active terminal user holds the punch-time CNIC today


def _source_digest(event: AttendanceEvent, connector: Connector) -> str:
    return _digest([
        _immutable_facts(event), event.raw_event, event.connector_id,
        event.zkt_device_id, event.device_user_id, event.cnic_encrypted,
        event.display_name, connector.hardware_id,
    ])


def _current_and_saved(event, users):
    if len(users) != 1:
        return None, None, None, "UNKNOWN_USER"
    try:
        current_cnic = normalize_cnic(decrypt_cnic(users[0].cnic_encrypted))
    except Exception:
        current_cnic = None
    if not current_cnic:
        return users[0], None, None, "CNIC_MISSING"
    try:
        saved_cnic = normalize_cnic(decrypt_cnic(event.cnic_encrypted))
    except Exception:
        saved_cnic = None
    return users[0], saved_cnic, current_cnic, None


def choices_for(punch: PunchTimeIdentity) -> tuple[str, ...]:
    """Which CNICs an administrator may choose for a punch with no saved CNIC."""
    if punch.state == DIFFERS:
        # Today's CNIC only for the same terminal user record whose CNIC was edited.
        return (PUNCH_TIME, CURRENT) if punch.continuity else (PUNCH_TIME,)
    if punch.state == CHANGED_NEAR_PUNCH:
        # The old CNIC only from today's own record that ended just before
        # the punch; today's only when its record started just after it.
        return tuple(
            option for option, allowed in (
                (PUNCH_TIME, punch.interval is not None), (CURRENT, punch.current_nearby),
            ) if allowed
        )
    if punch.state in {AMBIGUOUS, NONE}:
        return (CURRENT,)
    return ()


def _punch_time_interval(punch: PunchTimeIdentity, cnic_hash: str | None):
    """The newest retained interval at punch time that names ``cnic_hash``."""
    if punch.interval is not None and punch.interval.cnic_lookup_hash == cnic_hash:
        return punch.interval
    rows = [row for row in punch.good_intervals if row.cnic_lookup_hash == cnic_hash]
    return max(rows, key=lambda row: row.id) if rows else None


def _punch_time_resolution(user, current_cnic, punch, interval, *, choice, choices=()) -> Resolution:
    cnic = safe_cnic(interval.cnic_encrypted) if interval is not None else None
    if not cnic or cnic_lookup(cnic) != interval.cnic_lookup_hash:
        return Resolution(user, None, None, current_cnic, "PUNCH_TIME_CNIC_UNREADABLE", punch_time=punch)
    return Resolution(
        user, cnic, PUNCH_TIME_SOURCE, current_cnic,
        punch_time=punch, choice=choice, choices=choices, interval=interval,
    )


def _resolve(event, users, identity_for, *, choice=None) -> Resolution:
    """Pick the CNIC to send. Never today's CNIC by user ID alone.

    A CNIC saved with the punch is what ADD attributed at capture. Otherwise
    ADD's punch-time records decide: today's CNIC only when they show nothing
    else, or when the administrator explicitly chose it where that is allowed.
    """
    user, saved_cnic, current_cnic, error = _current_and_saved(event, users)
    if error:
        return Resolution(user=user, error=error)
    if saved_cnic:
        return Resolution(user, saved_cnic, "SAVED_PUNCH", current_cnic)
    punch = identity_for(user, cnic_lookup(current_cnic))
    if punch.state == MATCHES_CURRENT:
        return Resolution(
            user, current_cnic, "SYNCED_USER", current_cnic, punch_time=punch,
            choice=choice if choice in {PUNCH_TIME, CURRENT} else None,
        )
    if punch.state == CONFLICT:
        error = (
            "IDENTITY_CONFLICT_UNRESOLVED" if IDENTITY_CONFLICT_UNRESOLVED in punch.signals
            else "PUNCH_TIME_RECORDS_TOO_MANY" if RECORDS_TRUNCATED in punch.signals
            else "CNIC_CONFLICT"
        )
        return Resolution(user, None, None, current_cnic, error, punch_time=punch)
    choices = choices_for(punch)
    if choice == PUNCH_TIME and PUNCH_TIME in choices:
        return _punch_time_resolution(user, current_cnic, punch, punch.interval, choice=choice, choices=choices)
    if choice == CURRENT and CURRENT in choices:
        return Resolution(
            user, current_cnic, "SYNCED_USER", current_cnic,
            punch_time=punch, choice=choice, choices=choices,
        )
    return Resolution(
        user, None, None, current_cnic, "CNIC_CHOICE_REQUIRED",
        punch_time=punch, choices=choices,
    )


def _active_users(session, event):
    return session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == event.zkt_device_id,
            DeviceUser.user_id == event.user_id,
            DeviceUser.present.is_(True),
            DeviceUser.lifecycle_state == "ACTIVE",
        ).limit(2)
    ).all()


def _users_by_event(session, events) -> dict[int, list[DeviceUser]]:
    """The current active terminal users for each punch, in bounded queries."""
    found: dict = {}
    keys = sorted({(row.zkt_device_id, row.user_id) for row in events if row.zkt_device_id})
    for start in range(0, len(keys), 200):
        for user in session.scalars(
            select(DeviceUser).where(
                tuple_(DeviceUser.zkt_device_id, DeviceUser.user_id).in_(keys[start:start + 200]),
                DeviceUser.present.is_(True),
                DeviceUser.lifecycle_state == "ACTIVE",
            ).order_by(DeviceUser.id)
        ).all():
            found.setdefault((user.zkt_device_id, user.user_id), []).append(user)
    return {event.id: found.get((event.zkt_device_id, event.user_id), [])[:2] for event in events}


def _identity_finders(session, events, users_by_event):
    evidence = load_punch_evidence(session, events, {
        event.id: users[0] if len(users) == 1 else None for event, users in (
            (event, users_by_event[event.id]) for event in events
        )
    })
    return {
        event.id: (lambda user, current_hash, event=event: classify(
            event, user, current_hash, evidence[event.id],
        ))
        for event in events
    }


def _no_identity(_user, _current_hash) -> PunchTimeIdentity:
    """Records not loaded (a saved CNIC decides): never guess today's CNIC."""
    return PunchTimeIdentity(CONFLICT)


def _revalidate(session, event, saved: dict, users=None) -> Resolution:
    """Re-derive a saved approval's identity at queueing and at delivery.

    The approval stays valid while the same terminal user record is current,
    the approved CNIC is still the one it was (today's CNIC for a synced-user
    send, the retained punch-time CNIC for a punch-time send), and ADD has
    found no evidence of another CNIC or person that the approval did not
    already show the administrator. Better evidence for the same CNIC (a
    later snapshot covering the punch) never stops it; the choice label is
    not compared. Approvals saved before per-punch choices accepted no
    contradicting evidence at all.
    """
    users = _active_users(session, event) if users is None else users
    user, saved_cnic, current_cnic, error = _current_and_saved(event, users)
    if error:
        return Resolution(user=user, error=error)
    current_hash = cnic_lookup(current_cnic)
    source = saved.get("cnic_source")
    if source is not None and (
        saved.get("current_user_key") != user.user_key
        or (source != PUNCH_TIME_SOURCE and saved.get("current_cnic_hash") != current_hash)
    ):
        # Another terminal user record, or today's CNIC changed under a send
        # that uses it: stop rather than deliver a different identity.
        return Resolution(user, None, None, current_cnic, "SOURCE_CHANGED")
    if saved_cnic:
        return Resolution(user, saved_cnic, "SAVED_PUNCH", current_cnic)
    punch = single_event_identity(session, event, user, current_hash)
    held = False
    if source == PUNCH_TIME_SOURCE:
        sent_hash = saved.get("sent_cnic_hash") or saved.get("punch_time_cnic_hash")
        interval = _punch_time_interval(punch, sent_hash)
        if interval is None:
            return Resolution(user, None, None, current_cnic, "PUNCH_TIME_CNIC_DIFFERS", punch_time=punch)
        held = bool(punch_cnics_held_elsewhere(session, [(sent_hash, user.id)]))
        resolution = _punch_time_resolution(user, current_cnic, punch, interval, choice=PUNCH_TIME)
        resolution = replace(resolution, held_elsewhere=held)
    else:
        sent_hash = current_hash
        resolution = Resolution(
            user, current_cnic, "SYNCED_USER", current_cnic,
            punch_time=punch, choice=saved.get("cnic_choice"),
        )
    accepted = frozenset(saved.get("evidence_tokens") or ()) if "cnic_choice" in saved else frozenset()
    if not approval_tokens(punch, sent_hash, held_by_other=held) <= accepted:
        return Resolution(user, None, None, current_cnic, "PUNCH_TIME_CNIC_DIFFERS", punch_time=punch)
    return resolution


def _confirmed(event) -> bool:
    """Oracle already holds it: the ledger never offers it for a manual send."""
    return bool(event.oracle_confirmed_at or (event.ords_status or "").startswith("ACK"))


def _has_saved_cnic(event) -> bool:
    return bool(safe_cnic(event.cnic_encrypted))


def identity_hints_for_page(session, events):
    """Tell the ledger which rows have a usable synced identity in bounded queries.

    Punch-time records are read only for rows a manual send could still
    decide (not confirmed, no CNIC saved with the punch); the send dialog
    polls this list during a run.
    """
    from zk_add.worker import event_uid_is_valid

    if not events:
        return {}
    users_by_event = _users_by_event(session, events)
    undecided = [event for event in events if not _confirmed(event) and not _has_saved_cnic(event)]
    finders = _identity_finders(session, undecided, users_by_event) if undecided else {}
    hints = {}
    for event in events:
        if _confirmed(event):
            user, saved_cnic, current_cnic, _error = _current_and_saved(event, users_by_event[event.id])
            hints[event.id] = {
                "eligible": False,
                "cnic_source": "SAVED_PUNCH" if saved_cnic else "SYNCED_USER" if current_cnic else None,
                "exclusion": "ALREADY_CONFIRMED",
                "punch_time_cnic": None,
                "cnic_choice_required": False,
            }
            continue
        resolution = _resolve(event, users_by_event[event.id], finders.get(event.id) or _no_identity)
        error = resolution.error
        if error == "CNIC_CHOICE_REQUIRED":
            error = None  # The administrator chooses in the send dialog.
        if not error:
            error = time_refusal(event)
        if not error and not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            error = "INVALID_EVENT_UID"
        punch = resolution.punch_time
        hints[event.id] = {
            "eligible": error is None,
            "cnic_source": resolution.source or (
                "SYNCED_USER" if resolution.error == "CNIC_CHOICE_REQUIRED" else None
            ),
            "exclusion": error,
            "punch_time_cnic": punch.state if punch else None,
            "cnic_choice_required": resolution.error == "CNIC_CHOICE_REQUIRED",
        }
    return hints


def _employee_name(event, resolution: Resolution):
    user = resolution.user
    if resolution.source == PUNCH_TIME_SOURCE:
        # The name retained with the punch-time CNIC, or the punch's own name;
        # never today's user's name next to another person's CNIC.
        return (
            safe_text(resolution.interval.display_name_encrypted) if resolution.interval else None
        ) or event.display_name
    if resolution.source == "SYNCED_USER":
        return user.display_name or event.display_name
    return event.display_name or user.display_name


def _payload(connector, terminal, event, resolution: Resolution):
    payload = oracle_payload(connector, terminal, event, resolution.cnic)
    payload["device_serial"] = event.device_serial or "unknown"
    payload["employee_name"] = _employee_name(event, resolution)
    return payload


def _identity_proof(resolution: Resolution, *, legacy: bool = False) -> dict:
    """Saved with each item and decision; queueing and delivery re-derive it.

    An approval saved before per-punch choices keeps its original keys, so it
    is re-checked under the rule it was approved with (see ``_revalidate``).
    """
    proof = {
        "cnic_source": resolution.source,
        "current_user_key": resolution.user.user_key,
        "current_cnic_hash": cnic_lookup(resolution.current_cnic),
    }
    if legacy:
        return proof
    punch = resolution.punch_time
    sent_hash = cnic_lookup(resolution.cnic)
    return {
        **proof,
        "cnic_choice": resolution.choice,
        "punch_time_state": punch.state if punch else None,
        "punch_time_cnic_hash": sent_hash if resolution.source == PUNCH_TIME_SOURCE else None,
        "sent_cnic_hash": sent_hash,
        # Evidence the administrator saw and accepted; anything new stops it.
        "evidence_tokens": sorted(approval_tokens(
            punch, sent_hash,
            held_by_other=resolution.held_elsewhere and resolution.source == PUNCH_TIME_SOURCE,
        )) if punch else [],
    }


EMPTY_PROOF = {
    "cnic_source": None, "current_user_key": None, "current_cnic_hash": None,
    "cnic_choice": None, "punch_time_state": None, "punch_time_cnic_hash": None,
    "sent_cnic_hash": None, "evidence_tokens": [],
}


def _same_identity(saved: dict, resolution: Resolution) -> bool:
    """The same terminal user record and approved CNIC source as at approval."""
    if (
        saved.get("cnic_source") != resolution.source
        or saved.get("current_user_key") != resolution.user.user_key
    ):
        return False
    if resolution.source == PUNCH_TIME_SOURCE:
        # Today's CNIC may change (an HR correction) without changing the
        # punch-time CNIC this approval sends; _revalidate checked that one.
        return True
    return saved.get("current_cnic_hash") == cnic_lookup(resolution.current_cnic)


def _released_automatically_after(event, decision) -> bool:
    """ADD's own identity proof released this punch after the direct approval was saved."""
    from zk_add.attendance_manual_guard import requires_approval

    return not (
        decision is None
        or decision.proof.get("policy") != POLICY
        or decision.created_at is None
        or event.identity_resolved_at is None
        or requires_approval(event)
        or ensure_utc(event.identity_resolved_at) <= ensure_utc(decision.created_at)
    )


def _posted_cnic_is_todays_release(event, decision) -> bool:
    """The stopped approval's payload carries the CNIC ADD's own release now sends.

    Oracle keeps the first stored row for an event UID; if that row carries
    the same CNIC, the automatic release names the same person.
    """
    try:
        posted = normalize_cnic(decrypt_json(decision.payload_encrypted).get("cnic"))
    except Exception:
        return False
    released = safe_cnic(event.cnic_encrypted)
    return bool(posted and released and posted == released and cnic_lookup(posted) == event.cnic_lookup_hash)


def automatic_takeover(session, event, decision) -> str | None:
    """May ordinary delivery own a punch whose direct approval stopped?

    None: the approval still owns the punch, or ADD's own proof has not
    released it since the approval was saved. ``TAKEOVER``: the automatic
    proof (and database guard) released it after the approval stopped, so the
    punch must not stay held forever behind a stale approval.
    ``PREVIOUS_POST_UNCERTAIN``: the stopped approval may already have POSTed
    a different CNIC. Ordinary delivery treats Oracle's 409 ("already
    stored") as success with no content check, so it would certify the
    automatic CNIC while Oracle keeps the posted one: the punch is held for
    review instead. Nothing here retries the approval itself.
    """
    if decision is None or not _released_automatically_after(event, decision):
        return None
    if approved_payload(session, event, session.get(Connector, event.connector_id), decision) is not None:
        return None
    if _post_attempted(session, decision) and not _posted_cnic_is_todays_release(event, decision):
        return TAKEOVER_UNCERTAIN
    return TAKEOVER


def superseded_by_automatic_release(session, event, decision) -> bool:
    """ADD's own proof released this punch after a stopped direct approval.

    A direct approval that no longer validates must not keep the punch held
    forever: once ADD's automatic identity proof (and the database guard)
    released the punch after that approval was saved, ordinary delivery owns
    it again, unless the approval may already have POSTed another CNIC (see
    ``automatic_takeover``). The automatic path is unchanged; this only stops
    a stale manual approval from overriding it.
    """
    return automatic_takeover(session, event, decision) == TAKEOVER


def hold_uncertain_takeover(session, event, outbox, decision) -> None:
    """Keep a punch whose stopped approval may be in Oracle under another CNIC.

    QUARANTINED_IDENTITY_CONFLICT is a review state the snapshot release
    never touches, so the punch does not loop through BLOCKED_IDENTITY and an
    automatic release every snapshot. The approval's item says why.
    """
    outbox.status = event.ords_status = "QUARANTINED_IDENTITY_CONFLICT"
    outbox.next_attempt_at = None
    outbox.last_error = TAKEOVER_UNCERTAIN
    item = (
        session.get(Item, decision.item_id, with_for_update=True, populate_existing=True)
        if decision.item_id else None
    )
    if item is not None:
        item.status, item.error_code = "NEEDS_REVIEW", TAKEOVER_UNCERTAIN
        item.completed_at = item.completed_at or utc_now()
        item.result = {**(item.result or {}), "reason": REASONS[TAKEOVER_UNCERTAIN], "needs_attention": True}
        item.updated_at = utc_now()


def _post_attempted(session, decision) -> bool:
    """Whether a direct approval may already have POSTed to Oracle.

    Send intent is saved before every direct POST (``reserve_direct_post``),
    so a zero count proves no POST under this approval. A missing item is
    treated as attempted.
    """
    item = session.get(Item, decision.item_id) if decision.item_id else None
    return item is None or bool((item.result or {}).get("direct_post_attempts"))


def _stopped_and_held(session, event, decision) -> bool:
    """The approval stopped and ADD holds the punch; nothing would resume it.

    Its run item was closed for review while the punch is held for identity,
    so the worker never claims it. Only the duplicate-CNIC restore can
    resume it, and a new explicit approval that wins the row lock first
    supersedes it instead.
    """
    item = session.get(Item, decision.item_id) if decision.item_id else None
    return bool(
        item is not None and item.status == "NEEDS_REVIEW"
        and event.ords_status in {"BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT"}
    )


def _replacement_block(session, event, decision, outbox, connector) -> str | None:
    """Why a saved direct approval may not be replaced; None when it may.

    Only an approval that can no longer deliver (it no longer validates, or
    it stopped and ADD holds the punch), that ADD's own release has not taken
    over, and that never POSTed to Oracle may be replaced: a POST may have
    stored the earlier identity in Oracle, and a replacement's first content
    check would otherwise POST a different identity for the same event UID.
    """
    if (
        decision.proof.get("policy") == POLICY
        and event.ords_status == "QUARANTINED_IDENTITY_CONFLICT"
        and outbox is not None and outbox.last_error == TAKEOVER_UNCERTAIN
    ):
        return TAKEOVER_UNCERTAIN
    if not (
        decision.proof.get("policy") == POLICY
        and event.ords_status in REPLACEABLE_STATUSES
        and not (outbox and outbox.status == "IN_FLIGHT")
        and event.oracle_confirmed_at is None
        and connector is not None
        and not _released_automatically_after(event, decision)
        and (
            _stopped_and_held(session, event, decision)
            or approved_payload(session, event, connector, decision) is None
        )
    ):
        return "ALREADY_DELIVERING"
    if _post_attempted(session, decision):
        return "PREVIOUS_POST_UNCERTAIN"
    return None


def evidence_digest(event, resolution: Resolution, held_elsewhere: bool) -> str | None:
    """The punch-time evidence the administrator saw; a CNIC choice is bound to it.

    An opaque SHA-256 over keyed CNIC lookup hashes and record ids: it reveals
    no CNIC, and any new record, signal or holder changes it.
    """
    punch, user = resolution.punch_time, resolution.user
    if punch is None or user is None:
        return None
    return _digest([
        "punch-time-evidence-v1", event.id, user.user_key, cnic_lookup(resolution.current_cnic),
        punch.state, sorted(punch.signals), sorted(value for value in punch.evidence_hashes if value),
        punch.unknown_holder, punch.cnic_hash, punch.interval.id if punch.interval is not None else None,
        punch.basis, punch.position, list(choices_for(punch)), bool(held_elsewhere),
    ])


def _auto_release_key(event, digest):
    """What the cached automatic-release answer for a punch depended on."""
    return (
        event.ords_status, bool(event.manual_release_required), event.captured_cnic_lookup_hash,
        event.identity_resolved_at.isoformat() if event.identity_resolved_at else None, digest,
    )


@dataclass(frozen=True)
class Assessment:
    event: AttendanceEvent
    connector: Connector | None
    terminal: ZKTDevice | None
    resolution: Resolution
    error: str | None
    auto_release: dict
    replaces_decision: bool = False
    held_elsewhere: bool = False
    evidence_digest: str | None = None


def assess_many(
    session, events, *, choices=None, send_now=frozenset(), include_guard=True,
    evidence_digests=None, held_confirmed=frozenset(), auto_release_cache=None,
) -> list[Assessment]:
    """Everything a direct approval checks for each punch, without writing.

    ``evidence_digests`` (approval only) binds each CNIC choice to the
    evidence the administrator saw. ``auto_release_cache`` reuses automatic
    release answers computed before the scheduler lock for punches whose
    state and evidence did not change since.
    """
    from zk_add.worker import event_uid_is_valid

    choices = choices or {}
    auto_release_cache = auto_release_cache or {}
    users_by_event = _users_by_event(session, events)
    finders = _identity_finders(session, events, users_by_event)
    resolutions = {
        event.id: _resolve(event, users_by_event[event.id], finders[event.id], choice=choices.get(event.id))
        for event in events
    }
    held = punch_cnics_held_elsewhere(session, [
        (resolution.punch_time.cnic_hash, resolution.user.id)
        for resolution in resolutions.values()
        if resolution.punch_time is not None and resolution.user is not None
        and resolution.punch_time.cnic_hash and PUNCH_TIME in choices_for(resolution.punch_time)
    ])
    ids = [event.id for event in events]
    decisions: dict = {}
    outboxes: dict = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        latest = (
            select(func.max(Decision.id)).where(Decision.attendance_event_id.in_(chunk))
            .group_by(Decision.attendance_event_id)
        )
        for decision in session.scalars(select(Decision).where(Decision.id.in_(latest))).all():
            decisions[decision.attendance_event_id] = decision
        for outbox in session.scalars(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id.in_(chunk))).all():
            outboxes[outbox.attendance_event_id] = outbox
    checks = []
    for event in events:
        connector = session.get(Connector, event.connector_id)
        terminal = session.get(ZKTDevice, event.zkt_device_id)
        resolution = resolutions[event.id]
        held_elsewhere = bool(
            resolution.punch_time is not None and resolution.user is not None
            and (resolution.punch_time.cnic_hash, resolution.user.id) in held
        )
        resolution = replace(resolution, held_elsewhere=held_elsewhere)
        digest = evidence_digest(event, resolution, held_elsewhere)
        outbox, decision = outboxes.get(event.id), decisions.get(event.id)
        block = _replacement_block(session, event, decision, outbox, connector) if decision is not None else None
        replaces = decision is not None and block is None
        auto_release = {"at_next_snapshot": False, "awaiting_snapshot": False, "database_guard": "NOT_CHECKED"}
        if event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
            error = "ALREADY_CONFIRMED"
        elif outbox and outbox.status == "IN_FLIGHT":
            error = "ALREADY_DELIVERING"
        elif block:
            error = block
        elif not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            error = "INVALID_EVENT_UID"
        elif not connector or not terminal:
            error = "SOURCE_MISSING"
        else:
            stale = bool(
                evidence_digests is not None and event.id in choices
                and evidence_digests.get(event.id) != digest
            )
            error = time_refusal(event) or ("CNIC_CHOICE_STALE" if stale else None)
            if not error and newest_slot.newest_only_event_ids(session, [event]):
                # Bytes read from an unwritten newest slot may carry another person's
                # identity, so no CNIC choice can make them sendable.
                error = newest_slot.UNCONFIRMED_CONDITION
            error = error or resolution.error
        if (
            error is None and resolution.source == PUNCH_TIME_SOURCE and held_elsewhere
            and event.id not in held_confirmed
        ):
            error = PUNCH_TIME_CNIC_HELD_BY_OTHER
        if error is None and resolution.source == PUNCH_TIME_SOURCE and not _employee_name(event, resolution):
            error = "PUNCH_TIME_NAME_MISSING"
        if error is None or error == "CNIC_CHOICE_REQUIRED":
            key = _auto_release_key(event, digest)
            cached = auto_release_cache.get(event.id)
            if cached is not None and cached[0] == key:
                auto_release = cached[1]
            else:
                auto_release = auto_release_preview(
                    session, event, user=resolution.user, identity=resolution.punch_time,
                    include_guard=include_guard,
                )
            if error is None and event.id not in send_now and (
                auto_release["at_next_snapshot"] or auto_release["awaiting_snapshot"]
            ):
                error = "AUTO_RELEASE_PENDING"
        checks.append(Assessment(
            event, connector, terminal, resolution, error, auto_release, replaces, held_elsewhere, digest,
        ))
    return checks


def assess(session, event, *, choice=None, send_now=False, include_guard=True) -> Assessment:
    return assess_many(
        session, [event], choices={event.id: choice} if choice else {},
        send_now={event.id} if send_now else set(), include_guard=include_guard,
    )[0]


def _reason(error: str | None) -> str:
    if error == "INVALID_EVENT_UID":
        return "Oracle cannot accept this older punch ID. The punch remains saved for ID repair."
    if error in REASONS:
        return REASONS[error]
    return error.replace("_", " ").title() if error else "Approved; waiting to prepare delivery."


def _group_key(kind: str, row: dict, *facts) -> str:
    """An opaque group id: punches share it only when their evidence is identical."""
    return f"{kind}|" + _digest([
        kind, row["zkt_device_id"], row["device_serial"], row["user_id"], *facts,
    ])[:24]


def _decision(row, punch, held_elsewhere: bool) -> tuple[str | None, str | None]:
    """How the dialog asks for this punch: one choice per group of identical evidence.

    Groups never span terminals, serials or user IDs. Punches whose records
    disagree near the punch time, or whose saved name is not today's user's,
    are decided one at a time.
    """
    if row["exclusion"] is not None or not row["cnic_choice_required"]:
        return None, None
    per_row = f"row|{row['event_id']}"
    choices = row["cnic_choices"]
    signals = sorted(punch.signals)
    if punch.state == DIFFERS:
        return "CHOOSE_CNIC", _group_key(
            "choose", row, punch.cnic_hash, punch.basis, choices, signals, held_elsewhere,
        )
    if punch.state == CHANGED_NEAR_PUNCH:
        return ("CHOOSE_CNIC" if PUNCH_TIME in choices else "CONFIRM_TODAY"), per_row
    if punch.state == NONE and NAME_DIFFERS not in punch.signals:
        # No record at the punch time: one confirmation per terminal user ID,
        # serial and position in ADD's history (before its first record, in a
        # break, after its last record, or no record at all).
        return "CONFIRM_TODAY", _group_key("today", row, punch.position, signals)
    return "CONFIRM_TODAY", per_row


def preview(session, event_ids) -> dict:
    """Read-only checks the send dialog shows before an administrator approves."""
    rows = session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(sorted(set(event_ids))))
        .order_by(AttendanceEvent.id)
    ).all()
    found = {row.id for row in rows}
    checks = assess_many(session, rows)
    result = []
    for check in checks:
        event = check.event
        resolution, punch = check.resolution, check.resolution.punch_time
        user = resolution.user
        current_hash = cnic_lookup(resolution.current_cnic)
        held_elsewhere = check.held_elsewhere
        signals = sorted((punch.signals if punch else set()) | (
            {PUNCH_TIME_CNIC_HELD_BY_OTHER} if held_elsewhere else set()
        ))
        other_cnic = bool(punch is not None and punch.cnic_hash and punch.interval is not None)
        hidden = {None, "CNIC_CHOICE_REQUIRED", "AUTO_RELEASE_PENDING"}
        row = {
            "event_id": event.id,
            "zkt_device_id": event.zkt_device_id,
            "device_serial": event.device_serial,
            "user_id": event.user_id,
            "display_name": user.display_name if user else event.display_name,
            "device_event_time": event.device_event_time,
            "exclusion": None if check.error in hidden else check.error,
            "exclusion_reason": None if check.error in hidden else _reason(check.error),
            "cnic_source": resolution.source,
            "punch_time_cnic": punch.state if punch else None,
            "punch_time_cnic_masked": mask_cnic(punch.cnic) if other_cnic else None,
            "punch_time_display_name": punch.display_name if other_cnic else None,
            # COVERING_RECORD, BRIDGED_GAP (a break between two identical
            # records) or RECORD_BEFORE_PUNCH (today's own record of the old
            # CNIC ended just before an uncovered punch).
            "punch_time_cnic_basis": punch.basis if other_cnic else None,
            "record_position": punch.position if punch else None,
            "first_observed_at": punch.first_observed_at if punch else None,
            # An uncovered punch: when the record before it ended and the one after it started.
            "record_before_ended_at": punch.record_before_until if punch else None,
            "record_after_started_at": punch.record_after_from if punch else None,
            "punch_display_name": event.display_name if punch and NAME_DIFFERS in punch.signals else None,
            "other_cnics_masked": other_cnics_masked(punch, current_hash) if punch else [],
            "evidence_signals": signals,
            "current_cnic_masked": mask_cnic(resolution.current_cnic),
            "uid_matches_current": bool(punch.uid_matches_current) if punch else bool(
                user and (not event.uid or not user.uid or user.uid == event.uid)
            ),
            "identity_conflict_code": (
                user.identity_conflict_code
                if user is not None and punch is not None and IDENTITY_CONFLICT_UNRESOLVED in punch.signals
                else None
            ),
            "punch_time_cnic_held_by_other": held_elsewhere,
            "cnic_choice_required": check.error == "CNIC_CHOICE_REQUIRED",
            "cnic_choices": list(resolution.choices),
            "default_cnic_choice": (
                PUNCH_TIME if punch is not None and punch.state == DIFFERS
                and PUNCH_TIME in resolution.choices and not held_elsewhere else None
            ),
            # Echoed with a CNIC choice; ADD skips the choice if the evidence changed.
            "evidence_digest": check.evidence_digest,
            "auto_release_at_next_snapshot": check.auto_release["at_next_snapshot"],
            "awaiting_next_snapshot": check.auto_release["awaiting_snapshot"],
            "database_guard": check.auto_release["database_guard"],
            "replaces_stopped_approval": check.replaces_decision and check.error not in {
                "ALREADY_CONFIRMED", "ALREADY_DELIVERING",
            },
        }
        row["decision_kind"], row["decision_group"] = _decision(row, punch, held_elsewhere)
        result.append(row)
    return {
        "rows": result,
        "missing_event_ids": sorted(set(event_ids) - found),
        "counts": {
            "selected": len(set(event_ids)),
            "refused": sum(row["exclusion"] is not None for row in result),
            "cnic_choice_required": sum(row["cnic_choice_required"] for row in result),
            "auto_release_soon": sum(
                row["auto_release_at_next_snapshot"] or row["awaiting_next_snapshot"]
                for row in result
            ),
        },
    }


@dataclass(frozen=True)
class RequestDecisions:
    choices: dict
    send_now: frozenset
    evidence_digests: dict
    held_confirmed: frozenset


def _request_decisions(request) -> RequestDecisions:
    rows = getattr(request, "cnic_choices", None) or []
    return RequestDecisions(
        choices={row.event_id: row.choice for row in rows},
        send_now=frozenset(getattr(request, "send_now_event_ids", None) or []),
        evidence_digests={row.event_id: row.evidence_digest for row in rows},
        held_confirmed=frozenset(row.event_id for row in rows if row.held_by_other_confirmed),
    )


def _existing_run(session, *, actor: str, request, request_digest: str):
    previous = session.scalar(select(Job).where(Job.idempotency_key == request.idempotency_key))
    if previous and (
        previous.action != ACTION or previous.actor != actor or previous.candidate_digest != request_digest
    ):
        raise RecoveryError("This request key belongs to another selection.", "IDEMPOTENCY_CONFLICT")
    return previous


def create(session, *, actor: str, request):
    event_ids = sorted(request.event_ids)
    decided = _request_decisions(request)
    choices, send_now = decided.choices, decided.send_now
    request_digest = _digest(
        [ACTION, actor, event_ids, request.reason.strip()]
        # Approvals saved before per-punch decisions keep their digest.
        + ([
            sorted(
                [event_id, choice, decided.evidence_digests.get(event_id), event_id in decided.held_confirmed]
                for event_id, choice in choices.items()
            ),
            sorted(send_now),
        ] if choices or send_now else [])
    )
    previous = _existing_run(session, actor=actor, request=request, request_digest=request_digest)
    if previous:
        return previous
    decisions = dict(
        choices=choices, send_now=send_now, evidence_digests=decided.evidence_digests,
        held_confirmed=decided.held_confirmed,
    )
    # ADD's automatic proof and its database guard are the slow part of the
    # checks (one proof per punch). Run them before taking the scheduler lock
    # and the row locks, so a snapshot release never waits on them; under the
    # locks every identity, time, choice and delivery check runs again, and
    # an automatic-release answer is reused only for a punch whose state and
    # evidence are unchanged. Every check reads before the run is added, so
    # the read-only database guard probe never flushes a half-built approval.
    unlocked = session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(event_ids)).order_by(AttendanceEvent.id)
    ).all()
    if len(unlocked) != len(event_ids):
        raise RecoveryError("One of the selected punches is no longer saved.", "SOURCE_MISSING")
    early = {
        check.event.id: (_auto_release_key(check.event, check.evidence_digest), check.auto_release)
        for check in assess_many(session, unlocked, **decisions)
        if check.error is None or check.error in {"CNIC_CHOICE_REQUIRED", "AUTO_RELEASE_PENDING"}
    }
    _lock_scheduler(session)
    # Everything read before the lock (terminal users, identity records,
    # outboxes, earlier approvals and their items) is read again from the
    # database, not from the session's identity map: a snapshot or delivery
    # that committed meanwhile must decide the checks below. Pending writes
    # are flushed first so nothing of this transaction is lost.
    session.flush()
    session.expire_all()
    previous = _existing_run(session, actor=actor, request=request, request_digest=request_digest)
    if previous:
        return previous
    rows = session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(event_ids))
        .order_by(AttendanceEvent.id).with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) != len(event_ids):
        raise RecoveryError("One of the selected punches is no longer saved.", "SOURCE_MISSING")
    checks = assess_many(session, rows, auto_release_cache=early, **decisions)
    now = utc_now()
    job = Job(
        action=ACTION, status="RUNNING", actor=actor,
        reason="Administrator-selected Oracle delivery",
        idempotency_key=request.idempotency_key,
        scope={
            "policy": POLICY,
            "selected_event_ids": event_ids,
            "approval": {"at": now.isoformat(), "reason_encrypted": encrypt_text(request.reason.strip())},
            "cnic_choices": {str(key): value for key, value in sorted(choices.items())},
            "cnic_choice_evidence": {
                str(key): value for key, value in sorted(decided.evidence_digests.items())
            },
            "held_by_other_confirmed_event_ids": sorted(decided.held_confirmed),
            "send_now_event_ids": sorted(send_now),
        },
        candidate_digest=request_digest,
        requested_count=len(rows), eligible_count=0, excluded_count=0,
        started_at=now, updated_at=now,
    )
    session.add(job)
    session.flush()
    accepted = 0
    excluded_by_code: dict[str, int] = {}
    replaced = 0
    for check in checks:
        event, error = check.event, check.error
        payload = (
            _payload(check.connector, check.terminal, event, check.resolution)
            if error is None else None
        )
        if payload:
            accepted += 1
            replaced += int(check.replaces_decision)
        else:
            excluded_by_code[error] = excluded_by_code.get(error, 0) + 1
        session.add(Item(
            job_id=job.id, source_kind="ATTENDANCE_EVENT", source_ref=str(event.id),
            connector_id=event.connector_id, attendance_event_id=event.id,
            expected_state_digest=(
                _source_digest(event, check.connector) if check.connector else _digest(event.id)
            ),
            lane="DIRECT_ORDS",
            status="READY" if payload else "SKIPPED",
            error_code=error,
            result={
                "payload_encrypted": encrypt_json(payload) if payload else None,
                "payload_digest": _digest(payload) if payload else None,
                **(_identity_proof(check.resolution) if payload else EMPTY_PROOF),
                "auto_release_override": bool(
                    payload and (
                        check.auto_release["at_next_snapshot"]
                        or check.auto_release["awaiting_snapshot"]
                    )
                ),
                "replaces_stopped_approval": bool(payload and check.replaces_decision),
                "reason": _reason(error),
            },
            completed_at=now if error else None,
        ))
    job.eligible_count = accepted
    job.excluded_count = len(rows) - accepted
    if accepted == 0:
        job.status, job.completed_at = "COMPLETED_WITH_REVIEW", now
    append_audit(
        session, actor=actor, action="ATTENDANCE_DIRECT_ORDS_APPROVED",
        target_type="attendance_recovery_job", target_id=job.job_id,
        outcome="APPROVED", after={
            "selected": len(rows), "ready": accepted, "digest": request_digest,
            "excluded": dict(sorted(excluded_by_code.items())),
            "cnic_choices": {
                choice: sum(value == choice for value in choices.values())
                for choice in sorted(set(choices.values()))
            },
            "send_now": len(send_now),
            "held_by_other_confirmed": len(decided.held_confirmed),
            "replaces_stopped_approvals": replaced,
        },
    )
    return job


def approved_payload(session, event, connector, decision):
    if decision.proof.get("policy") != POLICY or connector is None:
        return None
    if time_refusal(event):
        return None
    if _source_digest(event, connector) != decision.proof.get("source_digest"):
        return None
    users = _active_users(session, event)
    if _current_and_saved(event, users)[3]:
        return None
    try:
        payload = decrypt_json(decision.payload_encrypted)
    except Exception:
        return None
    if _digest(payload) != decision.payload_digest:
        return None
    if decision.proof.get("cnic_source") is None:
        return payload  # Preserve approvals queued before the stricter identity proof.
    resolution = _revalidate(session, event, decision.proof, users=users)
    if resolution.error:
        return None
    terminal = session.get(ZKTDevice, event.zkt_device_id)
    if (
        terminal is None
        or not _same_identity(decision.proof, resolution)
        or payload != _payload(connector, terminal, event, resolution)
    ):
        return None
    return payload


def _queue(session, job, item):
    outbox = session.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == item.attendance_event_id)
        .with_for_update()
    )
    event = session.scalar(
        select(AttendanceEvent).where(AttendanceEvent.id == item.attendance_event_id)
        .with_for_update()
    )
    connector = session.get(Connector, event.connector_id) if event else None
    resolution, legacy = None, "cnic_choice" not in item.result
    previous = decision_for(session, event) if event else None
    if not event or not connector or item.expected_state_digest != _source_digest(event, connector):
        code = "SOURCE_CHANGED"
    elif event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
        code = "ALREADY_CONFIRMED"
    elif previous is not None and (block := _replacement_block(session, event, previous, outbox, connector)):
        code = block
    elif outbox and outbox.status == "IN_FLIGHT":
        return  # Let the earlier claim settle; the next short tick will recheck.
    else:
        resolution = _revalidate(session, event, item.result)
        code = time_refusal(event) or resolution.error
        if not code and newest_slot.newest_only_event_ids(session, [event]):
            code = newest_slot.UNCONFIRMED_CONDITION
    if code:
        item.status, item.error_code, item.completed_at = "SKIPPED", code, utc_now()
        item.result = {"reason": _reason(code)}
        return
    try:
        payload = decrypt_json(item.result["payload_encrypted"])
    except Exception:
        item.status, item.error_code, item.completed_at = (
            "NEEDS_REVIEW", "APPROVAL_PAYLOAD_UNREADABLE", utc_now()
        )
        item.result = {"reason": "The saved approval cannot be read. No Oracle send was made."}
        return
    if (
        payload != _payload(connector, session.get(ZKTDevice, event.zkt_device_id), event, resolution)
        or not _same_identity(item.result, resolution)
    ):
        item.status, item.error_code, item.completed_at = "SKIPPED", "SOURCE_CHANGED", utc_now()
        item.result = {"reason": "Saved punch or employee changed before delivery."}
        return
    decision = Decision(
        item_id=item.id, attendance_event_id=event.id, job_id=job.id,
        actor=job.actor, reason_encrypted=job.scope["approval"]["reason_encrypted"],
        proof={"policy": POLICY, "source_digest": item.expected_state_digest,
               "immutable_facts": _immutable_facts(event), "hardware_id": connector.hardware_id,
               "terminal": event.device_serial,
               **_identity_proof(resolution, legacy=legacy),
               "auto_release_override": bool(item.result.get("auto_release_override"))},
        prior_state_encrypted=encrypt_json({
            "ords_status": event.ords_status, "manual_release_required": event.manual_release_required,
        }),
        payload_encrypted=item.result["payload_encrypted"],
        payload_digest=item.result["payload_digest"], operation_id=str(uuid4()),
    )
    session.add(decision)
    event.manual_release_required = True
    event.ords_status = "PENDING"
    if outbox is None:
        outbox = OrdsOutbox(attendance_event_id=event.id)
        session.add(outbox)
    outbox.status, outbox.delivery_type = "PENDING", "FULL_HISTORY"
    outbox.next_attempt_at, outbox.last_error = None, None
    outbox.payload_hash = decision.payload_digest
    item.status, item.error_code = "WAITING_ORACLE", None
    item.result = {"reason": "Waiting for Oracle confirmation.", "cnic_source": resolution.source}
    audit = append_audit(
        session, actor=job.actor, action="ATTENDANCE_DIRECT_ORDS_QUEUED",
        target_type="attendance_event", target_id=event.event_uid,
        outcome="WAITING_ORACLE", after={
            "job_id": job.job_id, "payload_digest": decision.payload_digest,
            "cnic_source": resolution.source, "cnic_choice": resolution.choice,
            "auto_release_override": decision.proof["auto_release_override"],
            "replaces_decision_id": previous.id if previous is not None else None,
        },
    )
    decision.audit_id = audit.id


def _observe(session, job):
    from zk_add.worker import event_uid_is_valid

    waiting = session.scalars(
        select(Item).where(Item.job_id == job.id, Item.status == "WAITING_ORACLE")
        .order_by(Item.id).limit(BATCH)
    ).all()
    for item in waiting:
        event = session.get(AttendanceEvent, item.attendance_event_id)
        decision = decision_for(session, event) if event else None
        if not event:
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", "SOURCE_MISSING", utc_now()
        elif decision is not None and decision.item_id != item.id:
            # A later explicit approval replaced this stopped one; it owns delivery.
            # Replacement is refused after any POST, so nothing of this one is in
            # Oracle; a recorded POST would still be shown for review.
            item.status, item.error_code, item.completed_at = "SKIPPED", "SUPERSEDED", utc_now()
            item.result = {
                **item.result, "reason": REASONS["SUPERSEDED"],
                "needs_attention": bool(item.result.get("direct_post_attempts")),
            }
        elif event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
            if decision and item.result.get("oracle_verified_payload_digest") == decision.payload_digest:
                item.status, item.completed_at = "CONFIRMED", utc_now()
            elif superseded_by_automatic_release(session, event, decision):
                item.status, item.error_code, item.completed_at = "SKIPPED", "AUTO_RELEASED", utc_now()
                item.result = {
                    **item.result, "reason": REASONS["AUTO_RELEASED"],
                    # A POST under this approval carried the same CNIC; still shown.
                    "needs_attention": bool(item.result.get("direct_post_attempts")),
                }
        elif (
            event.ords_status == "QUARANTINED_IDENTITY_CONFLICT"
            and (outbox := session.scalar(
                select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id)
            )) is not None
            and outbox.last_error == TAKEOVER_UNCERTAIN
        ):
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", TAKEOVER_UNCERTAIN, utc_now()
            item.result = {**item.result, "reason": REASONS[TAKEOVER_UNCERTAIN], "needs_attention": True}
        elif not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            outbox = session.scalar(
                select(OrdsOutbox)
                .where(OrdsOutbox.attendance_event_id == event.id)
                .with_for_update()
            )
            # An in-flight worker owns the row and will apply the same guard.
            if outbox and outbox.status != "IN_FLIGHT":
                now = utc_now()
                outbox.status = event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
                outbox.next_attempt_at, outbox.last_error = None, "INVALID_EVENT_UID"
                item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", "INVALID_EVENT_UID", now
                item.result = {
                    **item.result,
                    "reason": "Oracle cannot accept this older punch ID. The punch and approval remain saved for ID repair.",
                    "needs_attention": True,
                }
        elif event.ords_status not in {"PENDING", "IN_FLIGHT", "FAILED_RETRYABLE", "RETRYING"}:
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", event.ords_status, utc_now()
        item.updated_at = utc_now()


def advance_once(session):
    _lock_scheduler(session)
    job = session.scalar(
        select(Job).where(Job.action == ACTION, Job.status.in_(ACTIVE))
        .order_by(Job.updated_at, Job.id).limit(1).with_for_update(skip_locked=True)
    )
    if not job:
        return
    ready = session.scalars(
        select(Item).where(Item.job_id == job.id, Item.status == "READY")
        .order_by(Item.id).limit(BATCH)
    ).all()
    for item in ready:
        _queue(session, job, item)
    _observe(session, job)
    session.flush()
    counts = dict(session.execute(
        select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)
    ).all())
    job.succeeded_count = counts.get("CONFIRMED", 0)
    job.skipped_count = counts.get("SKIPPED", 0)
    job.failed_count = counts.get("NEEDS_REVIEW", 0)
    if counts.get("READY", 0):
        job.status = "RUNNING"
    elif counts.get("WAITING_ORACLE", 0):
        job.status = "WAITING_ORACLE"
    else:
        job.status = "COMPLETED_WITH_REVIEW" if job.failed_count or job.skipped_count else "COMPLETED"
        job.completed_at = utc_now()
    job.updated_at = utc_now()


def tick():
    with session_scope() as session:
        advance_once(session)


def recheck_legacy(session, job, *, actor: str) -> int:
    """Reopen only saved direct approvals for read-only Oracle source verification."""
    from zk_add.attendance_force_release import delivery_payload

    _lock_scheduler(session)
    if job.action != ACTION:
        raise RecoveryError("This is not a saved Oracle send run.", "RUN_MISMATCH")
    count = 0
    items = session.scalars(
        select(Item).where(
            Item.job_id == job.id,
            Item.status == "NEEDS_REVIEW",
            Item.error_code == "INVALID_EVENT_UID",
        ).order_by(Item.id).with_for_update()
    ).all()
    for item in items:
        event = session.scalar(
            select(AttendanceEvent).where(AttendanceEvent.id == item.attendance_event_id).with_for_update()
        )
        if not event or not potentially_recoverable(event.event_uid):
            continue
        decision = decision_for(session, event)
        connector = session.get(Connector, event.connector_id)
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id).with_for_update()
        )
        try:
            approved_payload = decrypt_json(decision.payload_encrypted) if decision else None
        except Exception:
            approved_payload = None
        if (
            not decision or decision.job_id != job.id or decision.item_id != item.id
            or decision.proof.get("policy") != POLICY
            or not connector or _source_digest(event, connector) != decision.proof.get("source_digest")
            or not outbox or outbox.status != "QUARANTINED_INVALID_EVENT_UID"
            or event.ords_status != "QUARANTINED_INVALID_EVENT_UID"
            or event.oracle_confirmed_at is not None
            or approved_payload is None
            or delivery_payload(session, event, connector) != approved_payload
        ):
            continue
        outbox.status = event.ords_status = "PENDING"
        outbox.next_attempt_at = outbox.last_error = None
        item.status, item.error_code, item.completed_at = "WAITING_ORACLE", None, None
        item.result = {**item.result, "reason": "Checking the already saved Oracle punch.", "needs_attention": False}
        item.updated_at = utc_now()
        count += 1
    if items and not count:
        raise RecoveryError(
            "These saved approvals or punch details changed. No Oracle check was started.",
            "RECHECK_UNAVAILABLE",
        )
    if count:
        job.status, job.completed_at, job.updated_at = "WAITING_ORACLE", None, utc_now()
        append_audit(
            session, actor=actor, action="ATTENDANCE_DIRECT_ORDS_LEGACY_RECHECK",
            target_type="attendance_recovery_job", target_id=job.job_id,
            outcome="WAITING_ORACLE", after={"rechecked_count": count},
        )
    return count


def serialize(session, job):
    counts = dict(session.execute(
        select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)
    ).all())
    waiting_attention = sum(
        bool(result.get("needs_attention"))
        for result in session.scalars(
            select(Item.result).where(Item.job_id == job.id, Item.status == "WAITING_ORACLE")
        ).all()
    )
    recheckable = sum(
        potentially_recoverable(uid)
        for (uid,) in session.execute(
            select(AttendanceEvent.event_uid)
            .join(Item, Item.attendance_event_id == AttendanceEvent.id)
            .where(Item.job_id == job.id, Item.status == "NEEDS_REVIEW", Item.error_code == "INVALID_EVENT_UID")
        ).all()
    )
    return {
        "job_id": job.job_id, "status": job.status, "created_at": job.created_at,
        "approved_at": job.scope["approval"]["at"], "actor": job.actor,
        "selected": job.requested_count, "ready": counts.get("READY", 0),
        "waiting": counts.get("WAITING_ORACLE", 0), "confirmed": counts.get("CONFIRMED", 0),
        "skipped": counts.get("SKIPPED", 0),
        "attention": counts.get("NEEDS_REVIEW", 0) + waiting_attention,
        "legacy_recheckable": recheckable,
        "completed_at": job.completed_at,
    }


def items_page(session, job, *, cursor: int = 0, limit: int = 50):
    rows = session.scalars(
        select(Item).where(Item.job_id == job.id, Item.id > cursor)
        .order_by(Item.id).limit(limit + 1)
    ).all()
    return {
        "rows": [
            {"item_id": row.item_id, "attendance_event_id": row.attendance_event_id,
             "status": "NEEDS_ATTENTION" if row.status == "WAITING_ORACLE" and row.result.get("needs_attention") else row.status,
             "reason": row.result.get("reason"),
             "error_code": row.error_code}
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }
