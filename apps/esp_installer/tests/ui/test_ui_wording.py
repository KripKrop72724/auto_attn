"""Plain words and the State Life palette, checked over the whole UI source."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from sl_installer.core.api import ConsoleEvent, ConsoleKind, PresenceReport, PresenceState
from sl_installer.ui import naming, pictures, wording

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
