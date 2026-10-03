"""Counterexamples to timestamp/arrival-order matching; no real qualification."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import itertools
import random
import struct

import pytest

from zk_add.zkt_decode import DECODER_VERSION, MODEL_PROFILES, PAKISTAN_TIME, decode_live_record, decode_source, encode_time
from zk_add.zkt_occurrence_match import (Binding, LiveObservation, MatchEvidenceError, MatchWindow,
    SourceOccurrence, MAX_WINDOW_RECORDS, propose_matches)

SERIAL = "synthetic-terminal"
EPOCH = "11111111-2222-3333-4444-555555555555"
PROFILE = MODEL_PROFILES["G3"]
LOCAL = datetime(2026, 10, 3, 9, 14, 22, tzinfo=PAKISTAN_TIME)


def sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def window(count, **changes):
    return replace(MatchWindow(SERIAL, EPOCH, 0, count, sha("source chain"), sha("revision"),
                              PROFILE, DECODER_VERSION, 40, frozenset({32}), True, True, True), **changes)


def source(ordinal=0, *, user="123", local=LOCAL, status=7, punch=2, uid=456):
    raw = bytearray(40)
    struct.pack_into("<H", raw, 0, uid)
    raw[2:2 + len(user)] = user.encode()
    raw[26] = status
    struct.pack_into("<I", raw, 27, encode_time(local))
    raw[31] = punch
    return SourceOccurrence(SERIAL, EPOCH, ordinal, hashlib.sha256(raw).hexdigest(),
                            PROFILE, DECODER_VERSION, decode_source(raw, record_size=40))


def live(index=0, *, user="123", local=LOCAL, status=7, punch=2, work="packet", offset=None):
    raw = bytearray(32)
    raw[:len(user)] = user.encode()
    raw[24:32] = bytes([status, punch, local.year - 2000, local.month, local.day,
                        local.hour, local.minute, local.second])
    offset = 8 + index * 32 if offset is None else offset
    return LiveObservation(SERIAL, EPOCH, sha(work), offset, hashlib.sha256(raw).hexdigest(),
                           PROFILE, DECODER_VERSION, decode_live_record(raw, record_size=32, offset=offset))


def test_different_wire_layouts_match_exact_facts_without_attendance_uid_join():
    history, observation = source(uid=999), live()
    plan = propose_matches(window(1), source=(history,), live=(observation,))
    assert history.raw_digest != observation.raw_digest
    assert history.facts.attendance_uid == 999 and observation.facts.attendance_uid is None
    assert plan.decisions[0].state == "PROPOSED"
    assert plan.decisions[0].occurrence_id == history.identity
    assert plan.evidence_revision == window(1).evidence_revision
    assert plan.source_chain_digest == window(1).source_chain_digest
    assert plan.unbound_occurrences == ()


@pytest.mark.parametrize("source_count,live_count,reason", [
    (2, 2, "AMBIGUOUS_SOURCE_OCCURRENCES"), (2, 1, "AMBIGUOUS_SOURCE_OCCURRENCES"),
    (1, 2, "COMPETING_LIVE_OBSERVATIONS"), (0, 1, "NO_SOURCE_CANDIDATE"),
])
def test_equal_counts_or_arrival_order_do_not_prove_repeated_same_second_punches(source_count, live_count, reason):
    history = tuple(source(i) for i in range(source_count))
    observations = tuple(live(i) for i in range(live_count))
    assert len({row.identity for row in history}) == source_count
    assert len({row.identity for row in observations}) == live_count
    for sources in itertools.permutations(history):
        for lives in itertools.permutations(observations):
            plan = propose_matches(window(source_count), source=sources, live=lives)
            assert len(plan.decisions) == live_count
            assert {(row.state, row.reason) for row in plan.decisions} == {("HELD", reason)}
            assert set(plan.unbound_occurrences) == {row.identity for row in history}


@pytest.mark.parametrize("field,value", [("user", "00123"), ("status", 1), ("punch", 1),
                                        ("local", LOCAL + timedelta(seconds=1))])
def test_time_alone_or_normalized_user_id_never_links_different_facts(field, value):
    plan = propose_matches(window(1), source=(source(),), live=(live(**{field: value}),))
    assert plan.decisions[0].reason == "NO_SOURCE_CANDIDATE"


def test_same_second_distinct_users_and_punch_codes_remain_individually_matchable():
    inputs = [dict(user="123", punch=1), dict(user="123", punch=2), dict(user="00123", punch=2)]
    history = tuple(source(i, **values) for i, values in enumerate(inputs))
    observations = tuple(live(i, **values) for i, values in enumerate(inputs))
    for order in itertools.permutations(observations):
        plan = propose_matches(window(3), source=history[::-1], live=order)
        assert {row.observation_id: row.occurrence_id for row in plan.decisions} == {
            observation.identity: occurrence.identity for observation, occurrence in zip(observations, history)}


@pytest.mark.parametrize("gate,reason", [
    ("profile_qualified", "PROFILE_QUALIFICATION_REQUIRED"),
    ("source_complete", "SOURCE_WINDOW_INCOMPLETE"), ("live_complete", "LIVE_WINDOW_INCOMPLETE"),
])
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_unqualified_or_unclosed_windows_cannot_propose_even_plausible_unique_matches(gate, reason, value):
    plan = propose_matches(window(1, **{gate: value}), source=(source(),), live=(live(),))
    assert plan.decisions[0].state == "HELD" and plan.decisions[0].reason == reason


def test_omitted_or_uninterpreted_source_record_cannot_make_a_match_appear_unique():
    with pytest.raises(MatchEvidenceError, match="SOURCE_ORDINAL_GAP"):
        propose_matches(window(2), source=(source(),), live=(live(),))
    for unresolved in (None, replace(source(1).facts, user_id=None)):
        history = (source(), replace(source(1), facts=unresolved))
        result = propose_matches(window(2), source=history, live=(live(),))
        assert result.decisions[0].reason == "SOURCE_WINDOW_HAS_UNRESOLVED_FACTS"
        assert len(result.unbound_occurrences) == 2
    # The held association window does not corrupt a separate closed interval.
    assert propose_matches(window(1), source=(source(),), live=(live(),)).decisions[0].state == "PROPOSED"


def test_missing_live_reference_never_joins_historical_attendance_uid():
    observation = live()
    observation = replace(observation, facts=replace(observation.facts, user_id=None, attendance_uid=456))
    result = propose_matches(window(1), source=(source(uid=456),), live=(observation,))
    assert result.decisions[0].reason == "MISSING_USER_REFERENCE"


def test_retained_one_to_one_binding_prevents_second_observation_consuming_same_occurrence():
    history, observations = (source(),), (live(), live(1))
    prior = Binding(observations[0].identity, history[0].identity)
    result = propose_matches(window(1), source=history, live=observations, prior=(prior,))
    assert result.decisions[0].state == "EXISTING"
    assert result.decisions[1].reason == "SOURCE_OCCURRENCE_ALREADY_BOUND"
    assert not result.unbound_occurrences
    replay = propose_matches(window(1, profile_qualified=False), source=history, live=observations, prior=(prior,))
    assert replay.decisions[0].state == "EXISTING"  # Recorded, never re-certified.
    assert replay.decisions[1].reason == "PROFILE_QUALIFICATION_REQUIRED"


def test_independently_proved_prior_link_can_disambiguate_remaining_same_second_occurrence():
    history, observations = (source(), source(1)), (live(), live(1))
    result = propose_matches(window(2), source=history, live=observations,
                             prior=(Binding(observations[1].identity, history[0].identity),))
    assert result.decisions[0].occurrence_id == history[1].identity
    assert result.decisions[0].state == "PROPOSED" and result.decisions[1].state == "EXISTING"


@pytest.mark.parametrize("field,value", [("terminal_serial", "another-terminal"),
    ("source_epoch", "22222222-2222-3333-4444-555555555555"),
    ("decoder_profile", MODEL_PROFILES["MB40-VL/ID"]), ("decoder_version", "future")])
def test_source_and_live_binding_changes_are_not_cross_matched(field, value):
    with pytest.raises(MatchEvidenceError, match="SOURCE_WINDOW_BINDING"):
        propose_matches(window(1), source=(replace(source(), **{field: value}),), live=(live(),))
    with pytest.raises(MatchEvidenceError, match="LIVE_WINDOW_BINDING"):
        propose_matches(window(1), source=(source(),), live=(replace(live(), **{field: value}),))


def test_prior_conflicts_and_duplicate_inputs_fail_without_partial_proposals():
    history, observations = (source(), source(1, user="456")), (live(), live(1, user="456"))
    for priors in ((Binding(observations[0].identity, history[1].identity),),
                   (Binding(sha("missing"), history[0].identity),),
                   (Binding(observations[0].identity, history[0].identity),) * 2):
        with pytest.raises(MatchEvidenceError, match="PRIOR_BINDING_CONFLICT"):
            propose_matches(window(2), source=history, live=observations, prior=priors)
    with pytest.raises(MatchEvidenceError, match="DUPLICATE_SOURCE_COORDINATES"):
        propose_matches(window(2), source=(history[0], history[0]), live=observations)
    with pytest.raises(MatchEvidenceError, match="DUPLICATE_LIVE_OBSERVATION"):
        propose_matches(window(2), source=history, live=(observations[0], observations[0]))


@pytest.mark.parametrize("changes", [dict(start_ordinal=-1), dict(end_ordinal=2**31 + 1),
    dict(end_ordinal=MAX_WINDOW_RECORDS + 1), dict(source_epoch=None), dict(source_chain_digest="bad"),
    dict(evidence_revision="bad"), dict(decoder_profile="unqualified"), dict(decoder_version="unknown"),
    dict(source_record_size=52), dict(live_record_sizes=frozenset({40})), dict(live_record_sizes=frozenset())])
def test_window_contract_and_resource_bounds(changes):
    with pytest.raises(MatchEvidenceError, match="MATCH_WINDOW_BOUNDS"):
        propose_matches(window(0, **changes), source=(), live=())


def test_fact_clock_corruption_and_live_offset_cannot_be_accepted():
    observation = live()
    with pytest.raises(MatchEvidenceError, match="INVALID_DECODED_FACTS"):
        propose_matches(window(1), source=(source(),), live=(replace(observation,
            facts=replace(observation.facts, encoded_time=0)),))
    with pytest.raises(MatchEvidenceError, match="LIVE_WINDOW_BINDING"):
        propose_matches(window(1), source=(source(),), live=(replace(observation, record_offset=100),))
    with pytest.raises(MatchEvidenceError, match="SOURCE_WINDOW_BINDING"):
        propose_matches(window(1, source_record_size=16), source=(source(),), live=(live(),))
    with pytest.raises(MatchEvidenceError, match="LIVE_WINDOW_BINDING"):
        propose_matches(window(1, live_record_sizes=frozenset({12})), source=(source(),), live=(live(),))


def test_two_thousand_identical_facts_never_build_a_cartesian_candidate_matrix():
    history = tuple(source(i) for i in range(MAX_WINDOW_RECORDS))
    # Spread observations across immutable packet work keys within wire bounds.
    observations = tuple(live(work=i) for i in range(MAX_WINDOW_RECORDS))
    result = propose_matches(window(MAX_WINDOW_RECORDS), source=history, live=observations)
    assert len(result.decisions) == len(result.unbound_occurrences) == MAX_WINDOW_RECORDS
    assert all(row.state == "HELD" and row.candidate_count == MAX_WINDOW_RECORDS for row in result.decisions)


def test_seeded_mixed_replay_population_preserves_a_bijection_without_losing_held_records():
    randomizer = random.Random(270)
    for count in (1, 3, 30, 100, 1000):
        users = [str(randomizer.randrange(max(2, count // 2))) for _ in range(count)]
        history = [source(i, user=user) for i, user in enumerate(users)]
        observations = [live(work=i, user=user) for i, user in enumerate(users)]
        randomizer.shuffle(history)
        randomizer.shuffle(observations)
        result = propose_matches(window(count), source=tuple(history), live=tuple(observations))
        proposed = [row.occurrence_id for row in result.decisions if row.state == "PROPOSED"]
        assert len(proposed) == len(set(proposed))
        assert len(result.decisions) == count
        assert set(proposed) | set(result.unbound_occurrences) == {row.identity for row in history}
        assert not set(proposed) & set(result.unbound_occurrences)
        assert all(row.candidate_count == 1 for row in result.decisions if row.state == "PROPOSED")
