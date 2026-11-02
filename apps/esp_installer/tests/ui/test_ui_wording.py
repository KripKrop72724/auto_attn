"""Plain words and the State Life palette, checked over the whole UI source."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from sl_installer.core import texts
from sl_installer.core.api import (
    BootOutcome,
    BootReport,
    ConsoleEvent,
    ConsoleKind,
    Family,
    FieldDefaults,
    PresenceReport,
    PresenceState,
    Severity,
)
from sl_installer.ui import naming, pictures, wording
from sl_installer.ui.pages.settings_form import SettingsForm
from sl_installer.ui.widgets import FieldBlock

APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
UI_SOURCE = APP_ROOT / "src" / "sl_installer" / "ui"
PICTURES = APP_ROOT / "src" / "sl_installer" / "resources" / "pictures"
FAKES = APP_ROOT / "src" / "sl_installer" / "core" / "fakes.py"
# The engine's advice catalogue: the screens show these words as they are.
ENGINE_TEXTS = APP_ROOT / "src" / "sl_installer" / "core" / "texts.py"

# Words a person using the app should never have to read (design section 7).
JARGON = re.compile(
    r"\b(efuse|nvs|partition|com port|mac address|esptool|spiffs|psram|ota|"
    r"provision\w*|firmware|serial port|bootloader|sdm)\b",
    re.IGNORECASE,
)


def visible_strings(path: Path):
    """String literals that read like sentences (they contain a space)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            value = node.value.strip()
            if " " in value and not RAW_LINE.match(value):
                yield node.lineno, node.value


# The board's own console lines (shown only behind "Show the board's own words") and
# technical causes for the log may use technical words.
RAW_LINE = re.compile(r"^([IWE] \(\d+\) |rst:|fake: )")


@pytest.mark.parametrize(
    "path", sorted(UI_SOURCE.rglob("*.py")) + [FAKES, ENGINE_TEXTS], ids=lambda item: item.name
)
def test_no_jargon_in_screen_words(path):
    if path.name == "theme.py":  # style sheet text, never shown
        return
    found = [(line, value) for line, value in visible_strings(path) if JARGON.search(value)]
    assert not found, found


def allowed_colours():
    spec = importlib.util.spec_from_file_location(
        "repo_contract", REPO_ROOT / "scripts" / "check_repository_contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ALLOWED_UI_COLORS


def test_only_state_life_colours_in_code_and_pictures():
    allowed = allowed_colours()
    files = list(UI_SOURCE.rglob("*.py")) + list(PICTURES.rglob("*.svg"))
    used = {}
    for path in files:
        content = path.read_text(encoding="utf-8")
        for colour in re.findall(r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])", content):
            used.setdefault(colour.lower(), set()).add(path.name)
        assert not re.search(r"#[0-9a-fA-F]{3}\b", content), f"short colour in {path.name}"
    unexpected = {colour: sorted(names) for colour, names in used.items() if colour not in allowed}
    assert not unexpected
    assert "#0094da" in used and "#ffffff" in used


def test_every_console_kind_reads_well_with_or_without_data():
    for kind in ConsoleKind:
        for data in (
            {},
            {"zone_id": "ZONE-LAHORE-01", "device_id": "1", "ssid": "RE7", "version": "2.7.8"},
        ):
            words = wording.console_words(ConsoleEvent(kind, "raw", data))
            if words is None:
                # Only "other" lines, and a version line without its version, say nothing.
                assert kind is ConsoleKind.OTHER or (kind is ConsoleKind.APP_VERSION and not data)
                continue
            assert "{" not in words[0] and "?" not in words[0] and words[0].endswith(".")


def test_portal_steps_name_the_hotspot_and_address():
    steps = " ".join(wording.portal_steps("SLIC ATTENDANCE-3D31"))
    assert "SLIC ATTENDANCE-3D31" in steps and naming.SETUP_PAGE_ADDRESS in steps
    assert "2.4 GHz" in steps and "Test connection and save" in steps


@pytest.mark.parametrize("state", list(PresenceState))
def test_presence_fallbacks(state):
    advice = wording.presence_advice(PresenceReport(state, seconds_in_state=20))
    if state is PresenceState.ONE_BOARD:
        assert advice is None
    else:
        assert advice.what_to_do and pictures.picture_path(advice.illustration) is not None


def test_engine_picture_keys_all_have_pictures():
    for key in (
        "unplug",
        "replug",
        "usb_socket",
        "boot_button",
        "cable",
        "faulty_board",
        "hotspot",
        "wait",
    ):
        assert pictures.picture_path(key) is not None, key
    for key in pictures.ILLUSTRATIONS:
        assert pictures.picture_path(key) is not None, key


