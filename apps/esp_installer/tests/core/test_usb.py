"""USB discovery, the Windows device-tree fallback and restart-loop detection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import pytest

from sl_installer.core.api import CancelToken, PresenceState, UsbKind
from sl_installer.core.usb import (DeviceTree, PresenceTimeline, UsbWatcher, classify_port,
                                   device_tree_macs)

from .support import MAC, FakeClock


@dataclass
class Port:
    device: str
    vid: Optional[int]
    pid: Optional[int]
    serial_number: Optional[str] = None
    description: str = ""


def native(port="COM5", serial="AC:27:6E:A4:3D:30"):
    return Port(port, 0x303A, 0x1001, serial, "USB Serial Device (COM5)")


def test_classify_native_bridge_other():
    candidate = classify_port(native())
    assert candidate.kind is UsbKind.NATIVE and candidate.mac == MAC
    assert classify_port(native(serial="ac276ea43d30")).mac == MAC
    assert classify_port(Port("COM3", 0x10C4, 0xEA60)).kind is UsbKind.BRIDGE
    assert classify_port(Port("COM4", 0x1A86, 0x55D4)).kind is UsbKind.BRIDGE
    assert classify_port(Port("COM6", 0x0403, 0x6001)).kind is UsbKind.BRIDGE
    assert classify_port(Port("COM8", 0x303A, 0x0002)).kind is UsbKind.OTHER
    assert classify_port(Port("COM1", None, None)) is None          # a built-in serial port
    assert classify_port(Port("COM9", 0x8087, 0x0AAA)) is None      # Bluetooth and the like


class FakeTree(DeviceTree):
    def __init__(self, ids: List[str], children: Dict[str, List[str]]) -> None:
        self.ids = ids
        self.kids = children
        self.nodes = {name: index + 1 for index, name in enumerate(ids)}

    def usb_instance_ids(self):
        return self.ids

    def locate(self, instance_id):
        return self.nodes.get(instance_id)

    def children(self, devinst):
        parent = next(name for name, node in self.nodes.items() if node == devinst)
        for index, port in enumerate(self.kids.get(parent, [])):
            yield 1000 * devinst + index

    def port_name(self, devinst):
        parent = next(name for name, node in self.nodes.items() if node == devinst // 1000)
        return self.kids[parent][devinst % 1000]


def test_device_tree_fallback_reads_mac_from_the_composite_parent():
    tree = FakeTree(
        ["USB\\VID_303A&PID_1001\\AC:27:6E:A4:3D:30",
         "USB\\VID_303A&PID_1001&MI_00\\6&2A4B5C3D&0&0000",
         "USB\\VID_10C4&PID_EA60\\0001",
         "USB\\ROOT_HUB30\\4&1234&0&0"],
        {"USB\\VID_303A&PID_1001\\AC:27:6E:A4:3D:30": ["COM5"]})
    assert device_tree_macs(tree) == {"COM5": MAC}


def test_fallback_is_used_only_when_pyserial_has_no_serial():
    clock = FakeClock()
    ports = [native(serial=None)]
    asked = []

    def tree():
        asked.append(1)
        return {"COM5": MAC}
    watcher = UsbWatcher(lambda: ports, clock, tree_macs=tree)
    assert watcher.snapshot().board.mac == MAC and asked == [1]
    ports[0] = native()
    watcher.snapshot()
    assert asked == [1]


def scripted(clock, timeline):
    """A port lister that follows ``timeline``: [(seconds, [ports...]), ...]."""
    start = clock.now

    def lister():
        elapsed = clock.now - start
        current = []
        for moment, ports in timeline:
            if elapsed >= moment:
                current = ports
        return current
    return lister


@pytest.mark.parametrize("ports, state", [
    ([], PresenceState.NO_BOARD),
    ([native()], PresenceState.ONE_BOARD),
    ([native(), native("COM9", "AC:27:6E:A4:CE:A8")], PresenceState.SEVERAL_BOARDS),
    ([Port("COM3", 0x10C4, 0xEA60)], PresenceState.WRONG_SOCKET),
    ([native(), Port("COM3", 0x10C4, 0xEA60)], PresenceState.ONE_BOARD),
])
def test_presence_states(ports, state):
    report = UsbWatcher(lambda: ports, FakeClock(), tree_macs=dict).snapshot()
    assert report.state is state
    if state is PresenceState.ONE_BOARD:
        assert report.board.mac == MAC


def test_seconds_in_state_grows():
    clock = FakeClock()
    watcher = UsbWatcher(lambda: [], clock, tree_macs=dict)
    watcher.snapshot()
    clock.advance(16)
    assert watcher.snapshot().seconds_in_state == 16.0


def loop_timeline(period, present_for, cycles=4):
    timeline = []
    for cycle in range(cycles):
        timeline.append((cycle * period, [native()]))
        timeline.append((cycle * period + present_for, []))
    return timeline


def run_watcher(watcher, clock, seconds, step=0.3):
    reports = []
    end = clock.now + seconds
    while clock.now < end:
        reports.append(watcher.snapshot())
        clock.advance(step)
    return reports


def test_faulty_board_watchdog_loop_is_detected():
    clock = FakeClock()
    watcher = UsbWatcher(scripted(clock, loop_timeline(10.3, 9.5)), clock, tree_macs=dict)
    reports = run_watcher(watcher, clock, 35)
    loops = [r for r in reports if r.state is PresenceState.RESTART_LOOP]
    assert loops, [r.state for r in reports]
    assert loops[-1].loop_period_s == pytest.approx(10.3, abs=0.4)
    assert loops[-1].board.mac == MAC
    # Not before two full cycles were seen.
    first = next(i for i, r in enumerate(reports) if r.state is PresenceState.RESTART_LOOP)
    assert first * 0.3 >= 20


def test_fast_power_loop_is_detected_with_a_short_period():
    clock = FakeClock()
    watcher = UsbWatcher(scripted(clock, loop_timeline(2.0, 1.4, cycles=8)), clock,
                         tree_macs=dict)
    reports = run_watcher(watcher, clock, 15, step=0.1)
    loops = [r for r in reports if r.state is PresenceState.RESTART_LOOP]
    assert loops and loops[-1].loop_period_s < 6


def test_a_person_replugging_once_is_not_a_loop():
    clock = FakeClock()
    timeline = [(0, [native()]), (20, []), (24, [native()])]
    watcher = UsbWatcher(scripted(clock, timeline), clock, tree_macs=dict)
    reports = run_watcher(watcher, clock, 60)
    assert all(r.state is not PresenceState.RESTART_LOOP for r in reports)


def test_loop_ends_when_the_board_stays():
    clock = FakeClock()
    timeline = loop_timeline(10.0, 9.0, cycles=4) + [(40.0, [native()])]
    watcher = UsbWatcher(scripted(clock, timeline), clock, tree_macs=dict)
    reports = run_watcher(watcher, clock, 80)
    assert any(r.state is PresenceState.RESTART_LOOP for r in reports)
    assert reports[-1].state is PresenceState.ONE_BOARD


def test_timeline_marks_a_disappearance_seen_by_the_console():
    timeline = PresenceTimeline()
    timeline.update(0.0, {MAC})
    timeline.mark_gone(5.0, MAC)
    timeline.update(5.5, {MAC})
    assert timeline._events[MAC] == [(0.0, True), (5.0, False), (5.5, True)]


def test_watch_reports_changes_and_heartbeats():
    clock = FakeClock()
    ports = [native()]
    watcher = UsbWatcher(lambda: ports, clock, tree_macs=dict)
    cancel = CancelToken()
    seen = []

    def on_report(report):
        seen.append(report)
        if len(seen) == 3:
            ports.clear()
        if len(seen) == 5:
            cancel.cancel()
    watcher.watch(on_report, cancel, interval_s=0.5)
    assert seen[0].state is PresenceState.ONE_BOARD
    assert seen[-1].state is PresenceState.NO_BOARD


def test_listing_errors_do_not_crash():
    def broken():
        raise OSError("SetupDiGetClassDevs failed")
    report = UsbWatcher(broken, FakeClock(), tree_macs=dict).snapshot()
    assert report.state is PresenceState.NO_BOARD


def test_find_by_mac():
    ports = [native(), native("COM9", "AC:27:6E:A4:CE:A8")]
    watcher = UsbWatcher(lambda: ports, FakeClock(), tree_macs=dict)
    candidate, count = watcher.find(MAC)
    assert candidate.port == "COM5" and count == 2
    assert watcher.find("aa:bb:cc:dd:ee:ff") == (None, 2)
    lone = UsbWatcher(lambda: [native(serial=None)], FakeClock(), tree_macs=dict)
    assert lone.find(MAC)[0].port == "COM5"
