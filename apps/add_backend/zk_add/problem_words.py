"""Plain problem words: a title, a sentence and what to do for every problem code.

From 02-words Appendix C1. The backend fields plain_title, plain_detail and
next_step are built here from the code and the alert's details (numbers
only); the stored message and clear_condition stay unchanged and are shown
only under Technical details.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Mapping

from zk_add.activity_words import MESSAGE_TYPE_OTHER, MESSAGE_TYPES

# code -> (group, title, sentence, what to do)
# fmt: off
WORDS: dict[str, tuple[str, str, str, str]] = {'ADD_MESSAGE_PROCESSING_FAILED': ('link_box',
                                   'Some information from this device could not be processed',
                                   "The problem is on the dashboard's side, not at the place. The link box keeps "
                                   'the data and sends it again.',
                                   'No visit is needed. Tell IT if it lasts more than an hour.'),
 'ADD_SOURCE_COVERAGE_INVALIDATED': ('machine',
                                     "Machine records no longer match the dashboard's copy",
                                     "The machine's saved check-ins changed in a way that doesn't match the "
                                     "dashboard's copy (for example after a reset), so automatic reading stopped "
                                     'to keep records safe.',
                                     'Run Recover attendance for this machine; tell IT if it happens again.'),
 'ADD_SOURCE_COVERAGE_UNVERIFIED': ('machine',
                                    "Recovery result couldn't be checked",
                                    "A recovery finished but its result couldn't be checked. Nothing was changed.",
                                    "Ask IT to review it. Cancelling the run doesn't close this."),
 'ADD_SOURCE_GENERATION_LINEAGE_DIVERGED': ('machine',
                                            'Machine history reads differently after a reset',
                                            'After the machine was reset or changed, its older check-ins read '
                                            'differently from the saved copy; nothing was changed.',
                                            'Ask IT to review the records for this machine.'),
 'ADD_SOURCE_TAIL_STALE_GENERATION': ('link_box',
                                      'Link box is not keeping a backup copy of new check-ins',
                                      'Link box is not keeping a backup copy of new check-ins. Live check-ins '
                                      'still arrive.',
                                      'Ask someone at the place to unplug the small link box next to the machine, '
                                      'wait 10 seconds and plug it back in, at a quiet time (not at arrival or '
                                      'leaving time). The Restart button on this page restarts the attendance '
                                      'machine, not the link box. Do not do this if the card says "Do not switch '
                                      'the link box off". If it continues, call IT.'),
 'ADMIN_REVOKE_OVERDUE': ('people',
                          'Temporary admin access not removed',
                          "Someone was given admin rights on the machine, and they couldn't be removed "
                          'automatically.',
                          'Make sure the device is online so the removal can retry. If it stays, remove the admin '
                          'rights on the machine itself and tell IT.'),
 'ATTENDANCE_EVENT_QUARANTINED': ('link_box',
                                  'Check-ins set aside for checking',
                                  'Some check-in records arrived damaged or unreadable, so they were set aside; '
                                  'later check-ins went through normally.',
                                  'Press "Check them" and mark each one as checked.'),
 'ATTENDANCE_REPAIR_NEEDS_ATTENTION': ('people',
                                       'Attendance fix needs your decision',
                                       'An attendance fix started for this machine has stopped and is waiting for '
                                       'someone to decide.',
                                       'Open the attendance fix for this machine and continue or cancel it.'),
 'ATTENDANCE_REPAIR_STALLED': ('people',
                               'Attendance fix is stuck',
                               "An attendance fix hasn't moved for 10 minutes. Saved check-ins are safe.",
                               "Check that this place's machine and link box are online; the fix continues by "
                               'itself.'),
 'ATTENDANCE_TIMESTAMP_QUARANTINED': ('machine',
                                      'Check-in with an impossible date or time',
                                      "A check-in had a date or time that can't be right (often the machine clock "
                                      'was wrong), so it was kept but not sent to the HR system.',
                                      'Correct the machine clock, then open Recover attendance › Unreadable '
                                      'records and press "Fix impossible times".'),
 'CHECKIN_NOTIFY_DEGRADED': ('hr_system',
                             'Check-in phone messages delayed',
                             'Staff phone messages about their check-ins are late or failing. Check-ins and '
                             "sending to the HR system aren't affected.",
                             'Nothing at the place. Tell IT if it lasts for hours.'),
 'DELIVERY_AUTHORITY_UNKNOWN': ('link_box',
                                'Device setup needs checking',
                                "After a restart, the link box hasn't finished getting ready to send check-ins.",
                                'Wait 15 minutes. If it stays, call IT.'),
 'DEVICE_MESSAGE_REJECTED': ('link_box',
                             'Some information from this device was refused',
                             "Information the link box sent wasn't in the expected form, so it was refused. The "
                             'box keeps it and tries again.',
                             'Call IT; the link box may need a software update.'),
 'DUPLICATE_USER_CNIC': ('people',
                         'People share one CNIC',
                         'More than one person on this machine has the same CNIC, so check-ins could be counted '
                         'for the wrong person.',
                         'Open People for this machine and correct the CNIC, or confirm they are the same person.'),
 'ESP_CLOCK_SKEW': ('link_box',
                    'Link box clock is off (check-in times are fine)',
                    "The link box's own clock is wrong, but check-in times are corrected automatically.",
                    "Nothing. Don't change the machine's clock because of this."),
 'ESP_DELIVERY_WORKER_FAULT': ('link_box',
                               'Link box is stuck sending attendance',
                               'The part of the link box that sends check-ins has stopped. Check-ins are kept on '
                               'the box until it recovers.',
                               'Wait 10 minutes. If it stays: Ask someone at the place to unplug the small link '
                               'box next to the machine, wait 10 seconds and plug it back in, at a quiet time (not '
                               'at arrival or leaving time). The Restart button on this page restarts the '
                               'attendance machine, not the link box. Do not do this if the card says "Do not '
                               'switch the link box off". If it still stays, call IT.'),
 'ESP_DURABILITY_FAULT': ('storage',
                          'Link box cannot save attendance safely',
                          'Its memory has a problem, so check-ins could be lost if the power or internet goes off.',
                          'Do not switch the link box off. Call IT. Do not unplug it, even if another card says '
                          'so.'),
 'ESP_FATAL': ('link_box',
               'Link box has a serious fault',
               'It reported a serious start-up fault and may not be sending any check-ins.',
               'Ask someone at the place to unplug the small link box next to the machine, wait 10 seconds and '
               'plug it back in, at a quiet time (not at arrival or leaving time). The Restart button on this page '
               'restarts the attendance machine, not the link box. Do not do this if the card says "Do not switch '
               'the link box off". If it stays, call IT; it may need replacing.'),
 'ESP_LOCAL_FAILURE': ('link_box',
                       'Link box reported a problem',
                       'Its status light shows a memory fault. Check-ins may be delayed.',
                       'Ask someone at the place to unplug the small link box next to the machine, wait 10 seconds '
                       'and plug it back in, at a quiet time (not at arrival or leaving time). The Restart button '
                       'on this page restarts the attendance machine, not the link box. Do not do this if the card '
                       'says "Do not switch the link box off". If it comes back, call IT.'),
 'ESP_OFFLINE': ('link_box',
                 'No internet',
                 'The link box at this place has stopped reporting. It may be switched off, or the internet here '
                 "isn't working.",
                 'Ask someone at the place to check the link box has power and the internet works. Check-ins stay '
                 'saved on the machine.'),
 'ESP_PRESERVATION_UNVERIFIED': ('storage',
                                 'Link box memory not checked yet',
                                 'A memory problem was closed by hand, but the link box has not yet shown its '
                                 'memory is healthy.',
                                 'Nothing now. It closes after the link box gets software that can check its '
                                 'memory.'),
 'ESP_RESTART_LOOP': ('link_box',
                      'Link box keeps restarting',
                      'It keeps switching itself off and on, which usually means a power or cable problem.',
                      "Check the link box's power adapter and socket; try another socket."),
 'HEARTBEAT_STALE': ('link_box',
                     'Link box stopped reporting',
                     'It is connected but has stopped reporting.',
                     'Wait a few minutes. If it continues: Ask someone at the place to unplug the small link box '
                     'next to the machine, wait 10 seconds and plug it back in, at a quiet time (not at arrival or '
                     'leaving time). The Restart button on this page restarts the attendance machine, not the link '
                     'box. Do not do this if the card says "Do not switch the link box off".'),
 'HIK_CAPTURE_UNHEALTHY': ('machine',
                           'Machine not giving attendance properly',
                           "The machine isn't giving its attendance properly.",
                           'Ask IT to check the machine.'),
 'HIK_CLOCK_DRIFT': ('machine',
                     'Machine clock is wrong',
                     "The machine's clock is more than 2 minutes wrong, so new check-in times will be wrong too.",
                     'Set the correct date and time on the machine, or ask IT.'),
 'HIK_LIGHT_RECONCILE_BLOCKED': ('machine',
                                 'Routine record check is stuck',
                                 "The regular check that compares the machine's saved check-ins with the "
                                 "dashboard's has stopped; live check-ins still arrive.",
                                 "Ask IT to look at this device's Activity."),
 'HISTORY_BACKFILL_BLOCKED': ('people',
                              "Older check-ins couldn't be matched to people",
                              "While reading this machine's older check-ins, some couldn't be matched to people, "
                              'so nothing was changed in the HR system.',
                              "Open People for this machine › Old check-ins and enter the people's details; "
                              'reading continues by itself afterwards.'),
 'ORACLE_RECEIPT_CONNECTOR_MISMATCH': ('hr_system',
                                       "Link box may be set up with another place's details",
                                       'This link box reported sending a check-in that belongs to a different '
                                       'place, so the report was ignored.',
                                       "Call IT; this link box may be set up with another place's details."),
 'ORDS_DELIVERY_FAILED': ('hr_system',
                          'Check-ins not reaching the HR system (trying again)',
                          'Check-ins from this place are saved and are being sent again automatically; the HR '
                          "system hasn't accepted them yet.",
                          'Nothing at the place. If it lasts more than an hour, tell IT.'),
 'ORDS_EVENT_REJECTED': ('hr_system',
                         'HR system refused a check-in',
                         'The HR system refused one check-in from this place; the others continue normally.',
                         "Open Attendance with the Problem filter, check the person's CNIC and send it again."),
 'OTA_DEVICE_REPORTED_FAILURE': ('software',
                                 'Software update failed',
                                 "The link box couldn't download or check the new software. It keeps working on "
                                 'its old software, and the update is paused.',
                                 'Check the internet at this place, then press Try again on Software updates.'),
 'OTA_DEVICE_ROLLED_BACK': ('software',
                            'Software update undone',
                            'After installing the new software, the link box restarted and went back to its old '
                            'version. It keeps working, and the update is paused.',
                            "Check the link box's power at the place, then try the update again on Software "
                            'updates.'),
 'QUARANTINED_DUPLICATE_SERIAL': ('machine',
                                  'Two link boxes claim the same machine',
                                  'Two link boxes say they are connected to the same machine, so changes to its '
                                  'people are blocked to keep records safe.',
                                  'Find which place really has this machine and ask IT to correct the other link '
                                  'box.'),
 'TERMINAL_DISCONNECTED': ('machine',
                           'Machine not responding',
                           'The link box has not reached the machine for a while.',
                           'Check the machine is switched on and its network cable is plugged in at both ends.'),
 'TERMINAL_LINK_DOWN': ('machine',
                        'Machine not responding',
                        "The link box can't reach the attendance machine. It may be switched off or its cable "
                        'unplugged.',
                        'Check the machine is switched on and its network cable is plugged in at both ends.'),
 'TERMINAL_NEWEST_RECORD_CHANGED': ('machine',
                                    'A check-in may belong to someone else',
                                    'A record read from the machine changed later, so one check-in may be for the '
                                    'wrong person; it is set aside.',
                                    'Ask HR to check this check-in with the person, then mark this problem as '
                                    'fixed.'),
 'TERMINAL_NEWEST_RECORD_CORRECTED': ('machine',
                                      'A corrected check-in needs HR to confirm',
                                      'A check-in time was corrected using a record that later proved wrong; the '
                                      'corrected check-in is set aside.',
                                      'Ask HR to confirm it, then mark this problem as fixed.'),
 'TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED': ('machine',
                                             'Recent check-ins may be missing',
                                             "The machine's newest record may have a check-in the dashboard hasn't "
                                             'received yet.',
                                             'After the next check-in on this machine, run Recover attendance for '
                                             'it.'),
 'TERMINAL_NEWEST_RECORD_RELEASE_HELD': ('people',
                                         'Check-ins set aside for safety',
                                         "These check-ins couldn't be matched to a person safely, so they are set "
                                         'aside until Recover attendance confirms them.',
                                         'Run Recover attendance for this machine; they are released automatically '
                                         'afterwards.'),
 'TERMINAL_SOURCE_EXCEPTION': ('machine',
                               "Machine records couldn't be read",
                               'Some records saved on the machine were unreadable; they were kept for checking and '
                               'reading continued.',
                               'Press "See the records" to review them.'),
 'TERMINAL_STABILIZING_STALLED': ('machine',
                                  'Machine connection not settling',
                                  'The machine came back but is not steady yet.',
                                  "Check the machine's cable; if it continues, restart the machine."),
 'USER_CNIC_FORMAT_SUSPECT': ('people',
                              'People have a wrongly typed CNIC',
                              "Their CNIC is written in a form that can't be used (for example with dashes or a "
                              'letter), so their check-ins are not matched.',
                              'Confirm each CNIC with HR and enter it in People.'),
 'USER_DELETION_JOB_INCOMPLETE': ('people',
                                  'Some people were not removed',
                                  "A removal of several people from this machine didn't finish for everyone.",
                                  'Open People for this machine, check who is still there and remove them again.'),
 'USER_SNAPSHOT_TRUNCATED': ('people',
                             "List of people couldn't be read in full",
                             "Only part of the machine's list of people arrived, so adding or changing people on "
                             'this machine is paused.',
                             'Open the device, go to People and press "Refresh people list". If it happens again, '
                             'call IT.'),
 'ZKT_CLOCK_DRIFT': ('machine',
                     'Machine clock is wrong',
                     "The machine's clock is more than 2 minutes wrong, so new check-in times will be wrong too.",
                     'Set the correct date and time on the machine, or ask IT.'),
 'ZKT_CONNECTION_FLAPPING': ('machine',
                             'Machine connection keeps dropping',
                             'The connection between the link box and the machine keeps breaking and coming back.',
                             'Check the cable and the network switch between them; replace a loose or damaged '
                             'cable.'),
 'ZKT_SERIAL_MISMATCH': ('machine',
                         'Wrong machine connected',
                         'This device is linked to a different attendance machine than expected. Its check-ins are '
                         'paused.',
                         'If the machine was replaced on purpose, confirm the new machine in Tools › Advanced '
                         'tools. If not, connect the right machine again.')}
# fmt: on

GROUP_WORDS = {"machine": "Machine", "link_box": "Link box", "hr_system": "HR system", "people": "People",
               "software": "Software", "storage": "Link box memory"}
UNKNOWN_DETAIL = "The dashboard reported something this page has no words for yet."
UNKNOWN_NEXT_STEP = "Open the device for details."
PREFIX_GROUPS = (("ZKT_", "machine"), ("HIK_", "machine"), ("TERMINAL_", "machine"), ("ORDS_", "hr_system"),
                 ("ORACLE_", "hr_system"), ("CHECKIN_", "hr_system"), ("USER_", "people"), ("IDENTITY_", "people"),
                 ("DUPLICATE_", "people"), ("ATTENDANCE_REPAIR", "people"), ("OTA_", "software"),
                 ("FIRMWARE_", "software"), ("STORAGE_", "storage"), ("ESP_DURABILITY", "storage"),
                 ("ESP_", "link_box"), ("ADD_SOURCE", "machine"))
HIK_REASONS = {
    "NETWORK": ("Machine not responding", "The attendance machine may be switched off or its cable unplugged.",
                "Check its power and cable."),
    "AUTH": ("Wrong machine settings", "The machine's password or settings don't match, so its attendance can't "
             "be read.", "Ask IT to update the machine's settings."),
    "CONFIGURATION": ("Wrong machine settings", "The machine's password or settings don't match, so its "
                      "attendance can't be read.", "Ask IT to update the machine's settings."),
    "SOURCE_CHANGED": ("Machine records changed", "The machine's saved records changed, for example after a reset.",
                       "Ask IT to check the machine."),
    "STORAGE": ("Link box cannot save check-ins", "The link box can't store check-ins.",
                "Don't switch the link box off; call IT."),
    "WAITING": ("Waiting for the machine's first reply", "The machine isn't giving its attendance properly.",
                "Ask IT to check the machine."),
    "LIVE": ("Older records need checking", "New check-ins arrive, but the machine's older records need a check.",
             "Ask IT to check the machine."),
}
PKT = timezone(timedelta(hours=5))


def group_for(code: str) -> str:
    if code in WORDS:
        return WORDS[code][0]
    for prefix, group in PREFIX_GROUPS:
        if code.startswith(prefix):
            return group
    return "link_box"


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _people(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _pkt_time(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(PKT)
    hour = local.hour % 12 or 12
    return f"{local.day} {local.strftime('%b')}, {hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"


def _minutes(seconds) -> int | None:
    value = _int(seconds)
    return None if value is None else max(1, round(value / 60))


def plain(code: str, details: Mapping | None = None, *, minutes: int | None = None,
          severity: str | None = None) -> dict:
    """{"plain_title", "plain_detail", "next_step", "group"} for a problem code.

    details is the alert's details (numbers and names only are read);
    minutes is how long a derived reason (no alert) has lasted.
    """
    details = details or {}
    code = code or ""
    group = group_for(code)
    if code in WORDS:
        _group, title, detail, next_step = WORDS[code]
    else:
        title, detail, next_step = f"{GROUP_WORDS[group]} problem", UNKNOWN_DETAIL, UNKNOWN_NEXT_STEP

    if code == "ESP_RESTART_LOOP":
        boots = details.get("boots")
        count = len(boots) if isinstance(boots, (list, tuple)) else _int(boots)
        window = _minutes(details.get("window_seconds"))
        if count and window:
            title = f"Link box keeps restarting ({count} times in {window} min)"
    elif code == "HIK_CAPTURE_UNHEALTHY":
        # details.reason is the terminal error code, for example "HIK_NETWORK".
        reason = str(details.get("reason") or "").upper().removeprefix("HIK_")
        if details.get("live_capture") or reason == "LIVE":
            reason = "LIVE"
        if reason in HIK_REASONS:
            title, detail, next_step = HIK_REASONS[reason]
    elif code == "DEVICE_MESSAGE_REJECTED":
        types = details.get("types")
        names = sorted(types) if isinstance(types, (list, tuple, dict)) else (
            [details.get("message_type")] if details.get("message_type") else [])
        words = [MESSAGE_TYPES.get(str(name), MESSAGE_TYPE_OTHER) for name in names]
        words = list(dict.fromkeys(words))
        if words:
            title = f"{title} ({', '.join(words[:3])})"
    elif code in {"ZKT_CLOCK_DRIFT", "HIK_CLOCK_DRIFT"}:
        drift = _int(details.get("drift_seconds"))
        if drift:
            title = f"Machine clock is {max(1, round(abs(drift) / 60))} min {'fast' if drift > 0 else 'slow'}"
    elif code == "ORDS_DELIVERY_FAILED":
        category = str(details.get("failure_category") or "").upper()
        if "CHECK" in category or "VERIF" in category:
            title = "Double-checking check-ins with the HR system"
        elif "MISSING" in category:
            title = "A sent check-in was missing in the HR system, so it is being sent again"
        if category in {"HTTP_401", "HTTP_403"}:
            next_step = "The HR system refused the login; tell IT now."
    elif code == "CHECKIN_NOTIFY_DEGRADED":
        if (severity or details.get("severity") or "").upper() == "HIGH" or details.get("paused"):
            title = "Check-in phone messages paused"
    elif code == "ATTENDANCE_EVENT_QUARANTINED":
        count = _int(details.get("quarantined"))
        if count:
            title = f"{_people(count, 'check-in', 'check-ins')} set aside for checking"
    elif code == "ADD_SOURCE_TAIL_STALE_GENERATION":
        since = _pkt_time(details.get("first_stale_at"))
        if since:
            title = f"{title} (since {since})"
    elif code == "TERMINAL_SOURCE_EXCEPTION":
        count = _int(details.get("exception_count"))
        if count:
            title = f"{_people(count, 'machine record', 'machine records')} couldn't be read"
    elif code == "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED":
        count = _int(details.get("slot_count"))
        if count:
            title = f"{_people(count, 'recent check-in', 'recent check-ins')} may be missing"
    elif code == "TERMINAL_NEWEST_RECORD_RELEASE_HELD":
        count = _int(details.get("event_count"))
        if count:
            title = f"{_people(count, 'check-in', 'check-ins')} set aside for safety"
    elif code == "DUPLICATE_USER_CNIC":
        count = _int(details.get("affected_users"))
        if count:
            title = f"{_people(count, 'person shares', 'people share')} one CNIC"
    elif code == "USER_CNIC_FORMAT_SUSPECT":
        count = _int(details.get("affected_users"))
        if count:
            title = f"{_people(count, 'person has', 'people have')} a wrongly typed CNIC"
    elif code in {"HEARTBEAT_STALE", "TERMINAL_DISCONNECTED", "TERMINAL_STABILIZING_STALLED"} and minutes:
        detail = {
            "HEARTBEAT_STALE": f"It is connected but has not reported for {minutes} min.",
            "TERMINAL_DISCONNECTED": f"The link box has not reached the machine for {minutes} min.",
            "TERMINAL_STABILIZING_STALLED": f"The machine came back but is not steady yet ({minutes} min).",
        }[code]
    return {"plain_title": title, "plain_detail": detail, "next_step": next_step, "group": group}
