"""Windows stays awake while a board is set up (keepawake.py)."""
from __future__ import annotations

import os
import threading

import pytest

from sl_installer.core import keepawake


@pytest.fixture
def states(monkeypatch):
    seen = []
    monkeypatch.setattr(keepawake, "_set_state", lambda flags: seen.append(flags) or True)
    return seen


def test_held_for_the_block_and_released_after(states):
    with keepawake.keep_awake():
        assert states == [keepawake.AWAKE]
    assert states == [keepawake.AWAKE, keepawake.RELEASED]
    assert keepawake.AWAKE == 0x80000003 and keepawake.RELEASED == 0x80000000


def test_released_when_the_work_fails(states):
    with pytest.raises(RuntimeError):
        with keepawake.keep_awake():
            raise RuntimeError("the install failed")
    assert states == [keepawake.AWAKE, keepawake.RELEASED]


def test_nested_blocks_release_once_at_the_end(states):
    with keepawake.keep_awake():
        with keepawake.keep_awake():
            pass
        assert states == [keepawake.AWAKE]
    assert states == [keepawake.AWAKE, keepawake.RELEASED]


def test_each_thread_holds_its_own(states):
    # SetThreadExecutionState belongs to the calling thread, so each worker asks (and
    # clears) for itself whatever another worker holds.
    def work():
        with keepawake.keep_awake():
            pass

    with keepawake.keep_awake():
        worker = threading.Thread(target=work)
        worker.start()
        worker.join()
    assert states == [keepawake.AWAKE, keepawake.AWAKE, keepawake.RELEASED, keepawake.RELEASED]


@pytest.mark.skipif(os.name == "nt", reason="Windows really sets the state")
def test_does_nothing_off_windows():
    assert keepawake._set_state(keepawake.AWAKE) is False
    with keepawake.keep_awake():
        pass


@pytest.mark.skipif(os.name != "nt", reason="the Windows call itself")
def test_windows_accepts_the_request():
    assert keepawake._set_state(keepawake.AWAKE) is True
    assert keepawake._set_state(keepawake.RELEASED) is True
