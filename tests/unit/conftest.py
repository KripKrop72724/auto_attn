"""Shared unit-test setup."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_naming_and_status_memory():
    """device_names and public_status keep the latest fleet and /status answers in
    process memory (one uvicorn worker). Tests reuse connector ids, so each test
    starts and ends with that memory empty instead of inheriting another test's fleet."""
    from zk_add import device_names, public_status

    device_names.forget_fleet()
    public_status.remember_statuses([])
    yield
    device_names.forget_fleet()
    public_status.remember_statuses([])
