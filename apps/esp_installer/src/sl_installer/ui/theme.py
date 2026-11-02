"""Colours, fonts and the style sheet.

Every colour comes from ALLOWED_UI_COLORS in scripts/check_repository_contract.py
(the ADD dashboard tokens): State Life blue #0094DA with white for the header,
#0071A7 for buttons and links (#0094DA on white is too faint for text), and
the dashboard's success, warning and error pairs. Text is never smaller than
11 pt and controls are at least 44 px high, because the people using this
rarely use computers.
"""

from __future__ import annotations

from string import Template
from typing import Dict, Tuple

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from ..core.api import Severity
from . import pictures

BRAND = "#0094da"
PRIMARY = "#0071a7"
PRIMARY_HOVER = "#005d8a"
WHITE = "#ffffff"
CANVAS = "#f6f7f9"
SURFACE_SUBTLE = "#f9fafb"
SUNKEN = "#f1f3f5"
HOVER = "#f4f6f8"
BORDER = "#e4e7ec"
BORDER_STRONG = "#d0d5dd"
BORDER_INPUT = "#c5ccd6"
TEXT = "#101828"
TEXT_SECONDARY = "#475467"
TEXT_TERTIARY = "#667085"
TEXT_DISABLED = "#98a2b3"
ACCENT_SUBTLE = "#eef8fd"
ACCENT_MUTED = "#d8effb"
ACCENT_BORDER = "#a8dcf4"
ACCENT_WASH = "#f7fbfd"
FOCUS = "#7cc8ef"
NAVY = "#12324f"
NAVY_DARK = "#0b2238"
SCRIM = "#101828"

SUCCESS = "#067647"
SUCCESS_BG = "#ecfdf3"
SUCCESS_BORDER = "#abefc6"
SUCCESS_SOLID = "#17b26a"
WARNING = "#b54708"
WARNING_BG = "#fffaeb"
WARNING_BORDER = "#fedf89"
WARNING_SOLID = "#f79009"
ERROR = "#b42318"
ERROR_BG = "#fef3f2"
ERROR_BORDER = "#fecdca"
ERROR_SOLID = "#f04438"

LIFE_TEXT = PRIMARY_HOVER
LIFE_BG = ACCENT_MUTED
HEALTH_TEXT = "#0c704d"
HEALTH_BG = "#e7f6f0"
HEALTH_BORDER = "#96cfba"

FONT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", "Arial"]
BODY_PT = 11.0
TITLE_PT = 22.0

# (text, background, border, solid) for each kind of message.
SEVERITY_COLOURS: Dict[Severity, Tuple[str, str, str, str]] = {
    Severity.INFO: (PRIMARY, ACCENT_SUBTLE, ACCENT_BORDER, PRIMARY),
    Severity.SUCCESS: (SUCCESS, SUCCESS_BG, SUCCESS_BORDER, SUCCESS_SOLID),
    Severity.WARNING: (WARNING, WARNING_BG, WARNING_BORDER, WARNING_SOLID),
    Severity.ERROR: (ERROR, ERROR_BG, ERROR_BORDER, ERROR_SOLID),
}


def font(point_size: float = BODY_PT, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    value = QFont()
    value.setFamilies(FONT_FAMILIES)
    value.setStyleHint(QFont.StyleHint.SansSerif)
    value.setPointSizeF(point_size)
    value.setWeight(weight)
    return value


def palette() -> QPalette:
    value = QPalette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        value.setColor(group, QPalette.ColorRole.Window, QColor(CANVAS))
        value.setColor(group, QPalette.ColorRole.WindowText, QColor(TEXT))
        value.setColor(group, QPalette.ColorRole.Base, QColor(WHITE))
        value.setColor(group, QPalette.ColorRole.AlternateBase, QColor(SURFACE_SUBTLE))
        value.setColor(group, QPalette.ColorRole.Text, QColor(TEXT))
        value.setColor(group, QPalette.ColorRole.Button, QColor(WHITE))
        value.setColor(group, QPalette.ColorRole.ButtonText, QColor(TEXT))
        value.setColor(group, QPalette.ColorRole.Highlight, QColor(PRIMARY))
        value.setColor(group, QPalette.ColorRole.HighlightedText, QColor(WHITE))
        value.setColor(group, QPalette.ColorRole.Link, QColor(PRIMARY))
        value.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(TEXT_TERTIARY))
        value.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(TEXT))
        value.setColor(group, QPalette.ColorRole.ToolTipText, QColor(WHITE))
    disabled = QPalette.ColorGroup.Disabled
    value.setColor(disabled, QPalette.ColorRole.WindowText, QColor(TEXT_DISABLED))
    value.setColor(disabled, QPalette.ColorRole.Text, QColor(TEXT_DISABLED))
    value.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_DISABLED))
    value.setColor(disabled, QPalette.ColorRole.Base, QColor(SUNKEN))
    return value


