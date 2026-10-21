"""Bounded error labels; never copy exception messages or rejected PII."""
import re

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

# An upper-case identifier with at least one underscore. Device values that
# could reach an exception message (CNICs, names, user IDs, digests) do not
# have this shape, so only a deliberate code is ever recorded.
REASON_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
REASON_CODE_MAX_LENGTH = 80
UNCLASSIFIED_REASON = "UNCLASSIFIED_VALUE_ERROR"


class EvidenceRejected(ValueError):
    """A ValueError carrying a stable, non-secret reason code for operators.

    The message is kept for HTTP callers and tests. Only ``reason_code`` is
    ever recorded with a device rejection.
    """

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        if len(reason_code) > REASON_CODE_MAX_LENGTH or not REASON_CODE.fullmatch(reason_code):
            raise TypeError("A rejection reason must be a bounded upper-case code.")
        super().__init__(message or reason_code)
        self.reason_code = reason_code

    def __reduce__(self):
        # ``args`` holds only the message, so the default reduction would
        # rebuild without a reason. Copies and pickles keep both.
        state = {key: value for key, value in self.__dict__.items() if key != "reason_code"}
        return type(self), (self.reason_code, *self.args[:1]), state or None


def rejection_category(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "SCHEMA_INVALID"
    if isinstance(error, IntegrityError):
        return "CONSTRAINT_CONFLICT"
    if isinstance(error, OperationalError):
        return "DATABASE_UNAVAILABLE"
    if isinstance(error, TimeoutError):
        return "OPERATION_TIMEOUT"
    if isinstance(error, ValueError):
        return "EVIDENCE_INVALID"
    return "INTERNAL_ERROR"


def rejection_reason(error: Exception) -> str | None:
    """Why ADD refused evidence, as a code; never the message text itself.

    A plain ValueError whose whole message is already a code keeps it. Any
    other message may quote device values, so it is reported only as
    UNCLASSIFIED_VALUE_ERROR together with ``rejection_site``. A schema error
    has a reason only when a validator raised a PydanticCustomError whose
    type is a code (queue_evidence.verify_bytes); ordinary field errors keep
    their field paths instead.
    """
    if isinstance(error, ValidationError):
        for row in error.errors(include_input=False, include_url=False):
            kind = row.get("type")
            if isinstance(kind, str) and len(kind) <= REASON_CODE_MAX_LENGTH and REASON_CODE.fullmatch(kind):
                return kind
        return None
    if rejection_category(error) != "EVIDENCE_INVALID":
        return None
    if isinstance(error, EvidenceRejected):
        return error.reason_code
    text = str(error)
    if len(text) <= REASON_CODE_MAX_LENGTH and REASON_CODE.fullmatch(text):
        return text
    return UNCLASSIFIED_REASON


def rejection_site(error: Exception) -> str | None:
    """Where ADD raised it: ``module:function:line``. A code location, never data."""
    site = None
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        module = frame.f_globals.get("__name__")
        if isinstance(module, str) and module.startswith("zk_add.") and module != __name__:
            site = f"{module}:{frame.f_code.co_name}:{trace.tb_lineno}"
        trace = trace.tb_next
    return site[:120] if site else None
