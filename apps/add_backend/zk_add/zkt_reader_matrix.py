"""Reviewed signed-reader identities, not field qualification or runtime claims.

One canonical policy supplies firmware, signing and ADD. Empty policy compiles
an explicitly blocked writer for tests; it cannot sign, publish or admit one.
Populating it requires a reviewed source change after actual signed artifacts
exist. Each target still needs its own matching stored BRIDGE_READY evidence.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

VERSIONS = ("2.6.21", "2.6.22")
MATRIX_ID = "zkt-2.7.0-readers-v1"
MATRIX_PATH = Path(__file__).with_suffix(".json")
ENTRY_KEYS = frozenset(("version", "release_id", "application_sha256", "artifact_sha256",
                        "source_sha", "signing_key_id"))
MARKER_PREFIX = "ZONE_STORAGE_CONTRACT_V5:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:MATRIX="


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Reader matrix contains a duplicate field.")
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def validate_matrix(value, *, require_pinned=False):
    if (type(value) is not dict or set(value) != {"schema_version", "matrix_id", "state", "readers"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("matrix_id") != MATRIX_ID or type(value.get("readers")) is not list):
        raise ValueError("Reader matrix schema or identity is invalid.")
    rows = value["readers"]
    if value.get("state") == "BLOCKED" and not rows and not require_pinned:
        return deepcopy(value)
    if value.get("state") != "PINNED" or len(rows) != len(VERSIONS):
        raise ValueError("Reader matrix is unpopulated or incomplete.")
    for version, row in zip(VERSIONS, rows):
        if (type(row) is not dict or set(row) != ENTRY_KEYS
                or row.get("version") != version or row.get("release_id") != "zone-lite-" + version):
            raise ValueError("Reader matrix entries must have their exact ordered identities.")
        if (type(row["signing_key_id"]) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", row["signing_key_id"])):
            raise ValueError("Reader matrix signing key identity is invalid.")
        for key in ENTRY_KEYS - {"version", "release_id", "signing_key_id"}:
            length = 40 if key == "source_sha" else 64
            digest = row[key]
            if (type(digest) is not str or not re.fullmatch("[0-9a-f]{%d}" % length, digest)
                    or len(set(digest)) < 2):
                raise ValueError("Reader matrix digest is invalid or a placeholder.")
    for key in ("application_sha256", "artifact_sha256"):
        if len({row[key] for row in rows}) != len(rows):
            raise ValueError("Reader matrix image identities must be distinct.")
    return deepcopy(value)


def load_matrix(*, require_pinned=False):
    raw = MATRIX_PATH.read_bytes()
    if len(raw) > 4096:
        raise ValueError("Reader matrix exceeds its fixed bound.")
    return validate_matrix(json.loads(raw, object_pairs_hook=_object), require_pinned=require_pinned)


def matrix_hash(matrix):
    return hashlib.sha256(canonical(validate_matrix(matrix))).hexdigest()


def matrix_marker(matrix):
    return MARKER_PREFIX + matrix_hash(matrix)


def minimum_reader_version(matrix):
    """The generic semantic-version floor is independent of preferred order.

    Exact matrix/qualification checks remain mandatory after this coarse gate.
    """
    rows = validate_matrix(matrix, require_pinned=True)["readers"]
    return min((row["version"] for row in rows), key=lambda version: tuple(map(int, version.split("."))))


def writer_matrix_contract(matrix=None):
    matrix = load_matrix(require_pinned=True) if matrix is None else validate_matrix(matrix, require_pinned=True)
    rows = matrix["readers"]
    return {"schema_version": 5, "read_format": 2, "reader_mask": 63, "write_format": 1,
            "journal_read_format": 1, "journal_write_format": 1, "journal_reader_mask": 63,
            "journal_capture": True, "delivery_authority": "ADD",
            "reader_matrix": matrix, "reader_matrix_sha256": matrix_hash(matrix),
            "allowed_bootstrap_versions": list(VERSIONS),
            "allowed_bootstrap_images": {row["version"]: row["application_sha256"] for row in rows}}


def selected_reader(contract, version, application_sha256):
    """Match an already validated contract. No version-only or fallback match."""
    if type(contract) is not dict or contract.get("schema_version") != 5:
        return None
    try:
        expected = writer_matrix_contract(contract.get("reader_matrix"))
    except (TypeError, ValueError):
        return None
    if canonical(contract) != canonical(expected):
        return None
    return next((deepcopy(row) for row in expected["reader_matrix"]["readers"]
                 if row["version"] == version and row["application_sha256"] == application_sha256), None)
