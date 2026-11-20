"""Plain words for problem (alert) codes, and the Problems page's type map.

Read side only (UI/UX revamp WORDS-5 alert half, PRB-2). The stored alert
``message`` and the policy's "Clears when ..." sentences never change; they
stay in Technical details. Words follow 02-words Appendix C1 word for word.

* ``plain(code, details, severity=..., message=...)`` gives ``plain_title``,
  ``plain_detail`` and ``next_step`` for one alert; numbers come only from
  the alert's ``details`` and are left out when missing.
* ``GROUP`` is the six-value group (as on the Activity page); ``PROBLEM_TYPE``
  is the Problems page "Type" filter (eight values); ``AFFECTS_ATTENDANCE``
  lists the codes whose problem can stop or hold check-ins.
* ``codes_matching(q)`` finds codes whose plain title or type label contains
  the search text, for ``GET /api/v1/alerts?q=``.

Unknown codes get the group fallback ("Link box problem"), never the code.
"""

from __future__ import annotations

from typing import Any

RESTART_LINK_BOX = (
    "Ask someone at the place to unplug the small link box next to the machine, wait 10 seconds and "
    "plug it back in, at a quiet time (not at arrival or leaving time). The Restart button on this page "
    "restarts the attendance machine, not the link box. Do not do this if the card says "
    "\"Do not switch the link box off\"."
)

GROUP_LABELS = {
    "link_box": "Link box",
    "link_box_memory": "Link box memory",
    "machine": "Machine",
    "software": "Software",
    "people": "People",
    "hr_system": "HR system",
}
TYPE_LABELS = {
    "link_box": "Link box",
    "link_box_memory": "Link box memory",
    "machine": "Machine",
    "checkins": "Check-ins",
    "hr_system": "HR system",
    "people": "People",
    "software": "Software",
    "phone": "Phone messages",
}
PROBLEM_TYPES = tuple(TYPE_LABELS)

