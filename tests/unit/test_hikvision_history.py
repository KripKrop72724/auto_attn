from dataclasses import replace

import pytest

from zk_add.hikvision_history import SerialCheckpoint, CoverageError, coverage_certificate


def page(checkpoint, serials, **changes):
    request = checkpoint.request()
    remaining = checkpoint.retained_count - checkpoint.committed_count
    result = {"searchID": request["AcsEventCond"]["searchID"],
              "numOfMatches": len(serials), "totalMatches": remaining,
              "responseStatusStrg": "OK" if len(serials) == remaining else "MORE",
              "InfoList": [{"serialNo": serial, "major": 5, "minor": 75} for serial in serials],
              **changes}
    return request, {"AcsEvent": result}


def test_resume_uses_source_serial_and_new_session_not_saved_offset():
    start = SerialCheckpoint(100, 110, 3)
    request, response = page(start, [100, 105])
    rows, staged = start.stage_page(request, response)
    assert len(rows) == 2 and start.committed_count == 0
    assert staged.request()["AcsEventCond"]["beginSerialNo"] == 106
    assert staged.request()["AcsEventCond"]["searchResultPosition"] == 0
    assert staged.request()["AcsEventCond"]["searchID"] != request["AcsEventCond"]["searchID"]
    # Losing an ACK or power before committing the page replays identical data.
    replay_request, replay_response = page(start, [100, 105])
    assert start.stage_page(replay_request, replay_response)[1] == staged
    request, response = page(staged, [110])
    end = staged.stage_page(request, response)[1]
    assert end.enumeration_complete
    with pytest.raises(CoverageError):
        end.request()


@pytest.mark.parametrize("serials,changes", [
    ([100, 100], {}), ([101], {}), ([111], {}), ([True], {}),
    ([100], {"totalMatches": 2}), ([100], {"responseStatusStrg": "OK"}),
    ([100], {"searchID": "expired"}), ([], {"responseStatusStrg": "NO MATCH"}),
])
def test_conflicts_retention_loss_and_session_expiry_do_not_advance(serials, changes):
    start = SerialCheckpoint(100, 110, 3)
    request, response = page(start, serials, **changes)
    with pytest.raises(CoverageError):
        start.stage_page(request, response)
    assert start.committed_count == 0 and start.last_serial == 0


def test_counts_alone_cannot_seal_source_or_claim_oracle_completion():
    start = SerialCheckpoint(100, 110, 3)
    request, response = page(start, [100, 105, 110])
    end = start.stage_page(request, response)[1]
    anchors = dict(first_anchor_before="a" * 64, first_anchor_after="a" * 64,
                   last_anchor_before="b" * 64, last_anchor_after="b" * 64)
    assert coverage_certificate(end, end, **anchors)["oracle_assurance"] == "NOT_EVALUATED"
    for other in (start, replace(end, chain_digest="f" * 64)):
        with pytest.raises(CoverageError):
            coverage_certificate(end, other, **anchors)
    anchors["last_anchor_after"] = "c" * 64
    with pytest.raises(CoverageError):
        coverage_certificate(end, end, **anchors)


def test_150000_events_with_gaps_and_same_times_are_all_enumerated():
    state = SerialCheckpoint(1, 299999, 150000)
    for offset in range(0, 150000, 20):
        request, response = page(state, [1 + 2 * i for i in range(offset, offset + 20)])
        state = state.stage_page(request, response)[1]
    assert state.enumeration_complete and state.committed_count == 150000


@pytest.mark.parametrize("field,value", [("major", 5), ("minor", 75), ("major", False),
                                        ("picEnable", True), ("searchID", 42)])
def test_filtered_or_invalid_request_cannot_certify_all_source_records(field, value):
    state = SerialCheckpoint(100, 110, 3)
    request, response = page(state, [100, 105, 110])
    request["AcsEventCond"][field] = value
    with pytest.raises(CoverageError):
        state.stage_page(request, response)


@pytest.mark.parametrize("payload,response", [(None, {}), ({}, []), ([], None)])
def test_malformed_envelopes_raise_coverage_error(payload, response):
    with pytest.raises(CoverageError, match="INVALID_SEARCH_ENVELOPE"):
        SerialCheckpoint(100, 110, 3).stage_page(payload, response)


@pytest.mark.parametrize("changes", [
    {"chain_digest": "a" * 64},
    {"committed_count": 3, "last_serial": 105},
    {"committed_count": 2, "last_serial": 110},
])
def test_inconsistent_restored_checkpoint_is_rejected(changes):
    with pytest.raises(CoverageError, match="INVALID_SERIAL_CHECKPOINT"):
        replace(SerialCheckpoint(100, 110, 3), **changes)
