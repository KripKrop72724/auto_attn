"""One switch for every animation.

Screenshots freeze animations at a fixed moment, so the pictures are the same
on every run and show the "on" frame of blinking lights.
"""

from __future__ import annotations

_frozen = False


def frozen() -> bool:
    return _frozen


def freeze() -> None:
    global _frozen
    _frozen = True


def unfreeze() -> None:
    global _frozen
    _frozen = False