# code: (group, title, sentence, what to do). "{…}" titles are built in plain().
_WORDS: dict[str, tuple[str, str, str, str]] = {
    "QUARANTINED_DUPLICATE_SERIAL": (
        "machine", "Two link boxes claim the same machine",
        "Two link boxes say they are connected to the same machine, so changes to its people are "
        "blocked to keep records safe.",
        "Find which place really has this machine and ask IT to correct the other link box."),
    "ESP_FATAL": (
        "link_box", "Link box has a serious fault",
        "It reported a serious start-up fault and may not be sending any check-ins.",
        f"{RESTART_LINK_BOX} If it stays, call IT; it may need replacing."),
    "ZKT_SERIAL_MISMATCH": (
        "machine", "Wrong machine connected",
        "This device is linked to a different attendance machine than expected. Its check-ins are on hold.",
        "If the machine was replaced on purpose, confirm the new machine in Tools › Advanced tools. "
        "If not, connect the right machine again."),
    "ESP_DURABILITY_FAULT": (
        "link_box_memory", "Link box cannot save attendance safely",
        "Its memory has a problem, so check-ins could be lost if the power or internet goes off.",
        "Do not switch the link box off. Call IT. Do not unplug it, even if another card says so."),
    "ESP_PRESERVATION_UNVERIFIED": (
        "link_box_memory", "Link box memory not checked yet",
        "A memory problem was closed by hand, but the link box has not yet shown its memory is healthy.",
        "Nothing now. It closes after the link box gets software that can check its memory."),
    "ESP_RESTART_LOOP": (
        "link_box", "Link box keeps restarting",
        "It keeps switching itself off and on, which usually means a power or cable problem.",
        "Check the link box's power adapter and socket; try another socket."),
    "ESP_DELIVERY_WORKER_FAULT": (
        "link_box", "Link box is stuck sending attendance",
        "The part of the link box that sends check-ins has stopped. Check-ins are kept on the box until "
        "it recovers.",
        f"Wait 10 minutes. If it stays: {RESTART_LINK_BOX} If it still stays, call IT."),
    "HEARTBEAT_STALE": (
        "link_box", "Link box stopped reporting",
        "It is connected but has stopped reporting.",
        f"Wait a few minutes. If it continues: {RESTART_LINK_BOX}"),
    "ESP_LOCAL_FAILURE": (
        "link_box", "Link box reported a problem",
        "Its status light shows a memory fault. Check-ins may be delayed.",
        f"{RESTART_LINK_BOX} If it comes back, call IT."),
    "HIK_CAPTURE_UNHEALTHY": (
        "machine", "Machine not giving attendance properly",
        "The machine isn't giving its attendance properly.",
        "Ask IT to check the machine."),
    "DEVICE_MESSAGE_REJECTED": (
        "link_box", "Some information from this device was refused",
        "Information the link box sent wasn't in the expected form, so it was refused. The box keeps it "
        "and tries again.",
        "Call IT; the link box may need a software update."),
    "ADD_MESSAGE_PROCESSING_FAILED": (
        "link_box", "Some information from this device could not be processed",
        "The problem is on the dashboard's side, not at the place. The link box keeps the data and sends "
        "it again.",
        "No visit is needed. Tell IT if it lasts more than an hour."),
    "HIK_LIGHT_RECONCILE_BLOCKED": (
        "machine", "Routine record check is stuck",
        "The regular check that compares the machine's saved check-ins with the dashboard's has stopped; "
        "live check-ins still arrive.",
        "Ask IT to look at this device's Activity."),
    "OTA_DEVICE_ROLLED_BACK": (
        "software", "Software update undone",
        "After installing the new software, the link box restarted and went back to its old version. "
        "It keeps working, and the update is paused.",
        "Check the link box's power at the place, then try the update again on Software updates."),
    "OTA_DEVICE_REPORTED_FAILURE": (
        "software", "Software update failed",
        "The link box couldn't download or check the new software. It keeps working on its old software, "
        "and the update is paused.",
        "Check the internet at this place, then press Try again on Software updates."),
    "ZKT_CONNECTION_FLAPPING": (
        "machine", "Machine connection keeps dropping",
        "The connection between the link box and the machine keeps breaking and coming back.",
        "Check the cable and the network switch between them; replace a loose or damaged cable."),
    "TERMINAL_DISCONNECTED": (
        "machine", "Machine not responding",
        "The link box has not reached the machine for a while.",
        "Check the machine is switched on and its network cable is plugged in at both ends."),
    "TERMINAL_STABILIZING_STALLED": (
        "machine", "Machine connection not settling",
        "The machine came back but is not steady yet.",
        "Check the machine's cable; if it continues, restart the machine."),
    "DELIVERY_AUTHORITY_UNKNOWN": (
        "link_box", "Device setup needs checking",
        "After a restart, the link box hasn't finished getting ready to send check-ins.",
        "Wait 15 minutes. If it stays, call IT."),
    "ESP_OFFLINE": (
        "link_box", "No internet",
        "The link box at this place has stopped reporting. It may be switched off, or the internet here "
        "isn't working.",
        "Ask someone at the place to check the link box has power and the internet works. Check-ins stay "
        "saved on the machine."),
    "TERMINAL_LINK_DOWN": (
        "machine", "Machine not responding",
        "The link box can't reach the attendance machine. It may be switched off or its cable unplugged.",
        "Check the machine is switched on and its network cable is plugged in at both ends."),
    "ZKT_CLOCK_DRIFT": (
        "machine", "Machine clock is wrong",
        "The machine's clock is more than 2 minutes wrong, so new check-in times will be wrong too.",
        "Set the correct date and time on the machine, or ask IT."),
    "HIK_CLOCK_DRIFT": (
        "machine", "Machine clock is wrong",
        "The machine's clock is more than 2 minutes wrong, so new check-in times will be wrong too.",
        "Set the correct date and time on the machine, or ask IT."),
    "ESP_CLOCK_SKEW": (
        "link_box", "Link box clock is off (check-in times are fine)",
        "The link box's own clock is wrong, but check-in times are corrected automatically.",
        "Nothing. Don't change the machine's clock because of this."),
    "USER_SNAPSHOT_TRUNCATED": (
        "people", "List of people couldn't be read in full",
        "Only part of the machine's list of people arrived, so adding or changing people on this machine "
        "is paused.",
        "Open the device, go to People and press \"Refresh people list\". If it happens again, call IT."),
    "HISTORY_BACKFILL_BLOCKED": (
        "people", "Older check-ins couldn't be matched to people",
        "While reading this machine's older check-ins, some couldn't be matched to people, so nothing was "
        "changed in the HR system.",
        "Open People for this machine › Old check-ins and enter the people's details; reading continues "
        "by itself afterwards."),
    "ORDS_DELIVERY_FAILED": (
        "hr_system", "Check-ins not reaching the HR system (trying again)",
        "Check-ins from this place are saved and are being sent again automatically; the HR system hasn't "
        "accepted them yet.",
        "Nothing at the place. If it lasts more than an hour, tell IT."),
    "ORDS_EVENT_REJECTED": (
        "hr_system", "HR system refused a check-in",
        "The HR system refused one check-in from this place; the others continue normally.",
        "Open Attendance with the Problem filter, check the person's CNIC and send it again."),
    "CHECKIN_NOTIFY_DEGRADED": (
        "hr_system", "Check-in phone messages delayed",
        "Staff phone messages about their check-ins are late or failing. Check-ins and sending to the HR "
        "system aren't affected.",
        "Nothing at the place. Tell IT if it lasts for hours."),
    "ATTENDANCE_EVENT_QUARANTINED": (
        "link_box", "Some check-ins set aside for checking",
        "Some check-in records arrived damaged or unreadable, so they were set aside; later check-ins went "
        "through normally.",
        "Press \"Check them\" and mark each one as checked."),
    "ATTENDANCE_TIMESTAMP_QUARANTINED": (
        "machine", "Check-in with an impossible date or time",
        "A check-in had a date or time that can't be right (often the machine clock was wrong), so it was "
        "kept but not sent to the HR system.",
        "Correct the machine clock, then open Recover attendance › Unreadable records and press "
        "\"Fix impossible times\"."),
    "ATTENDANCE_REPAIR_NEEDS_ATTENTION": (
        "people", "Attendance fix needs your decision",
        "An attendance fix started for this machine has stopped and is waiting for someone to decide.",
        "Open the attendance fix for this machine and continue or cancel it."),
    "ATTENDANCE_REPAIR_STALLED": (
        "people", "Attendance fix is stuck",
        "An attendance fix hasn't moved for 10 minutes. Saved check-ins are safe.",
        "Check that this place's machine and link box are online; the fix continues by itself."),
    "ADD_SOURCE_COVERAGE_INVALIDATED": (
        "machine", "Machine records no longer match the dashboard's copy",
        "The machine's saved check-ins changed in a way that doesn't match the dashboard's copy (for "
        "example after a reset), so automatic reading stopped to keep records safe.",
        "Run Recover attendance for this machine; tell IT if it happens again."),
    "ADD_SOURCE_GENERATION_LINEAGE_DIVERGED": (
        "machine", "Machine history reads differently after a reset",
        "After the machine was reset or changed, its older check-ins read differently from the saved copy; "
        "nothing was changed.",
        "Ask IT to review the records for this machine."),
    "ADD_SOURCE_COVERAGE_UNVERIFIED": (
        "machine", "Recovery result couldn't be checked",
        "A recovery finished but its result couldn't be checked. Nothing was changed.",
        "Ask IT to review it. Cancelling the run doesn't close this."),
    "ADD_SOURCE_TAIL_STALE_GENERATION": (
        "link_box", "Link box is not keeping a backup copy of new check-ins",
        "Link box is not keeping a backup copy of new check-ins. Live check-ins still arrive.",
        f"{RESTART_LINK_BOX} If it continues, call IT."),
    "TERMINAL_SOURCE_EXCEPTION": (
        "machine", "Some machine records couldn't be read",
        "Some records saved on the machine were unreadable; they were kept for checking and reading "
        "continued.",
        "Press \"See the records\" to review them."),
    "TERMINAL_NEWEST_RECORD_CORRECTED": (
        "machine", "A corrected check-in needs HR to confirm",
        "A check-in time was corrected using a record that later proved wrong; the corrected check-in is "
        "set aside.",
        "Ask HR to confirm it, then mark this problem as fixed."),
    "TERMINAL_NEWEST_RECORD_CHANGED": (
        "machine", "A check-in may belong to someone else",
        "A record read from the machine changed later, so one check-in may be for the wrong person; it is "
        "set aside.",
        "Ask HR to check this check-in with the person, then mark this problem as fixed."),
    "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED": (
        "machine", "Recent check-ins may be missing",
        "The machine's newest record may hold a check-in the dashboard hasn't received yet.",
        "After the next check-in on this machine, run Recover attendance for it."),
    "TERMINAL_NEWEST_RECORD_RELEASE_HELD": (
        "people", "Some check-ins set aside for safety",
        "These check-ins couldn't be matched to a person safely, so they are set aside until Recover "
        "attendance confirms them.",
        "Run Recover attendance for this machine; they are released automatically afterwards."),
    "USER_DELETION_JOB_INCOMPLETE": (
        "people", "Some people were not removed",
        "A removal of several people from this machine didn't finish for everyone.",
        "Open People for this machine, check who is still there and remove them again."),
    "DUPLICATE_USER_CNIC": (
        "people", "Some people share one CNIC",
        "More than one person on this machine has the same CNIC, so check-ins could be counted for the "
        "wrong person.",
        "Open People for this machine and correct the CNIC, or confirm they are the same person."),
    "USER_CNIC_FORMAT_SUSPECT": (
        "people", "Some people have a wrongly typed CNIC",
        "Their CNIC is written in a form that can't be used (for example with dashes or a letter), so "
        "their check-ins are not matched.",
        "Confirm each CNIC with HR and enter it in People."),
    "ORACLE_RECEIPT_CONNECTOR_MISMATCH": (
        "hr_system", "Link box may be set up with another place's details",
        "This link box reported sending a check-in that belongs to a different place, so the report was "
        "ignored.",
        "Call IT; this link box may be set up with another place's details."),
    "ADMIN_REVOKE_OVERDUE": (
        "people", "Temporary admin access not removed",
        "Someone was given admin rights on the machine, and they couldn't be removed automatically.",
        "Make sure the device is online so the removal can retry. If it stays, remove the admin rights on "
        "the machine itself and tell IT."),
}

