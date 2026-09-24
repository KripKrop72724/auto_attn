"""Narrow read-only recovery for legacy punch IDs damaged in one four-byte block.

These IDs are never eligible for an Oracle insert.  Only a content-verified
match against an already stored, valid Oracle event UID can settle them.
"""

import re


_HEX = re.compile(r"[0-9a-f]")
_VALID = re.compile(r"[0-9a-f]{64}")
_DAMAGED = {"?", "\b", "\x14"}


def potentially_recoverable(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    bad = [i for i, char in enumerate(value) if not _HEX.fullmatch(char)]
    return (
        3 <= len(bad) <= 4
        and bad[0] >= 24
        and bad[-1] <= 35
        and bad[-1] - bad[0] <= 3
        and all(value[i] in _DAMAGED for i in bad)
    )


def matches_original(damaged: object, original: object) -> bool:
    if not potentially_recoverable(damaged) or not isinstance(original, str) or not _VALID.fullmatch(original):
        return False
    differences = [i for i, (left, right) in enumerate(zip(damaged, original)) if left != right]
    return (
        len(differences) == 4
        and differences[0] >= 24
        and differences[-1] <= 35
        and differences[-1] - differences[0] == 3
        and sum(damaged[i] in _DAMAGED for i in differences) >= 3
    )
