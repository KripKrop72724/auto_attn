"""Bounded error labels; never copy exception messages or rejected PII."""
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError


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