GROUP: dict[str, str] = {code: words[0] for code, words in _WORDS.items()}

# The Problems page "Type" filter: the group, except check-in and phone-message kinds.
_TYPE_OVERRIDES = {
    "ATTENDANCE_EVENT_QUARANTINED": "checkins",
    "ATTENDANCE_TIMESTAMP_QUARANTINED": "checkins",
    "TERMINAL_NEWEST_RECORD_CORRECTED": "checkins",
    "TERMINAL_NEWEST_RECORD_CHANGED": "checkins",
    "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED": "checkins",
    "TERMINAL_NEWEST_RECORD_RELEASE_HELD": "checkins",
    "HISTORY_BACKFILL_BLOCKED": "checkins",
    "ATTENDANCE_REPAIR_NEEDS_ATTENTION": "checkins",
    "ATTENDANCE_REPAIR_STALLED": "checkins",
    "CHECKIN_NOTIFY_DEGRADED": "phone",
}
PROBLEM_TYPE: dict[str, str] = {code: _TYPE_OVERRIDES.get(code, group) for code, group in GROUP.items()}

# Codes whose problem stops, holds or puts at risk check-ins reaching the HR system.
AFFECTS_ATTENDANCE = frozenset({
    "QUARANTINED_DUPLICATE_SERIAL", "ESP_FATAL", "ZKT_SERIAL_MISMATCH", "ESP_DURABILITY_FAULT",
    "ESP_DELIVERY_WORKER_FAULT", "HEARTBEAT_STALE", "ESP_LOCAL_FAILURE", "HIK_CAPTURE_UNHEALTHY",
    "DEVICE_MESSAGE_REJECTED", "ZKT_CONNECTION_FLAPPING", "TERMINAL_DISCONNECTED", "DELIVERY_AUTHORITY_UNKNOWN",
    "ESP_OFFLINE", "TERMINAL_LINK_DOWN", "ZKT_CLOCK_DRIFT", "HIK_CLOCK_DRIFT", "HISTORY_BACKFILL_BLOCKED",
    "ORDS_DELIVERY_FAILED", "ORDS_EVENT_REJECTED", "ATTENDANCE_EVENT_QUARANTINED",
    "ATTENDANCE_TIMESTAMP_QUARANTINED", "ATTENDANCE_REPAIR_NEEDS_ATTENTION", "ATTENDANCE_REPAIR_STALLED",
    "ADD_SOURCE_COVERAGE_INVALIDATED", "TERMINAL_SOURCE_EXCEPTION", "TERMINAL_NEWEST_RECORD_CORRECTED",
    "TERMINAL_NEWEST_RECORD_CHANGED", "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED",
    "TERMINAL_NEWEST_RECORD_RELEASE_HELD", "DUPLICATE_USER_CNIC", "USER_CNIC_FORMAT_SUSPECT",
    "ORACLE_RECEIPT_CONNECTOR_MISMATCH",
})