def test_help_topics_use_existing_pictures():
    for topic in wording.HELP_TOPICS.values():
        for item in topic.items:
            assert item.picture is None or pictures.picture_path(item.picture) is not None
            assert item.text.endswith(".")


def test_connect_help_shows_how_to_put_the_board_into_setup_mode():
    # Connect's own advice can ask for setup mode: Help there shows the same buttons.
    item = next(i for i in wording.HELP_TOPICS["connect"].items if i.picture == "boot_button")
    assert item.heading == texts.SETUP_MODE_NEEDED.title
    for words in ("Hold down the BOOT button.", "press and release the RST button",
                  "Then let go of BOOT.", "unplug the board", "Hold down BOOT while you plug"):
        assert words in item.text, words
    assert "Try again" not in item.text  # a button Connect does not have


def test_no_board_fallback_says_how_to_start_setup_mode_before_faulty():
    steps = wording.presence_advice(PresenceReport(PresenceState.NO_BOARD)).what_to_do
    assert steps[-2] == texts.SETUP_MODE_TIP and "faulty" in steps[-1]


# "Click Fix settings and type it under The attendance machine, Machine comm key.": a step
# that sends the person to a field names the part of the form it is in (review CR3).
FIELD_DIRECTION = re.compile(
    r"\b[Uu]nder ([A-Z][\w' ]*?), (?:set )?([A-Z][\w' -]*?)(?: to [^.]*)?\."
)


def form_places(fix_mode):
    """(part of the form, field) for every field the form shows, for each machine type."""
    places = set()
    for family in Family:
        form = SettingsForm(fix_mode=fix_mode)
        form.reset(family, FieldDefaults())
        parts = (
            (form.advanced.header.text(), form.advanced, form.advanced.content),
            (form.machine_card.title_label.text(), form.machine_card, form.machine_card),
        )
        for field in form.findChildren(FieldBlock):
            for title, part, body in parts:
                if part.isVisibleTo(form) and body.isAncestorOf(field) and field.isVisibleTo(body):
                    places.add((title, field.label.text()))
        form.deleteLater()
    return places


def test_steps_name_the_part_of_the_form_where_the_field_is():
    advices = list(texts.STATIC.values()) + [texts.light_hint(s) for s in texts.LED_GUIDE]
    steps = [step for advice in advices for step in advice.what_to_do]
    steps += list(texts._FIELD_RULES.values())  # shown after "Click Change the details."
    fix, details = form_places(fix_mode=True), form_places(fix_mode=False)
    directions = [(step, m.groups()) for step in steps for m in FIELD_DIRECTION.finditer(step)]
    assert len(directions) >= 3, directions  # the pattern still finds what it checks
    for step, place in directions:
        # Fix settings opens the fix form; "Change the details" goes back to either form.
        allowed = fix if "Fix settings" in step else fix | details
        assert place in allowed, (step, sorted(allowed))


# ----------------------------------------------------- the restart check (second start)


def test_restart_check_asks_for_a_second_deliberate_replug():
    assert texts.RESTART_REPLUG == (
        "Unplug the board again, wait 3 seconds, and plug it back in. "
        "The app checks that the board keeps its settings."
    )
    advice = wording.restart_check(BootReport(BootOutcome.OK, "LF-ZONE-BLD9-04", "1", "2.7.8"))
    assert advice.title == "One more check" and advice.severity is Severity.INFO
    # The requested sentence as it is, then the replug's own warning.
    assert advice.what_to_do == (texts.RESTART_REPLUG, "Do not press any button.")
    # The setup is not finished: whoever would take the board away now is told so.
    assert advice.what_happened == (
        "The board started for LF-ZONE-BLD9-04, device number 1. The setup is not finished. "
        "Do not take the board to a site yet."
    )
    # Not the end of the setup: no word of success before the second start.
    words = " ".join((advice.title, advice.what_happened) + advice.what_to_do)
    assert not re.search(r"correctly|ready|kept|working", words), words
    lost_start = wording.restart_check(BootReport(BootOutcome.OK))
    assert lost_start.what_happened.startswith("The board started. The setup is not finished.")
    # The page's subtitle says why; the card does not say it a second time.
    assert words.count("keeps its settings") == 1


