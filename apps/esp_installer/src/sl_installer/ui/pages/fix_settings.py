"""New settings for a board that is already set up (from what ADD knows).

Head office knows only the board's place: ZONE ID, ZONE NAME and device number.
A locked board cannot be read back, so everything else it has (the site's
Wi-Fi, the comm key or the Hikvision machine's details) is written again from
this page. The page says so, and asks for the values that live on the
attendance machine instead of quietly writing the usual ones (review E3, F2).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QComboBox, QVBoxLayout, QWidget

from ...core import api
from ...core.api import Advice, Family, Severity
from .. import naming, wording
from ..widgets import AdviceCard, BasePage, Card, FactList, FieldBlock, Notice, UpperCaseEdit
from . import FIX_SETTINGS
from .settings_form import SettingsForm

_SUBTITLE_KNOWN = "Head office knows only the zone and the device number. Check everything else."
_SUBTITLE_UNKNOWN = "Head office does not know this board. Type all of its settings."
_SUBTITLE_BLOCKED = "New settings cannot be written to this board now. Read why below."


def data_loss_warning(zone_id: str, record: Optional[api.DeviceRecord] = None) -> Advice:
    """The board was used at a site: punches it still holds are deleted with new settings."""
    status = ((record.status_text if record is not None else None) or "").strip()
    if record is not None and record.online is False:
        # Say what head office knows instead of hoping the board was online lately.
        lately = "head office has not heard from it lately"
        if status:
            lately = f"{lately} ({status})"
        happened = (
            f"This board has been used at a site, and {lately}. Punches it took since then "
            "are still on the board. They are deleted when its settings change."
        )
        steps = (
            "If you can, put the board back at its site until head office sees it online, so "
            "its punches are sent first.",
            "If you are not sure, call head office before you go on.",
            f"To go on anyway, type {zone_id} in the box below.",
        )
    else:
        happened = (
            "This board has been used at a site. Punches it has not sent to head office yet "
            "are deleted when its settings change."
        )
        steps = (
            "Make sure the board was online in the last hour, so its punches were sent.",
            f"To go on, type {zone_id} in the box below.",
        )
    return Advice(
        "UNSENT_PUNCHES_LOST",
        "Punches still on the board will be lost",
        happened,
        steps,
        Severity.ERROR,
        None,
    )


def what_is_written(family: Family, known: bool) -> str:
    """Which settings come from head office, and which the person must check."""
    if family is Family.HIKVISION:
        check = (
            "the attendance machine's address, password, model and serial number. Type them "
            "from the machine."
        )
    else:
        check = (
            "the comm key (type it from the attendance machine), and the machine address and "
            "port under Advanced settings."
        )
    source = (
        "From head office: the ZONE ID, the ZONE NAME and the device number."
        if known
        else "Head office does not know this board, so nothing comes from head office."
    )
    return (
        f"{source}\nYou must check {check} The board's own settings cannot be read, so the "
        "usual ones are written unless you change them.\nThe board also forgets the site's "
        "Wi-Fi. At the site, choose it again with a phone."
    )


class FixSettingsPage(BasePage):
    page_id = FIX_SETTINGS
    help_topic = "fix_settings"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Change the board's settings", _SUBTITLE_KNOWN, parent)
        # Why nothing can be written to this board (head office not reached, not allowed,
        # or a ZONE ID that board settings cannot hold). Next stays grey while it shows.
        self.blocker = AdviceCard()
        self.blocker.hide()
        self.blocker.action.connect(self.action)
        self.content.addWidget(self.blocker)
        self._blocked = False

        self.problem = AdviceCard()
        self.problem.hide()
        self.problem.action.connect(self.action)
        self.content.addWidget(self.problem)

        # The data-loss warning and its typed confirmation come first: Next waits for them.
        self.warning = AdviceCard()
        self.confirm_edit = UpperCaseEdit()
        self.confirm_field = FieldBlock("Type the ZONE ID to go on", self.confirm_edit)
        self.warning_card = QWidget()
        box = QVBoxLayout(self.warning_card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        box.addWidget(self.warning)
        box.addWidget(self.confirm_field)
        self.content.addWidget(self.warning_card)
        self.warning_card.hide()

        self.what_changes = Notice()
        self.content.addWidget(self.what_changes)

        about = Card("This board")
        self.facts = FactList()
        about.body.addWidget(self.facts)
        self.family_choice = QComboBox()
        for family in (Family.ZKT, Family.HIKVISION):
            self.family_choice.addItem(wording.family_name(family), family.value)
        self.family_field = FieldBlock(
            "Machine type",
            self.family_choice,
            "Head office does not know this board's machine type. Choose it.",
        )
        about.body.addWidget(self.family_field)
        self.about = about
        self.content.addWidget(about)

        self.form = SettingsForm(show_bundles=False, fix_mode=True)
        self.content.addWidget(self.form)
        self._confirm_value = ""
        self._known = True
        self.confirm_edit.textChanged.connect(self._confirm_changed)
        self.family_choice.currentIndexChanged.connect(self._family_changed)
        self.set_primary("Next", "next")
        self.set_back(True)

    def load(
        self,
        record: api.DeviceRecord,
        board: Optional[api.BoardInfo],
        family: Optional[Family],
        defaults: api.FieldDefaults,
    ) -> None:
        known_family = record.family if record.known and record.family else None
        chosen = known_family or family or Family.ZKT
        self.family_field.setVisible(known_family is None)
        self.family_choice.blockSignals(True)
        self.family_choice.setCurrentIndex(self.family_choice.findData(chosen.value))
        self.family_choice.blockSignals(False)
        rows = [("Board ID", naming.board_id(board.mac if board else record.mac))]
        if known_family:
            rows.append(("Machine type", wording.family_name(known_family)))
        if record.firmware_version:
            rows.append(("Board software", f"{record.firmware_version} (stays the same)"))
        if record.status_text:
            rows.append(("Status", record.status_text))
        self.facts.set_rows(rows)
        if record.known:
            self.form.load_record(record, chosen, defaults)
        else:
            self.form.reset(chosen, defaults)
        self._known = record.known  # the subtitle follows (see block)
        self._show_what_changes()
        self._confirm_value = (record.zone_id or "").strip().upper() if record.ever_online else ""
        self.confirm_edit.clear()
        self.confirm_field.set_error(None)
        if self._confirm_value:
            self.warning.set_advice(data_loss_warning(self._confirm_value, record))
            self.confirm_field.label.setText(f"Type {self._confirm_value} to go on")
            self.confirm_edit.setAccessibleName(f"Type {self._confirm_value} to go on")
        self.show_problem(None)
        self._set_parts_visible(True)
        self.warning_card.setVisible(bool(self._confirm_value))
        stored = (record.zone_id or "").strip().upper() if record.known else ""
        if stored and naming.zone_id_problem(stored):
            # Head office's own ZONE ID breaks the board-settings rule (for example the "&"
            # in LF-ZONE-G&PBLD8-01): never ask the person to edit what head office gave.
            # Nothing can be written, so there is nothing to fill in or to check.
            self.form.zone_id_field.set_error(None)
            for part in (self.warning_card, self.what_changes, self.form):
                part.hide()
            self.block(wording.zone_not_writable(record.zone_id or stored))
        else:
            self.block(None)
        self._confirm_changed(self.confirm_edit.text())

    def show_unavailable(self, advice: Advice, retry: bool = False) -> None:
        """No settings can be loaded or written: say why, and show nothing to fill in."""
        self._set_parts_visible(False)
        self.warning_card.hide()
        self.show_problem(None)
        self.block(advice, retry)

    def _set_parts_visible(self, visible: bool) -> None:
        for part in (self.what_changes, self.about, self.form):
            part.setVisible(visible)

    def block(self, advice: Optional[Advice], retry: bool = False) -> None:
        """Keep Next grey and say why (None: nothing in the way)."""
        self._blocked = advice is not None
        if advice is None:
            self.blocker.hide()
            self.set_subtitle(_SUBTITLE_KNOWN if self._known else _SUBTITLE_UNKNOWN)
        else:
            self.set_subtitle(_SUBTITLE_BLOCKED)
            self.blocker.set_advice(advice, [("Try again", "reload", "secondary")] if retry else [])
            self.blocker.show()
            self.reveal(self.blocker)
        self.set_primary_enabled(self.confirmed() and not self._blocked)

    @property
    def blocked(self) -> bool:
        return self._blocked

    def _show_what_changes(self) -> None:
        self.what_changes.set_notice(
            "Check the settings below before you go on",
            what_is_written(self.chosen_family(), self._known),
            Severity.WARNING,
        )

    def _family_changed(self, _index: int) -> None:
        # Only the form changes: a settings change needs no board software list (F3).
        self.form.set_family(self.chosen_family())
        self._show_what_changes()

    def chosen_family(self) -> Family:
        return Family(self.family_choice.currentData() or Family.ZKT.value)

    def _confirm_changed(self, value: str) -> None:
        self.confirm_field.set_error(None)
        self.set_primary_enabled(self.confirmed() and not self._blocked)

    def confirmed(self) -> bool:
        return (
            not self._confirm_value
            or self.confirm_edit.text().strip().upper() == self._confirm_value
        )

    def check(self) -> bool:
        """Form problems, and the typed ZONE ID when punches may be lost."""
        if self._blocked:
            return False
        if not self.form.validate():
            return False
        if not self.confirmed():
            self.confirm_field.set_error(f"Type {self._confirm_value} exactly.")
            self.confirm_edit.setFocus()
            return False
        return True

    def show_problem(self, advice: Optional[Advice], retry: bool = True) -> None:
        if advice is None:
            self.problem.hide()
            return
        self.problem.set_advice(advice, [("Try again", "reload", "secondary")] if retry else [])
        self.problem.show()
        self.reveal(self.problem)

    def first_focus(self) -> Optional[QWidget]:
        if self._blocked:
            return None
        return self.confirm_edit if self.needs_typed_confirmation else self.form.zone_id

    @property
    def needs_typed_confirmation(self) -> bool:
        return bool(self._confirm_value) and not self._blocked