_QSS = Template("""
QWidget { color: $TEXT; }
QMainWindow, QWidget#central, QWidget#pageBody, QWidget#pageColumn { background: $CANVAS; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QToolTip { background: $TEXT; color: $WHITE; border: none; padding: 6px 8px; font-size: 11pt; }

QFrame#topBar { background: $BRAND; }
QFrame#topBar QLabel { color: $WHITE; background: transparent; }
QLabel#appName { font-size: 14pt; font-weight: 700; }
QLabel#appTagline { font-size: 11pt; }
QLabel#userName { font-size: 11pt; font-weight: 600; }
QPushButton#signOut { background: transparent; color: $WHITE; border: 1px solid $WHITE;
    border-radius: 8px; min-height: 34px; padding: 0 14px; font-size: 11pt; font-weight: 600; }
QPushButton#signOut:hover { background: $PRIMARY; }
QPushButton#signOut:focus { border: 2px solid $NAVY_DARK; }
QPushButton#signOut:disabled { color: $ACCENT_MUTED; border-color: $ACCENT_BORDER; }

QFrame#chip { background: $WHITE; border-radius: 15px; }
QFrame#chip QLabel { color: $TEXT; font-size: 11pt; font-weight: 600; }

QFrame#testRibbon { background: $WARNING_BG; border-bottom: 1px solid $WARNING_BORDER; }
QFrame#testRibbon QLabel { color: $WARNING; font-size: 11pt; font-weight: 700; }

QFrame#stepBar { background: $WHITE; border-bottom: 1px solid $BORDER; }

QLabel#pageTitle { font-size: ${TITLE_PT}pt; font-weight: 700; color: $TEXT; }
QLabel#pageSubtitle { font-size: 12pt; color: $TEXT_SECONDARY; }
QFrame#footer { background: $WHITE; border-top: 1px solid $BORDER; }

QFrame#card { background: $WHITE; border: 1px solid $BORDER; border-radius: 12px; }
QFrame#cardSoft { background: $SURFACE_SUBTLE; border: 1px solid $BORDER; border-radius: 12px; }
QFrame#card[state="active"] { border: 2px solid $PRIMARY; background: $ACCENT_WASH; }
QFrame#card[state="done"] { border: 1px solid $SUCCESS_BORDER; }
QFrame#card[state="waiting"] { background: $WHITE; }

QLabel { font-size: 11pt; }
QLabel[role="h2"] { font-size: 14pt; font-weight: 700; }
QLabel[role="h3"] { font-size: 12pt; font-weight: 700; }
QLabel[role="muted"] { color: $TEXT_SECONDARY; }
QLabel[role="caption"] { color: $TEXT_TERTIARY; font-size: 10pt; }
QLabel[role="fieldLabel"] { font-size: 11pt; font-weight: 700; }
QLabel[role="hint"] { color: $TEXT_SECONDARY; font-size: 11pt; }
QLabel[role="error"] { color: $ERROR; font-size: 11pt; font-weight: 600; }
QLabel[role="value"] { font-size: 12pt; font-weight: 600; }
QLabel[role="key"] { color: $TEXT_SECONDARY; }
QLabel[role="big"] { font-size: 16pt; font-weight: 700; }
QLabel[role="mono"] { font-size: 15pt; font-weight: 700; color: $TEXT; }
QLabel[role="stepNumber"] { background: $PRIMARY; color: $WHITE; border-radius: 14px;
    font-size: 11pt; font-weight: 700; min-width: 28px; max-width: 28px; min-height: 28px;
    max-height: 28px; }
QLabel[role="groupLife"] { background: $LIFE_BG; color: $LIFE_TEXT;
    border: 1px solid $ACCENT_BORDER;
    border-radius: 13px; padding: 3px 14px; font-weight: 700; }
QLabel[role="groupHealth"] { background: $HEALTH_BG; color: $HEALTH_TEXT;
    border: 1px solid $HEALTH_BORDER;
    border-radius: 13px; padding: 3px 14px; font-weight: 700; }

QPushButton { min-height: 44px; padding: 0 22px; border-radius: 8px; font-size: 11pt;
    font-weight: 700; }
QPushButton[kind="primary"] { background: $PRIMARY; color: $WHITE; border: 2px solid $PRIMARY; }
QPushButton[kind="primary"]:hover { background: $PRIMARY_HOVER; border-color: $PRIMARY_HOVER; }
QPushButton[kind="primary"]:pressed { background: $NAVY; border-color: $NAVY; }
QPushButton[kind="primary"]:focus { border: 2px solid $NAVY_DARK; }
QPushButton[kind="primary"]:disabled { background: $BORDER_STRONG;
    border-color: $BORDER_STRONG; color: $TEXT_TERTIARY; }
QPushButton[kind="secondary"] { background: $WHITE; color: $PRIMARY;
    border: 1px solid $BORDER_INPUT; }
QPushButton[kind="secondary"]:hover { background: $ACCENT_SUBTLE; border-color: $PRIMARY; }
QPushButton[kind="secondary"]:pressed { background: $ACCENT_MUTED; }
QPushButton[kind="secondary"]:focus { border: 2px solid $PRIMARY; }
QPushButton[kind="secondary"]:disabled { color: $TEXT_DISABLED; border-color: $BORDER;
    background: $SURFACE_SUBTLE; }
QPushButton[kind="danger"] { background: $WHITE; color: $ERROR; border: 1px solid $ERROR_BORDER; }
QPushButton[kind="danger"]:hover { background: $ERROR_BG; border-color: $ERROR; }
QPushButton[kind="danger"]:focus { border: 2px solid $ERROR; }
QPushButton[kind="link"] { background: transparent; color: $PRIMARY; border: 2px solid transparent;
    padding: 0 6px; min-height: 36px; text-decoration: underline; text-align: left; }
QPushButton[kind="link"]:hover { color: $PRIMARY_HOVER; background: $ACCENT_SUBTLE; }
QPushButton[kind="link"]:focus { border: 2px solid $FOCUS; }
QPushButton[kind="link"]:disabled { color: $TEXT_DISABLED; }
QPushButton[kind="toggle"] { background: transparent; color: $PRIMARY;
    border: 2px solid transparent;
    padding: 0 8px; min-height: 40px; text-align: left; font-size: 12pt; }
QPushButton[kind="toggle"]:hover { background: $ACCENT_SUBTLE; }
QPushButton[kind="toggle"]:focus { border: 2px solid $FOCUS; }

QPushButton[kind="choice"] { background: $WHITE; border: 1px solid $BORDER_STRONG;
    border-radius: 12px;
    padding: 0; text-align: left; }
QPushButton[kind="choice"]:hover { background: $ACCENT_WASH; border: 1px solid $PRIMARY; }
QPushButton[kind="choice"]:focus { border: 2px solid $PRIMARY; }
QPushButton[kind="choice"]:checked { background: $ACCENT_SUBTLE; border: 2px solid $PRIMARY; }
QPushButton[kind="choice"][emphasis="true"] { border: 2px solid $ACCENT_BORDER; }
QPushButton[kind="choice"][emphasis="true"]:hover { border: 2px solid $PRIMARY; }
QPushButton[kind="choice"][emphasis="true"]:focus { border: 2px solid $PRIMARY; }
QPushButton[kind="choice"]:disabled { background: $SUNKEN; border: 1px solid $BORDER; }
QPushButton[kind="choice"][emphasis="true"]:disabled { border: 1px solid $BORDER; }
QLabel[role="choiceTitle"] { font-size: 14pt; font-weight: 700; color: $TEXT; }
QLabel[role="choiceTitleSmall"] { font-size: 12pt; font-weight: 700; color: $TEXT; }
QLabel[role="choiceText"] { font-size: 11pt; color: $TEXT_SECONDARY; }
QLabel[role="choiceTitle"]:disabled, QLabel[role="choiceTitleSmall"]:disabled,
QLabel[role="choiceText"]:disabled { color: $TEXT_DISABLED; }

QToolButton#eye { background: $WHITE; border: 1px solid $BORDER_INPUT; border-radius: 8px;
    min-width: 44px; min-height: 44px; max-width: 44px; max-height: 44px; }
QToolButton#eye:hover { background: $ACCENT_SUBTLE; border-color: $PRIMARY; }
QToolButton#eye:focus { border: 2px solid $PRIMARY; }
QToolButton#copy { background: $WHITE; color: $PRIMARY; border: 1px solid $BORDER_INPUT;
    border-radius: 8px;
    min-height: 40px; padding: 0 14px; font-size: 11pt; font-weight: 700; }
QToolButton#copy:hover { background: $ACCENT_SUBTLE; border-color: $PRIMARY; }
QToolButton#copy:focus { border: 2px solid $PRIMARY; }

QLineEdit, QComboBox { min-height: 44px; border: 1px solid $BORDER_INPUT; border-radius: 8px;
    padding: 0 12px; background: $WHITE; color: $TEXT; font-size: 12pt;
    selection-background-color: $ACCENT_MUTED; selection-color: $TEXT; }
QLineEdit:hover, QComboBox:hover { border-color: $TEXT_DISABLED; }
QLineEdit:focus, QComboBox:focus { border: 2px solid $PRIMARY; padding: 0 11px; }
QLineEdit[invalid="true"], QComboBox[invalid="true"] { border: 2px solid $ERROR; padding: 0 11px; }
QLineEdit:disabled, QComboBox:disabled { background: $SUNKEN; color: $TEXT_TERTIARY; }
QLineEdit[readOnly="true"] { background: $SUNKEN; border-color: $BORDER; color: $TEXT_SECONDARY; }
QComboBox::drop-down { border: none; width: 40px; }
QComboBox::down-arrow { image: url("$CHEVRON_DOWN"); width: 18px; height: 18px; }
QComboBox QAbstractItemView { background: $WHITE; border: 1px solid $BORDER_STRONG; outline: 0;
    selection-background-color: $ACCENT_SUBTLE; selection-color: $TEXT; padding: 4px;
    font-size: 12pt; }

QCheckBox { spacing: 12px; font-size: 12pt; font-weight: 600; color: $TEXT; }
QCheckBox::indicator { width: 24px; height: 24px; border: 2px solid $TEXT_DISABLED;
    border-radius: 6px;
    background: $WHITE; }
QCheckBox::indicator:hover { border-color: $PRIMARY; }
QCheckBox::indicator:checked { background: $PRIMARY; border-color: $PRIMARY;
    image: url("$CHECK_WHITE"); }
QCheckBox:focus { color: $PRIMARY; }

QProgressBar { background: $BORDER; border: none; border-radius: 4px; max-height: 8px;
    min-height: 8px;
    color: transparent; text-align: center; }
QProgressBar::chunk { background: $PRIMARY; border-radius: 4px; }

QScrollBar:vertical { background: transparent; width: 14px; margin: 2px; }
QScrollBar::handle:vertical { background: $BORDER_STRONG; border-radius: 5px; min-height: 48px; }
QScrollBar::handle:vertical:hover { background: $TEXT_DISABLED; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QFrame#advice { border-radius: 12px; }
QLabel#adviceTitle { font-size: 14pt; font-weight: 700; }
QLabel[role="bullet"] { color: $WHITE; border-radius: 12px; font-size: 10pt; font-weight: 700;
    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; }

QFrame#helpPanel { background: $WHITE; border-left: 1px solid $BORDER; }
QFrame#helpHeader { background: $WHITE; border-bottom: 1px solid $BORDER; }
QFrame#console { background: $SURFACE_SUBTLE; border: 1px solid $BORDER; border-radius: 10px; }
QFrame#credential { background: $ACCENT_SUBTLE; border: 1px solid $ACCENT_BORDER;
    border-radius: 10px; }
""")


