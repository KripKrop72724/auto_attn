"""The load source must not stop producing punches while awaiting receipts."""
from threading import Event

import pytest

from scripts.run_zkt_custody_load import BurstInputs


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