def test_success_is_said_only_with_the_settings_kept():
    assert texts.SETTINGS_KEPT == (
        "The board started correctly and kept its settings after a restart."
    )
    first = BootReport(BootOutcome.OK, "LF-ZONE-BLD9-04", None, None)
    second = BootReport(BootOutcome.OK, "LF-ZONE-BLD9-04", "1", "2.7.8")
    ok = wording.start_checks_ok(first, second)
    assert ok.severity is Severity.SUCCESS and ok.title == "The board kept its settings"
    # A sentence a line: the ZONE ID never breaks at one of its hyphens.
    assert ok.what_happened.split("\n") == [
        texts.SETTINGS_KEPT,
        "Its settings are for LF-ZONE-BLD9-04, device number 1.",
        "Its software is version 2.7.8.",
    ]
    # The Done screen says it in its subtitle, and only after the restart check passed.
    assert wording.done_subtitle(True) == (
        f"{texts.SETTINGS_KEPT} Take the board to the site. Then follow the steps below."
    )
    assert wording.done_subtitle(False) == wording.DONE_SUBTITLE
    assert "kept" not in wording.DONE_SUBTITLE and "correctly" not in wording.DONE_SUBTITLE


def test_lost_settings_keep_the_board_away_from_the_site():
    advice = texts.settings_lost_on_restart("ac:27:6e:a4:3d:30")
    words = " ".join((advice.title, advice.what_happened) + advice.what_to_do)
    assert advice.code == "SETTINGS_LOST_ON_RESTART"
    assert advice.title == "The board lost its settings" and "restarted" in advice.what_happened
    assert "correctly" not in words  # never read as a success
    assert advice.what_to_do[0] == (
        "Do not use this board. If it came from a site, do not put it back."
    )
    # It looks like a good board: a label with its ID, which the start screen shows nowhere.
    assert advice.what_to_do[1] == (
        "Unplug it. Put a label on it that says DO NOT USE and its board ID, "
        "AC:27:6E:A4:3D:30. Keep it aside. Do not throw it away."
    )
    # The help file now, while it still holds this board's record and console lines.
    assert advice.what_to_do[2] == (
        "Open Help and click Save a help file now, before you set up another board. Send the "
        "file to head office."
    )
    # Missing settings leave the board still; unreadable ones can make it restart.
    assert "solid red" not in words and "It cannot work like this." in words
    assert not advice.can_retry and advice.illustration == "unplug"
    # The screen offers only Go back to the start: no step may name another button.
    assert not re.search(r"\bclick (Try again|Next|Fix settings|Check the board)\b", words, re.I)
    changed = texts.settings_lost_on_restart("ac:27:6e:a4:3d:30", changed=True)
    assert changed.code == advice.code and changed.what_to_do == advice.what_to_do
    assert "it had other settings" in changed.what_happened
    # Without a board ID (the catalogue's copy) the label step still reads well.
    assert texts.SETTINGS_LOST_ON_RESTART.what_to_do[1] == (
        "Unplug it. Put a label on it that says DO NOT USE and its board ID. Keep it aside. "
        "Do not throw it away."
    )
    # The screens fall back to the engine's own words for both new outcomes.
    lost = BootReport(BootOutcome.SETTINGS_LOST)
    assert wording.boot_fallback(lost, None, "ac:27:6e:a4:3d:30") == advice
    assert wording.boot_fallback(lost, None) == texts.SETTINGS_LOST_ON_RESTART
    assert (
        wording.boot_fallback(BootReport(BootOutcome.SETTINGS_NOT_SEEN), None)
        is texts.SETTINGS_NOT_SEEN
    )


def test_settings_not_seen_is_a_retry_not_a_fault():
    advice = texts.SETTINGS_NOT_SEEN
    assert advice.severity is Severity.WARNING and advice.can_retry
    # Try again watches a new replug: the click comes first, then the replug it asks for.
    assert advice.what_to_do[0] == "Click Try again."
    assert advice.what_to_do[1].startswith("When the screen asks, unplug the board")
    assert "Do not press any button." in advice.what_to_do[1]
    # A computer that keeps missing the line must not send boards to the sites unchecked.
    assert advice.what_to_do[2].startswith("If it happens again, do not install the board.")
    assert "Put it aside." in advice.what_to_do[2] and "Save a help file" in advice.what_to_do[2]
    assert "faulty" not in " ".join(advice.what_to_do).lower()
    assert "So it cannot" not in advice.what_happened
    assert "The app cannot tell yet" in advice.what_happened


def test_first_start_help_tells_of_the_second_replug():
    item = wording.HELP_TOPICS["first_start"].items[-1]
    # No picture: item 3 shows plugging in already.
    assert item.heading == "5. Once more" and item.picture is None
    assert "one more time" in item.text and "keeps its settings" in item.text
    assert "the app tells you what to do" in item.text
