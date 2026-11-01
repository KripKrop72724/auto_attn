"""Only one window of the app at a time (single_instance.py)."""
from __future__ import annotations

import subprocess
import sys
import textwrap

from sl_installer.core.single_instance import InstanceLock

from .support import SRC

# Another process trying the same lock, as a second copy of the app would.
SECOND_COPY = textwrap.dedent("""\
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[2])
    from sl_installer.core.single_instance import InstanceLock
    print("got it" if InstanceLock(Path(sys.argv[1])).acquire() else "already open")
""")


def second_copy(path) -> str:
    return subprocess.run([sys.executable, "-c", SECOND_COPY, str(path), str(SRC)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60,
                          check=True).stdout.strip()


def test_a_second_copy_is_refused_until_the_first_one_ends(tmp_path):
    path = tmp_path / "state" / "app.lock"
    first = InstanceLock(path)
    assert first.acquire() and first.held
    assert second_copy(path) == "already open"
    assert not InstanceLock(path).acquire()  # a second window in this process too
    first.release()
    assert not first.held
    assert second_copy(path) == "got it"  # that copy has ended, so the lock went with it
    with InstanceLock(path) as again:
        assert again.held


def test_the_lock_goes_with_a_process_that_died(tmp_path):
    path = tmp_path / "app.lock"
    # The child takes the lock and exits without releasing it, as a crash would.
    assert second_copy(path) == "got it"
    assert path.exists() and InstanceLock(path).acquire()


def test_acquire_twice_and_release_twice_are_harmless(tmp_path):
    lock = InstanceLock(tmp_path / "app.lock")
    assert lock.acquire() and lock.acquire()
    lock.release()
    lock.release()
    assert InstanceLock(tmp_path / "app.lock").acquire()