_HIK_REASONS = {
    "HIK_NETWORK": ("Machine not responding", "The attendance machine may be switched off or its cable unplugged.",
                    "Check its power and cable."),
    "HIK_AUTH": ("Wrong machine settings",
                 "The machine's password or settings don't match, so its attendance can't be read.",
                 "Ask IT to update the machine's settings."),
    "HIK_CONFIGURATION": ("Wrong machine settings",
                          "The machine's password or settings don't match, so its attendance can't be read.",
                          "Ask IT to update the machine's settings."),
    "HIK_SOURCE_CHANGED": ("Machine records changed",
                           "The machine's saved records changed, for example after a reset.",
                           "Ask IT to check the machine."),
    "HIK_STORAGE": ("Link box cannot save check-ins", "The link box can't store check-ins.",
                    "Don't switch the link box off; call IT."),
    "HIK_WAITING": ("Waiting for the machine's first reply", "The machine isn't giving its attendance properly.",
                    "Ask IT to check the machine."),
}
_HIK_LIVE = ("Older records need checking", "New check-ins arrive, but the machine's older records need a check.",
             "Ask IT to check the machine.")


def _count(details: dict, key: str) -> int | None:
    value = details.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def fallback_title(code: str | None) -> str:
    group = GROUP.get(code or "") or _group_from_prefix(code)
    return f"{GROUP_LABELS[group]} problem"