def _severity_rules() -> str:
    rules = []
    for severity, (text, background, border, solid) in SEVERITY_COLOURS.items():
        name = severity.value
        rules.append(
            f'QFrame#advice[severity="{name}"] {{ background: {background}; '
            f"border: 1px solid {border}; }}"
        )
        rules.append(f'QFrame#advice[severity="{name}"] QLabel#adviceTitle {{ color: {text}; }}')
        # Numbers sit on the darker text colour: white on the light "solid" shades is too faint.
        rules.append(
            f'QFrame#advice[severity="{name}"] QLabel[role="bullet"] {{ background: {text}; }}'
        )
    return "\n".join(rules)


def style_sheet() -> str:
    values = {
        name: value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, str)
    }
    values["TITLE_PT"] = f"{TITLE_PT:g}"
    values["CHEVRON_DOWN"] = pictures.ui_icon_path("chevron-down")
    values["CHECK_WHITE"] = pictures.ui_icon_path("check-white")
    return _QSS.substitute(values) + "\n" + _severity_rules()


def apply(app: QApplication) -> None:
    """Set the font, palette and style sheet once.

    Setting a style sheet re-styles every widget that exists, so it is not
    repeated when nothing changed.
    """
    sheet = style_sheet()
    if app.styleSheet() == sheet:
        return
    app.setFont(font())
    app.setPalette(palette())
    app.setStyleSheet(sheet)


def repolish(widget) -> None:
    """Re-apply the style sheet after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
