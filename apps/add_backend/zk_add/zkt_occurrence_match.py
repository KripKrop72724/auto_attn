"""Bounded, order-independent proposals for live/history occurrence links.

This module has no database writes or employee/Oracle authority. A caller must
prove both windows complete, qualify their exact decoder profile, and commit
accepted links against the same locked evidence revision. A source timestamp
alone, capture arrival order, or equal list lengths cannot prove a match.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Literal

from zk_add.zkt_custody import digest, occurrence_id
from zk_add.zkt_decode import DECODER_VERSION, LIVE_SIZES, MODEL_PROFILES, SOURCE_SIZES, PunchFacts, encode_time

MAX_WINDOW_RECORDS = 2048
HEX64 = re.compile(r"^[a-f0-9]{64}$")
EPOCH = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class MatchEvidenceError(ValueError):
    """Fixed error categories contain no raw bytes or personal identifiers."""


@dataclass(frozen=True)
class MatchWindow:
    terminal_serial: str
    source_epoch: str
    start_ordinal: int
    end_ordinal: int  # exclusive
    source_chain_digest: str
    evidence_revision: str
    decoder_profile: str
    decoder_version: str
    source_record_size: int
    live_record_sizes: frozenset[int]
    profile_qualified: bool
    source_complete: bool
    live_complete: bool


@dataclass(frozen=True)
class SourceOccurrence:
    terminal_serial: str
    source_epoch: str
    ordinal: int
    raw_digest: str
    decoder_profile: str
    decoder_version: str
    facts: PunchFacts | None  # None retains an undecoded source obligation.

    @property
    def identity(self) -> str:
        return occurrence_id(self.terminal_serial, self.source_epoch, self.ordinal, self.raw_digest)


@dataclass(frozen=True)
class LiveObservation:
    terminal_serial: str
    source_epoch: str
    work_key: str
    record_offset: int
    raw_digest: str
    decoder_profile: str
    decoder_version: str
    facts: PunchFacts

    @property
    def identity(self) -> str:
        # Two identical punches in one packet have different offsets. Replaying
        # its custody receipt keeps the work key and therefore the same ID.
        return digest(["zkt-live-punch-v1", self.work_key, self.record_offset, self.raw_digest])


@dataclass(frozen=True)
class Binding:
    observation_id: str
    occurrence_id: str


@dataclass(frozen=True)
class MatchDecision:
    observation_id: str
    state: Literal["PROPOSED", "EXISTING", "HELD"]
    reason: str
    occurrence_id: str | None = None
    candidate_count: int = 0


@dataclass(frozen=True)
class MatchPlan:
    evidence_revision: str
    source_chain_digest: str
    decisions: tuple[MatchDecision, ...]
    unbound_occurrences: tuple[str, ...]


def _fact_key(facts: PunchFacts) -> tuple | None:
    # Inputs normally come from the fact decoder. Check internally inconsistent
    # values as well so a caller cannot join on an unvalidated clock alone.
    try:
        valid = (type(facts.encoded_time) is int
                 and encode_time(facts.local_time) == facts.encoded_time
                 and facts.utc_time.utcoffset() is not None
                 and facts.utc_time.utcoffset().total_seconds() == 0
                 and facts.utc_time == facts.local_time
                 and type(facts.status) is int and 0 <= facts.status <= 255
                 and type(facts.punch) is int and 0 <= facts.punch <= 255)
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise MatchEvidenceError("INVALID_DECODED_FACTS")
    if facts.user_id is None:
        return None
    if (not isinstance(facts.user_id, str) or not 1 <= len(facts.user_id) <= 24
            or facts.user_id != facts.user_id.rstrip(" ")
            or any(not 32 <= ord(char) <= 126 for char in facts.user_id)):
        raise MatchEvidenceError("INVALID_DECODED_USER_REFERENCE")
    # Historical attendance_uid and live enrollment UID are intentionally not
    # interchangeable. Text references, including leading zeros, stay exact.
    return facts.user_id, facts.encoded_time, facts.status, facts.punch


def propose_matches(window: MatchWindow, *, source: tuple[SourceOccurrence, ...],
                    live: tuple[LiveObservation, ...], prior: tuple[Binding, ...] = ()) -> MatchPlan:
    """Return proposals only for unique candidates in a proved closed window.

    Work is O(source + live + prior), with an explicit 2,048-record bound on
    each input. Ambiguous many-to-many buckets never allocate a Cartesian
    product, choose the first record, or equate counts with proof. Every live
    observation receives one disposition and every source occurrence remains
    accounted for, whether or not a live association can be made.
    """
    if (not isinstance(window.terminal_serial, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", window.terminal_serial)
            or not isinstance(window.source_epoch, str) or not EPOCH.fullmatch(window.source_epoch)
            or type(window.start_ordinal) is not int or type(window.end_ordinal) is not int
            or not 0 <= window.start_ordinal <= window.end_ordinal <= 2**31
            or window.end_ordinal - window.start_ordinal > MAX_WINDOW_RECORDS
            or any(len(rows) > MAX_WINDOW_RECORDS for rows in (source, live, prior))
            or not isinstance(window.source_chain_digest, str) or not HEX64.fullmatch(window.source_chain_digest)
            or not isinstance(window.evidence_revision, str) or not HEX64.fullmatch(window.evidence_revision)
            or window.decoder_profile not in MODEL_PROFILES.values()
            or window.decoder_version != DECODER_VERSION
            or window.source_record_size not in SOURCE_SIZES
            or not isinstance(window.live_record_sizes, frozenset)
            or not window.live_record_sizes or not window.live_record_sizes <= LIVE_SIZES):
        raise MatchEvidenceError("MATCH_WINDOW_BOUNDS")
    source_by_id = {}
    source_keys = {}
    ordinals = set()
    for row in source:
        if ((row.terminal_serial, row.source_epoch) != (window.terminal_serial, window.source_epoch)
                or (row.decoder_profile, row.decoder_version) != (window.decoder_profile, window.decoder_version)
                or type(row.ordinal) is not int
                or not window.start_ordinal <= row.ordinal < window.end_ordinal
                or (row.facts is not None and row.facts.length != window.source_record_size)
                or not isinstance(row.raw_digest, str) or not HEX64.fullmatch(row.raw_digest)):
            raise MatchEvidenceError("SOURCE_WINDOW_BINDING")
        if row.ordinal in ordinals or row.identity in source_by_id:
            raise MatchEvidenceError("DUPLICATE_SOURCE_COORDINATES")
        ordinals.add(row.ordinal)
        source_by_id[row.identity] = row
        source_keys[row.identity] = _fact_key(row.facts) if row.facts else None
    if len(source) != window.end_ordinal - window.start_ordinal:
        # Invalid/unknown source rows must be included with facts=None, not
        # silently omitted to make a candidate appear unique.
        raise MatchEvidenceError("SOURCE_ORDINAL_GAP")
    live_by_id = {}
    live_keys = {}
    for row in live:
        if ((row.terminal_serial, row.source_epoch) != (window.terminal_serial, window.source_epoch)
                or (row.decoder_profile, row.decoder_version) != (window.decoder_profile, window.decoder_version)
                or not isinstance(row.work_key, str) or not HEX64.fullmatch(row.work_key)
                or not isinstance(row.raw_digest, str) or not HEX64.fullmatch(row.raw_digest)
                or type(row.record_offset) is not int or not 0 <= row.record_offset < 65536
                or row.facts.offset != row.record_offset
                or row.facts.length not in window.live_record_sizes
                or row.record_offset + row.facts.length > 65536):
            raise MatchEvidenceError("LIVE_WINDOW_BINDING")
        if row.identity in live_by_id:
            raise MatchEvidenceError("DUPLICATE_LIVE_OBSERVATION")
        live_by_id[row.identity] = row
        live_keys[row.identity] = _fact_key(row.facts)

    bound_live = {}
    bound_source = set()
    for row in prior:
        if (row.observation_id not in live_by_id or row.occurrence_id not in source_by_id
                or live_keys[row.observation_id] is None
                or live_keys[row.observation_id] != source_keys[row.occurrence_id]
                or row.observation_id in bound_live or row.occurrence_id in bound_source):
            raise MatchEvidenceError("PRIOR_BINDING_CONFLICT")
        bound_live[row.observation_id] = row.occurrence_id
        bound_source.add(row.occurrence_id)

    gate = ("PROFILE_QUALIFICATION_REQUIRED" if window.profile_qualified is not True else
            "SOURCE_WINDOW_INCOMPLETE" if window.source_complete is not True else
            "LIVE_WINDOW_INCOMPLETE" if window.live_complete is not True else
            "SOURCE_WINDOW_HAS_UNRESOLVED_FACTS" if any(key is None for key in source_keys.values()) else None)
    available = defaultdict(list)
    pending = defaultdict(list)
    all_source_keys = set(source_keys.values())
    for identity, key in source_keys.items():
        if identity not in bound_source and key is not None:
            available[key].append(identity)
    for identity, key in live_keys.items():
        if identity not in bound_live and key is not None:
            pending[key].append(identity)

    decisions = []
    proposed_source = set()
    for identity, key in live_keys.items():
        if identity in bound_live:
            # Existing links remain recorded even when qualification is later
            # withdrawn, but are not re-certified by this proposal function.
            decisions.append(MatchDecision(identity, "EXISTING", "RETAINED_BINDING", bound_live[identity]))
            continue
        candidates = available.get(key, ())
        reason = gate
        if reason is None:
            reason = ("MISSING_USER_REFERENCE" if key is None else
                      "SOURCE_OCCURRENCE_ALREADY_BOUND" if not candidates and key in all_source_keys else
                      "NO_SOURCE_CANDIDATE" if not candidates else
                      "AMBIGUOUS_SOURCE_OCCURRENCES" if len(candidates) > 1 else
                      "COMPETING_LIVE_OBSERVATIONS" if len(pending[key]) > 1 else None)
        if reason:
            decisions.append(MatchDecision(identity, "HELD", reason, candidate_count=len(candidates)))
        else:
            occurrence = candidates[0]
            proposed_source.add(occurrence)
            decisions.append(MatchDecision(identity, "PROPOSED", "UNIQUE_FULL_FACTS_IN_CLOSED_WINDOW",
                                           occurrence, 1))
    return MatchPlan(window.evidence_revision, window.source_chain_digest, tuple(decisions),
                     tuple(identity for identity in source_by_id if identity not in bound_source | proposed_source))