def _group_from_prefix(code: str | None) -> str:
    value = (code or "").upper()
    if value.startswith(("OTA_",)):
        return "software"
    if value.startswith(("ORDS_", "ORACLE_")):
        return "hr_system"
    if value.startswith(("ZKT_", "HIK_", "TERMINAL_")):
        return "machine"
    if value.startswith(("USER_",)):
        return "people"
    return "link_box"


def _title(code: str, base: str, details: dict, severity: str | None, message: str | None) -> str:
    if code == "ESP_RESTART_LOOP":
        boots = details.get("boots")
        window = _count(details, "window_seconds")
        if isinstance(boots, list) and boots and window:
            return f"Link box keeps restarting ({len(boots)} times in {max(1, round(window / 60))} min)"
    elif code in {"ZKT_CLOCK_DRIFT", "HIK_CLOCK_DRIFT"}:
        drift = _count(details, "drift_seconds")
        if drift:
            minutes = max(1, round(abs(drift) / 60))
            return f"Machine clock is {minutes} min {'fast' if drift > 0 else 'slow'}"
    elif code == "ORDS_DELIVERY_FAILED":
        category = str(details.get("failure_category") or "")
        if category in {"FIRMWARE_RECEIPT_NOT_IN_ORACLE", "CONFIRMED_EVENT_MISSING_FROM_ORACLE"}:
            return "A sent check-in was missing in the HR system, so it is being sent again"
        if category.startswith(("CHECK_", "FIRMWARE_CHECK_", "REVERIFY_")):
            return "Double-checking check-ins with the HR system"
    elif code == "CHECKIN_NOTIFY_DEGRADED":
        if (severity or "").upper() in {"HIGH", "CRITICAL"}:
            return "Check-in phone messages paused"
    elif code == "ATTENDANCE_EVENT_QUARANTINED":
        count = _count(details, "quarantined")
        if count:
            return f"{_plural(count, 'check-in')} set aside for checking"
    elif code == "TERMINAL_SOURCE_EXCEPTION":
        count = _count(details, "exception_count")
        if count:
            return f"{count} machine {'record' if count == 1 else 'records'} couldn't be read"
    elif code == "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED":
        count = _count(details, "slot_count")
        if count:
            return f"{_plural(count, 'recent check-in')} may be missing"
    elif code == "TERMINAL_NEWEST_RECORD_RELEASE_HELD":
        count = _count(details, "event_count")
        if count:
            return f"{_plural(count, 'check-in')} set aside for safety"
    elif code == "DUPLICATE_USER_CNIC":
        count = _count(details, "affected_users")
        if count and count > 1:
            return f"{count} people share one CNIC"
    elif code == "USER_CNIC_FORMAT_SUSPECT":
        count = _count(details, "affected_users")
        if count:
            return f"{count} {'person has' if count == 1 else 'people have'} a wrongly typed CNIC"
    return base


