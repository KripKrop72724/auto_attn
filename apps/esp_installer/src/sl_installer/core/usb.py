"""Finds boards on USB and watches them come and go.

Only the board's native USB-Serial/JTAG socket (VID 0x303A, PID 0x1001, the socket
marked USB) can be set up: its USB serial number is the board MAC, which is the
only identity a board in Secure Download Mode still gives. USB-UART bridges
(CP210x, CH34x, FTDI) mean the cable is in the other socket.

pyserial 3.5 on Windows reads the serial number from the parent of the COM
interface with CM_Get_Parent/CM_Get_Device_IDW, but it raises from comports() if
the walk fails, and for some hubs returns nothing. ``device_tree_macs`` reads the
same device tree from the composite parent down (cfgmgr32 through ctypes) and is
used when pyserial gives no MAC. When Windows ignores serial numbers it hands out
a location-based instance ID instead (``5&2B33E3B5&0&12``), and twelve hex digits
can hide in one, so only a serial number that is a whole MAC counts.

A board whose ID neither source gives has ``mac`` None. ``find`` still returns it
when it is the only board, for reading its console or waiting for a replug; the
installer never writes to such a port (installer._resolve_port).

A faulty board shows as a port that appears for about 10 s, vanishes and comes
back, again and again, with nothing on its console (the bootloader's 9 s RTC
watchdog firing during the PSRAM test). ``PresenceTimeline`` spots that loop, and
the faster one a power or crash problem gives.
"""
from __future__ import annotations

import re
import statistics
import sys
import threading
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .api import CancelToken, PresenceFn, PresenceReport, PresenceState, UsbCandidate, UsbKind
from .clock import Clock
from .logging_setup import get_logger
from .naming import normalize_mac

log = get_logger("usb")

NATIVE_VID = 0x303A
NATIVE_PID = 0x1001
BRIDGE_VIDS = {0x10C4: "CP210x", 0x1A86: "CH34x", 0x0403: "FTDI"}

# A watchdog loop on a known-faulty board: present 9-10.5 s, gone briefly.
SLOW_LOOP_PERIOD = (6.0, 16.0)
SLOW_LOOP_MAX_GAP = 6.0
# Power dips and early crashes restart a board every one to a few seconds.
FAST_LOOP_PERIOD = (0.4, 6.0)
FAST_LOOP_MAX_GAP = 4.0
TIMELINE_WINDOW = 90.0

PortLister = Callable[[], Sequence[object]]

# The USB-Serial/JTAG serial number is the chip's MAC, "AC:27:6E:A4:3D:30".
_SERIAL_MAC = re.compile(r"[0-9A-Fa-f]{2}(?:[:-]?[0-9A-Fa-f]{2}){5}")


def default_lister() -> Sequence[object]:
    from serial.tools import list_ports
    return list(list_ports.comports())


def serial_mac(value: object) -> Optional[str]:
    """The MAC in a USB serial number that is nothing but a MAC, else None.

    naming.normalize_mac keeps any twelve hex digits, which suits typed input but
    would turn an instance ID like 5&2B33E3B5&0&12 into a made-up board ID.
    """
    text = str(value or "").strip()
    return normalize_mac(text) if _SERIAL_MAC.fullmatch(text) else None


def classify_port(info: object, macs: Optional[Dict[str, str]] = None) -> Optional[UsbCandidate]:
    """A candidate for a port that may be a board, None for anything else."""
    vid = getattr(info, "vid", None)
    pid = getattr(info, "pid", None)
    port = str(getattr(info, "device", "") or "")
    description = str(getattr(info, "description", "") or "")
    if not port or vid is None:
        return None
    if vid == NATIVE_VID and pid == NATIVE_PID:
        mac = serial_mac(getattr(info, "serial_number", None))
        if mac is None and macs:
            mac = macs.get(port.upper()) or macs.get(port)
        return UsbCandidate(port, UsbKind.NATIVE, vid, pid, mac, description)
    if vid in BRIDGE_VIDS:
        return UsbCandidate(port, UsbKind.BRIDGE, vid, pid, None, description)
    if vid == NATIVE_VID:
        # An Espressif chip in another USB mode; not something the app can set up.
        return UsbCandidate(port, UsbKind.OTHER, vid, pid, None, description)
    return None


# ------------------------------------------------- Windows device-tree fallback

_PARENT_ID = re.compile(r"^USB\\VID_303A&PID_1001\\([^\\&]+)$", re.I)


class DeviceTree:
    """The cfgmgr32 calls the fallback needs; tests provide a fake."""

    def usb_instance_ids(self) -> List[str]:
        raise NotImplementedError

    def locate(self, instance_id: str) -> Optional[int]:
        raise NotImplementedError

    def children(self, devinst: int) -> Iterator[int]:
        raise NotImplementedError

    def port_name(self, devinst: int) -> Optional[str]:
        raise NotImplementedError


