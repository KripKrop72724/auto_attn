"""The load source must not stop producing punches while awaiting receipts."""
from threading import Event
import multiprocessing
import sys
import time

import pytest

from scripts.run_zkt_custody_load import BurstInputs, emit_in_process


class Clock:
    value = 100.0

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds


def test_all_arrivals_are_retained_when_acknowledgements_wait_until_after_the_window():
    clock = Clock()
    inputs = BurstInputs(17, 10, 1, capacity=10)
    # No consumer acknowledges anything until generation has finished.
    inputs.emit(100.0, Event(), now=clock.now, wait=clock.wait)
    result = inputs.snapshot()
    assert result["offered"] == 170 and not result["input_refusals"]
    assert result["input_generation_complete"] and result["last_offer_elapsed_s"] == 0.9
    assert result["max_emitter_lag_ms"] == 0
    for queue in inputs.queues:
        observed = [queue.get_nowait() for _ in range(10)]
        assert [sequence for sequence, _ in observed] == list(range(1, 11))
        assert [due for _, due in observed] == [100 + i / 10 for i in range(10)]
        assert queue.empty()


def test_saturation_is_counted_without_overwriting_already_admitted_inputs():
    clock = Clock()
    inputs = BurstInputs(2, 10, 1, capacity=2)
    inputs.emit(100.0, Event(), now=clock.now, wait=clock.wait)
    result = inputs.snapshot()
    assert result["offered"] == 4 and result["input_refusals"] == 16
    assert result["input_queue_capacity_per_site"] == 2
    for queue in inputs.queues:
        assert queue.qsize() == 2
        assert queue.get_nowait()[0] == 1
        assert queue.get_nowait()[0] == 2


def test_source_deadline_never_extends_to_make_up_missed_inputs():
    clock = Clock()
    inputs = BurstInputs(2, 10, 1, capacity=20)

    def delayed(seconds):
        if seconds:
            clock.value = 101.0  # Host did not schedule the next pulse in time.

    inputs.emit(100.0, Event(), now=clock.now, wait=delayed)
    assert inputs.done.is_set() and inputs.offered == 2 and inputs.refused == 0
    assert all(queue.qsize() == 1 for queue in inputs.queues)


def test_interruption_is_not_treated_as_a_complete_input_set():
    stop = Event()
    stop.set()
    inputs = BurstInputs(2, 10, 1, capacity=20)
    inputs.emit(100, stop, now=lambda: 100, wait=lambda seconds: None)
    assert inputs.done.is_set() and inputs.offered == 0
    with pytest.raises(ValueError, match="Positive"):
        BurstInputs(0, 10, 1, capacity=20)


def test_pending_depths_account_for_taken_inputs_without_platform_qsize(monkeypatch):
    clock = Clock()
    inputs = BurstInputs(2, 10, 1, capacity=10)
    inputs.emit(100.0, Event(), now=clock.now, wait=clock.wait)

    def unsupported():
        raise NotImplementedError("macOS does not implement multiprocessing Queue.qsize")

    for queue in inputs.queues:
        monkeypatch.setattr(queue, "qsize", unsupported)
    assert inputs.pending_depths() == [10, 10]
    assert inputs.take(0, timeout=0.1)[0] == 1
    assert inputs.pending_depths() == [9, 10]
    assert inputs.snapshot()["offered"] == 20


def test_spawned_source_finishes_while_parent_python_execution_is_busy():
    context = multiprocessing.get_context("spawn")
    inputs = BurstInputs(2, 10, 1, capacity=10, context=context)
    start = context.Value('d', 0)
    gate, ready, stop = context.Event(), context.Event(), context.Event()
    process = context.Process(target=emit_in_process, args=(inputs, start, gate, ready, stop))
    process.start()
    previous_interval = sys.getswitchinterval()
    try:
        assert ready.wait(10)
        start.value = time.monotonic() + 0.1
        # Hold this interpreter's GIL across the whole input window. A source
        # thread in this process could not meet the original hard deadline.
        sys.setswitchinterval(2)
        gate.set()
        while time.monotonic() < start.value + 1.05:
            pass
        sys.setswitchinterval(previous_interval)
        assert inputs.done.wait(2)
        process.join(timeout=2)
        assert process.exitcode == 0
        assert inputs.snapshot()["offered"] == 20 and inputs.refused == 0
        assert inputs.pending_depths() == [10, 10]
        for site in range(2):
            assert [inputs.take(site, timeout=1)[0] for _ in range(10)] == list(range(1, 11))
        assert inputs.pending_depths() == [0, 0]
    finally:
        sys.setswitchinterval(previous_interval)
        stop.set()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        for queue in inputs.queues:
            queue.close()
            queue.join_thread()


def test_spawned_source_can_stop_before_the_window_starts():
    context = multiprocessing.get_context("spawn")
    inputs = BurstInputs(1, 10, 1, capacity=10, context=context)
    start = context.Value('d', 0)
    gate, ready, stop = context.Event(), context.Event(), context.Event()
    process = context.Process(target=emit_in_process, args=(inputs, start, gate, ready, stop))
    process.start()
    try:
        assert ready.wait(10)
        stop.set()
        process.join(timeout=2)
        assert process.exitcode == 0 and inputs.done.is_set()
        assert inputs.offered == 0 and inputs.pending_depths() == [0]
    finally:
        stop.set()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        for queue in inputs.queues:
            queue.close()
            queue.join_thread()
