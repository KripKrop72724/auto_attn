"""Re-check stored Hikvision events after an event-code policy change.

A policy change only classifies events ADD sees after it. Stored history that
the new mapping now recognises (a door event newly marked non-attendance, a
fingerprint code newly accepted) stays held until the terminal is rescanned,
and a full rescan of a large terminal takes days.

This sweep applies the rule a rescan applies to a re-observed event: evidence
still QUALIFICATION_PENDING or UNCLASSIFIED_SOURCE is delivered again under the
current policy (``preserve_observation``'s re-observation branch). It reads
ADD's own encrypted copy and never contacts the terminal. Every other
disposition, and evidence from another terminal serial or source epoch, is left
exactly as it is.

``configure_policy`` sets ``recheck_after_id`` to 0. Each tick advances one
bounded batch per connector in its own transaction, commits the cursor with
the batch, and clears it once that connector's evidence is exhausted, so a
restart resumes from the last committed batch. Open reconciliation jobs pick
up the new dispositions through the existing assurance refresh.
"""

from __future__ import annotations

import json

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.crypto import decrypt_text
from zk_add.db import session_scope
from zk_add.hikvision_delivery import HikvisionPolicy, deliver_observation
from zk_add.hikvision_evidence import HikvisionEvidence
from zk_add.hikvision_probe import ProbeError, decode_body
from zk_add.hikvision_protocol import SourceIdentityError, normalize_observation
from zk_add.models import Connector

RECHECK_DISPOSITIONS = ("QUALIFICATION_PENDING", "UNCLASSIFIED_SOURCE")
BATCH = 500


def queue_recheck(policy: HikvisionPolicy) -> None:
    """Start (or restart) the sweep for the policy's current mapping revision."""
    policy.recheck_after_id = 0
    policy.recheck_counts = {"mapping_revision": policy.mapping_revision, "checked": 0}


def _observation(evidence: HikvisionEvidence):
    try:
        text = decrypt_text(evidence.raw_encrypted)
        if text is None:
            return None, None
        data = decode_body(text.encode())
        return data, normalize_observation(
            data, terminal_serial=evidence.terminal_serial, source_epoch=evidence.source_epoch,
        )
    except (InvalidToken, ProbeError, SourceIdentityError, UnicodeError, json.JSONDecodeError):
        return None, None


def recheck_once(session: Session, connector_id: int, *, batch: int = BATCH) -> bool:
    """Advance one batch for one connector. Returns True while work remains."""
    from zk_add.audit import append_audit

    policy = session.scalar(
        select(HikvisionPolicy)
        .where(HikvisionPolicy.connector_id == connector_id)
        .with_for_update(skip_locked=True)
    )
    if policy is None or policy.recheck_after_id is None or not policy.enabled:
        return False
    connector = session.get(Connector, connector_id)
    if connector is None or connector.firmware_family != "hikvision" or not connector.zkt_device:
        return False
    rows = session.scalars(
        select(HikvisionEvidence)
        .where(
            HikvisionEvidence.connector_id == connector_id,
            HikvisionEvidence.id > policy.recheck_after_id,
            HikvisionEvidence.disposition.in_(RECHECK_DISPOSITIONS),
            HikvisionEvidence.terminal_serial == policy.terminal_serial,
            HikvisionEvidence.source_epoch == policy.source_epoch,
        )
        .order_by(HikvisionEvidence.id)
        .limit(batch)
    ).all()
    counts = dict(policy.recheck_counts or {})
    for evidence in rows:
        data, observation = _observation(evidence)
        if observation is None:
            outcome = "UNREADABLE"
        else:
            try:
                deliver_observation(session, connector, evidence, observation, data)
            except ValueError as exc:
                # Raised before any change (ATTENDANCE_CONNECTOR_CONFLICT); the
                # event stays held for review instead of wedging the sweep.
                outcome = str(exc)[:60]
            else:
                outcome = evidence.disposition
        counts[outcome] = counts.get(outcome, 0) + 1
        counts["checked"] = counts.get("checked", 0) + 1
    session.flush()
    policy.recheck_counts = counts
    if len(rows) == batch:
        policy.recheck_after_id = rows[-1].id
        return True
    policy.recheck_after_id = None
    append_audit(
        session,
        actor="system",
        action="HIKVISION_POLICY_RECHECK_COMPLETED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="COMPLETED",
        after=counts,
    )
    return False


def tick() -> None:
    """One bounded batch for every connector with a pending re-check."""
    with session_scope() as session:
        pending = session.scalars(
            select(HikvisionPolicy.connector_id).where(
                HikvisionPolicy.recheck_after_id.is_not(None),
                HikvisionPolicy.enabled == True,  # noqa: E712
            ).order_by(HikvisionPolicy.connector_id)
        ).all()
    for connector_id in pending:
        with session_scope() as session:
            recheck_once(session, connector_id)