def device_tree_macs(tree: DeviceTree) -> Dict[str, str]:
    """COM port name (upper case) -> MAC for every present native board."""
    found: Dict[str, str] = {}
    for instance in tree.usb_instance_ids():
        match = _PARENT_ID.match(instance)
        mac = serial_mac(match.group(1)) if match else None
        if mac is None:
            continue
        devinst = tree.locate(instance)
        if devinst is None:
            continue
        for child in tree.children(devinst):
            port = tree.port_name(child)
            if port:
                found[port.upper()] = mac
    return found


class _WindowsDeviceTree(DeviceTree):  # pragma: no cover - needs Windows
    CR_SUCCESS = 0
    FILTER_ENUMERATOR = 0x00000001
    FILTER_PRESENT = 0x00000100
    REGISTRY_HARDWARE = 0x00000000
    DISPOSITION_OPEN_EXISTING = 0x00000001
    KEY_READ = 0x20019

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._cfg = ctypes.WinDLL("cfgmgr32")
        self._adv = ctypes.WinDLL("advapi32")

    def usb_instance_ids(self) -> List[str]:
        ctypes, wintypes = self._ctypes, self._wintypes
        flags = self.FILTER_ENUMERATOR | self.FILTER_PRESENT
        size = wintypes.ULONG(0)
        if self._cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "USB", flags) \
                != self.CR_SUCCESS or not size.value:
            return []
        buffer = ctypes.create_unicode_buffer(size.value)
        if self._cfg.CM_Get_Device_ID_ListW("USB", buffer, size, flags) != self.CR_SUCCESS:
            return []
        return [item for item in buffer[:size.value].split("\0") if item]

    def locate(self, instance_id: str) -> Optional[int]:
        devinst = self._wintypes.DWORD(0)
        if self._cfg.CM_Locate_DevNodeW(self._ctypes.byref(devinst), instance_id, 0) \
                != self.CR_SUCCESS:
            return None
        return devinst.value

    def children(self, devinst: int) -> Iterator[int]:
        child = self._wintypes.DWORD(0)
        result = self._cfg.CM_Get_Child(self._ctypes.byref(child), devinst, 0)
        while result == self.CR_SUCCESS:
            yield child.value
            sibling = self._wintypes.DWORD(0)
            result = self._cfg.CM_Get_Sibling(self._ctypes.byref(sibling), child.value, 0)
            child = sibling

    def port_name(self, devinst: int) -> Optional[str]:
        ctypes, wintypes = self._ctypes, self._wintypes
        key = wintypes.HKEY()
        if self._cfg.CM_Open_DevNode_Key(devinst, self.KEY_READ, 0,
                                         self.DISPOSITION_OPEN_EXISTING, ctypes.byref(key),
                                         self.REGISTRY_HARDWARE) != self.CR_SUCCESS:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(64)
            length = wintypes.DWORD(ctypes.sizeof(buffer))
            if self._adv.RegQueryValueExW(key, "PortName", None, None, buffer,
                                          ctypes.byref(length)) != 0:
                return None
            return buffer.value or None
        finally:
            self._adv.RegCloseKey(key)


def windows_device_tree_macs() -> Dict[str, str]:
    if sys.platform != "win32":
        return {}
    try:  # pragma: no cover - needs Windows
        return device_tree_macs(_WindowsDeviceTree())
    except Exception as error:  # pragma: no cover - any failure only loses the fallback
        log.warning("device tree lookup failed: %s", error)
        return {}


# ---------------------------------------------------------------- timeline

@dataclass(frozen=True)
class LoopVerdict:
    key: str
    period_s: float
    fast: bool


class PresenceTimeline:
    """Appear/vanish history per board, for spotting restart loops."""

    def __init__(self, window_s: float = TIMELINE_WINDOW) -> None:
        self._window = window_s
        self._events: Dict[str, List[Tuple[float, bool]]] = {}
        self._present: Set[str] = set()

    def update(self, now: float, present: Iterable[str]) -> None:
        current = set(present)
        for key in current - self._present:
            self._events.setdefault(key, []).append((now, True))
        for key in self._present - current:
            self._events.setdefault(key, []).append((now, False))
        self._present = current
        horizon = now - self._window
        for key in list(self._events):
            kept = [event for event in self._events[key] if event[0] >= horizon]
            if kept:
                self._events[key] = kept
            else:
                del self._events[key]

    def mark_gone(self, now: float, key: str) -> None:
        """A disappearance seen another way (the console lost its port)."""
        if key in self._present:
            self.update(now, self._present - {key})

    def loop(self, now: float) -> Optional[LoopVerdict]:
        best: Optional[LoopVerdict] = None
        for key, events in self._events.items():
            verdict = _loop_in(key, events, now)
            if verdict and (best is None or verdict.period_s > best.period_s):
                best = verdict
        return best

    def reset(self) -> None:
        self._events.clear()
        self._present.clear()


