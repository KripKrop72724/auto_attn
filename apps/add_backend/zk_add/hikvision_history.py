"""Bounded serial-search checkpoints, independent of ZKT buffer ordinals.

Each request starts a new search at position zero. Only a durable source serial
checkpoint survives reconnects; a terminal's temporary search cursor does not.
The caller must persist the returned page and checkpoint in one transaction.
Two matching enumerations and boundary readbacks are required for coverage.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from uuid import uuid4


class CoverageError(ValueError):
    pass


def record_digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class SerialCheckpoint:
    first_serial: int
    cutoff_serial: int
    retained_count: int
    committed_count: int = 0
    last_serial: int = 0
    chain_digest: str = "0" * 64

    def __post_init__(self):
        integers = (self.first_serial, self.cutoff_serial, self.retained_count,
                    self.committed_count, self.last_serial)
        if (any(type(v) is not int for v in integers)
                or not 1 <= self.first_serial <= self.cutoff_serial <= 3_000_000_000
                or not 1 <= self.retained_count <= 150000
                or not 0 <= self.committed_count <= self.retained_count
                or (self.committed_count == 0 and self.last_serial != 0)
                or (self.committed_count and not self.first_serial <= self.last_serial <= self.cutoff_serial)
                or not isinstance(self.chain_digest, str) or len(self.chain_digest) != 64
                or any(c not in "0123456789abcdef" for c in self.chain_digest)
                or (self.committed_count == 0 and self.chain_digest != "0" * 64)
                or (self.committed_count == self.retained_count and self.last_serial != self.cutoff_serial)
                or (self.committed_count < self.retained_count and self.last_serial == self.cutoff_serial)):
            raise CoverageError("INVALID_SERIAL_CHECKPOINT")

    @property
    def enumeration_complete(self) -> bool:
        return self.committed_count == self.retained_count and self.last_serial == self.cutoff_serial

    def request(self, page_size: int = 20) -> dict:
        if type(page_size) is not int or not 1 <= page_size <= 30 or self.enumeration_complete:
            raise CoverageError("INVALID_SERIAL_REQUEST")
        return {"AcsEventCond": {
            "searchID": uuid4().hex, "searchResultPosition": 0,
            "maxResults": page_size, "major": 0, "minor": 0, "picEnable": False,
            "beginSerialNo": self.last_serial + 1 if self.last_serial else self.first_serial,
            "endSerialNo": self.cutoff_serial,
        }}

    def stage_page(self, request: dict, response: dict) -> tuple[list[dict], "SerialCheckpoint"]:
        """Validate without mutating this checkpoint, including on partial failures."""
        if not isinstance(request, dict) or not isinstance(response, dict):
            raise CoverageError("INVALID_SEARCH_ENVELOPE")
        if self.enumeration_complete:
            raise CoverageError("ENUMERATION_ALREADY_COMPLETE")
        condition = request.get("AcsEventCond", {})
        page = response.get("AcsEvent", {})
        if not isinstance(condition, dict) or not isinstance(page, dict):
            raise CoverageError("INVALID_SEARCH_ENVELOPE")
        if (condition.get("beginSerialNo") != (self.last_serial + 1 if self.last_serial else self.first_serial)
                or condition.get("endSerialNo") != self.cutoff_serial
                or condition.get("searchResultPosition") != 0
                or type(condition.get("major")) is not int or condition["major"] != 0
                or type(condition.get("minor")) is not int or condition["minor"] != 0
                or condition.get("picEnable") is not False
                or type(condition.get("maxResults")) is not int
                or not 1 <= condition["maxResults"] <= 30
                or not isinstance(condition.get("searchID"), str) or not condition["searchID"]
                or page.get("searchID") != condition["searchID"]):
            raise CoverageError("SEARCH_SCOPE_OR_SESSION_CHANGED")
        rows = page.get("InfoList", [])
        count, total = page.get("numOfMatches"), page.get("totalMatches")
        if (type(count) is not int or type(total) is not int
                or not isinstance(rows, list) or count != len(rows)
                or not 1 <= count <= condition["maxResults"]
                or total != self.retained_count - self.committed_count or count > total):
            raise CoverageError("RETAINED_SCOPE_CHANGED")
        if page.get("responseStatusStrg") != ("OK" if count == total else "MORE"):
            raise CoverageError("INCONSISTENT_SEARCH_COMPLETION")
        previous = self.last_serial
        chain = self.chain_digest
        for row in rows:
            serial = row.get("serialNo") if isinstance(row, dict) else None
            if (type(serial) is not int or not self.first_serial <= serial <= self.cutoff_serial
                    or serial <= previous):
                raise CoverageError("SERIAL_REUSE_ORDER_OR_SCOPE_CONFLICT")
            if not previous and serial != self.first_serial:
                raise CoverageError("RETAINED_FIRST_BOUNDARY_LOST")
            chain = hashlib.sha256(bytes.fromhex(chain) + bytes.fromhex(record_digest(row))).hexdigest()
            previous = serial
        if count == total and previous != self.cutoff_serial:
            raise CoverageError("RETAINED_LAST_BOUNDARY_LOST")
        return rows, replace(self, committed_count=self.committed_count + count,
                             last_serial=previous, chain_digest=chain)


def coverage_certificate(first_pass: SerialCheckpoint, second_pass: SerialCheckpoint,
                         *, first_anchor_before: str, first_anchor_after: str,
                         last_anchor_before: str, last_anchor_after: str) -> dict:
    anchors = (first_anchor_before, first_anchor_after, last_anchor_before, last_anchor_after)
    if (not first_pass.enumeration_complete or not second_pass.enumeration_complete
            or first_pass != second_pass
            or any(not isinstance(a, str) or len(a) != 64
                   or any(c not in "0123456789abcdef" for c in a) for a in anchors)
            or first_anchor_before != first_anchor_after or last_anchor_before != last_anchor_after):
        raise CoverageError("INCOMPLETE_OR_CHANGING_SOURCE_COVERAGE")
    return {
        "source_protocol": "hikvision-isapi-v1", "search_strategy": "serial-seek-v1",
        "first_serial": first_pass.first_serial, "cutoff_serial": first_pass.cutoff_serial,
        "record_count": first_pass.committed_count, "chain_digest": first_pass.chain_digest,
        "matching_passes": 2, "first_anchor": first_anchor_before, "last_anchor": last_anchor_before,
        "oracle_assurance": "NOT_EVALUATED",
    }