def plain(code: str | None, details: Any = None, *, severity: str | None = None,
          message: str | None = None) -> dict:
    """``plain_title``, ``plain_detail`` and ``next_step`` for one alert."""

    details = details if isinstance(details, dict) else {}
    code = code or ""
    words = _WORDS.get(code)
    if words is None:
        return {
            "plain_title": fallback_title(code),
            "plain_detail": "The dashboard reported something this page has no words for yet.",
            "next_step": "Open the device for details.",
        }
    _group, title, sentence, step = words
    if code == "HIK_CAPTURE_UNHEALTHY":
        reason = str(details.get("reason") or "")
        live = (message or "").startswith("Live Hikvision capture")
        title, sentence, step = _HIK_LIVE if live else _HIK_REASONS.get(reason, (title, sentence, step))
    elif code == "ORDS_DELIVERY_FAILED" and str(details.get("failure_category") or "") in {"HTTP_401", "HTTP_403"}:
        step = "The HR system refused the login; tell IT now."
    return {
        "plain_title": _title(code, title, details, severity, message),
        "plain_detail": sentence,
        "next_step": step,
    }


def problem_type(code: str | None) -> str | None:
    return PROBLEM_TYPE.get(code or "")


def affects_attendance(code: str | None) -> bool:
    return (code or "") in AFFECTS_ATTENDANCE


def codes_for_types(types) -> list[str]:
    wanted = set(types)
    return sorted(code for code, kind in PROBLEM_TYPE.items() if kind in wanted)


def codes_matching(q: str | None) -> list[str]:
    """Codes whose plain title (any variant) or type label contains ``q``."""

    needle = (q or "").strip().lower()
    if not needle:
        return []
    matches = set()
    for code, (_group, title, sentence, _step) in _WORDS.items():
        texts = [title, TYPE_LABELS.get(PROBLEM_TYPE.get(code, ""), "")]
        if code == "HIK_CAPTURE_UNHEALTHY":
            texts += [entry[0] for entry in _HIK_REASONS.values()] + [_HIK_LIVE[0]]
        if any(needle in text.lower() for text in texts):
            matches.add(code)
    return sorted(matches)


def alert_fields(code: str | None, details: Any = None, *, severity: str | None = None,
                 message: str | None = None) -> dict:
    """Every plain field one alert row carries (added next to its stored fields)."""

    return {
        **plain(code, details, severity=severity, message=message),
        "type": problem_type(code),
        "affects_attendance": affects_attendance(code),
    }