def _loop_in(key: str, events: Sequence[Tuple[float, bool]], now: float
             ) -> Optional[LoopVerdict]:
    appears = [t for t, present in events if present]
    if len(appears) < 3:
        return None
    periods = [b - a for a, b in zip(appears, appears[1:])][-5:]
    gaps = []
    for index, (moment, present) in enumerate(events):
        if not present:
            later = [t for t, again in events[index + 1:] if again]
            if later:
                gaps.append(later[0] - moment)
    period = statistics.median(periods)
    # Only a loop that is still going counts.
    if now - events[-1][0] > 1.5 * period + 3.0:
        return None
    if (len(periods) >= 2 and all(SLOW_LOOP_PERIOD[0] <= p <= SLOW_LOOP_PERIOD[1]
                                  for p in periods)
            and gaps and all(g <= SLOW_LOOP_MAX_GAP for g in gaps)):
        return LoopVerdict(key, period, fast=False)
    if (len(periods) >= 3 and all(FAST_LOOP_PERIOD[0] <= p < FAST_LOOP_PERIOD[1]
                                  for p in periods)
            and gaps and all(g <= FAST_LOOP_MAX_GAP for g in gaps)):
        return LoopVerdict(key, period, fast=True)
    return None


def candidate_key(candidate: UsbCandidate) -> str:
    return candidate.mac or f"port:{candidate.port}"


# ------------------------------------------------------------------ watcher

class UsbWatcher:
    """UsbService. Do not run ``watch`` while an install runs: every esptool call
    resets the board, which re-enumerates USB and would look like a loop."""

    def __init__(self, lister: Optional[PortLister] = None, clock: Optional[Clock] = None,
                 tree_macs: Optional[Callable[[], Dict[str, str]]] = None) -> None:
        self._lister = lister or default_lister
        self._clock = clock or Clock()
        self._tree_macs = tree_macs if tree_macs is not None else windows_device_tree_macs
        self._timeline = PresenceTimeline()
        self._last_seen: Dict[str, UsbCandidate] = {}
        self._state: Optional[PresenceState] = None
        self._since = 0.0
        self._lock = threading.Lock()
        self._listing_failed = False

    def candidates(self) -> Tuple[UsbCandidate, ...]:
        try:
            ports = list(self._lister())
            self._listing_failed = False
        except Exception as error:
            if not self._listing_failed:
                log.warning("listing serial ports failed: %s", error)
            self._listing_failed = True
            ports = []
        found = [classify_port(info) for info in ports]
        if any(c and c.kind is UsbKind.NATIVE and c.mac is None for c in found):
            macs = self._tree_macs()
            if macs:
                found = [classify_port(info, macs) for info in ports]
        result = [c for c in found if c is not None]
        result.sort(key=lambda c: (c.kind is not UsbKind.NATIVE, c.port))
        return tuple(result)

    def snapshot(self) -> PresenceReport:
        with self._lock:
            now = self._clock.monotonic()
            candidates = self.candidates()
            natives = [c for c in candidates if c.kind is UsbKind.NATIVE]
            for candidate in natives:
                self._last_seen[candidate_key(candidate)] = candidate
            self._timeline.update(now, (candidate_key(c) for c in natives))
            loop = self._timeline.loop(now)
            board: Optional[UsbCandidate] = None
            period: Optional[float] = None
            if loop is not None:
                state = PresenceState.RESTART_LOOP
                board = self._last_seen.get(loop.key)
                period = round(loop.period_s, 1)
            elif len(natives) == 1:
                state, board = PresenceState.ONE_BOARD, natives[0]
            elif len(natives) > 1:
                state = PresenceState.SEVERAL_BOARDS
            elif any(c.kind is UsbKind.BRIDGE for c in candidates):
                state = PresenceState.WRONG_SOCKET
            else:
                state = PresenceState.NO_BOARD
            if state is not self._state:
                self._state, self._since = state, now
            return PresenceReport(state, candidates, board, period, round(now - self._since, 1))

    def watch(self, on_report: PresenceFn, cancel: CancelToken, interval_s: float = 0.3) -> None:
        previous: Optional[PresenceReport] = None
        last_emit = -1e9
        while not cancel.cancelled:
            report = self.snapshot()
            now = self._clock.monotonic()
            if (previous is None or replace(report, seconds_in_state=0.0)
                    != replace(previous, seconds_in_state=0.0) or now - last_emit >= 1.0):
                on_report(report)
                previous, last_emit = report, now
            if self._clock.sleep(interval_s, cancel):
                break

    def find(self, mac: str) -> Tuple[Optional[UsbCandidate], int]:
        """The native port with this MAC, and how many native ports there are.

        A lone native port without a board ID is returned for any MAC, with ``mac``
        None: enough to read a console or see a replug, never to write to a board.
        """
        natives = [c for c in self.candidates() if c.kind is UsbKind.NATIVE]
        wanted = normalize_mac(mac)
        for candidate in natives:
            if candidate.mac == wanted:
                return candidate, len(natives)
        if len(natives) == 1 and natives[0].mac is None:
            return natives[0], 1
        return None, len(natives)

    def note_gone(self, candidate: UsbCandidate) -> None:
        with self._lock:
            self._timeline.mark_gone(self._clock.monotonic(), candidate_key(candidate))

    def reset_history(self) -> None:
        with self._lock:
            self._timeline.reset()
            self._state = None
