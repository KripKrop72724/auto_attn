"""The result of a board test: PASSED, NOT FIT FOR USE, or could not finish.

The certificate (PDF) and the evidence file (ZIP) are saved by themselves when
this screen opens (records.save_board_test); the buttons save a copy where the
person chooses. For a locked board that failed its setup the screen also offers
the way on when the fault can be fixed (Finish the setup, Fix the settings),
and a vendor claim when the fault is in the board's hardware.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLineEdit, QWidget

from ...core.api import Advice, SecurityState, Severity
from .. import naming, wording
from ..widgets import (
    AdviceCard,
    BasePage,
    Card,
    FactList,
    StateIcon,
    button,
    clear_layout,
    text,
)
from . import BOARD_TEST_RESULT

_ICON = {
    "ok": StateIcon.DONE,
    "fail": StateIcon.FAILED,
    "warn": StateIcon.SKIPPED,
    "info": StateIcon.SKIPPED,
}
ActionSpec = Tuple[str, str, str]


def _when(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso or "-")
    return moment.strftime("%d %b %Y, %H:%M")


class BoardTestResultPage(BasePage):
    page_id = BOARD_TEST_RESULT
    help_topic = "board_test_result"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("The test result", "", parent)
        self.verdict = AdviceCard(picture_size=QSize(208, 156))
        self.verdict.action.connect(self.action)
        self.content.addWidget(self.verdict)

        # A problem with the way on (for example Fix the settings without a board ID).
        self.problem = AdviceCard()
        self.problem.hide()
        self.problem.action.connect(self.action)
        self.content.addWidget(self.problem)

        self.files_card = files = Card("Certificate and evidence")
        tester_row = QHBoxLayout()
        tester_row.setSpacing(16)
        label = text("Tested by", "key", wrap=False)
        self.tester = QLineEdit()
        self.tester.setAccessibleName("Tested by")
        self.tester.setPlaceholderText("Your name")
        self.tester.setMaxLength(80)
        label.setBuddy(self.tester)
        tester_row.addWidget(label)
        tester_row.addWidget(self.tester, 1)
        files.body.addLayout(tester_row)
        self.tester_hint = text(
            "This name is printed on the certificate. Change it before you save a copy if "
            "someone else did the test.",
            "hint",
        )
        files.body.addWidget(self.tester_hint)
        self.saved = AdviceCard()
        self.saved.hide()
        self.saved.action.connect(self.action)
        files.body.addWidget(self.saved)
        self._extra = QHBoxLayout()
        self._extra.setSpacing(12)
        files.body.addLayout(self._extra)
        self.content.addWidget(files)

        tests = Card("What was tested")
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnStretch(2, 1)
        tests.body.addLayout(self._grid)
        self.content.addWidget(tests)

        about = Card("About this board")
        self.facts = FactList(key_width=190)
        about.body.addWidget(self.facts)
        self.content.addWidget(about)

        links = QHBoxLayout()
        links.setSpacing(18)
        self.retest_link = button("Test again", "link")
        self.retest_link.clicked.connect(lambda: self.action.emit("retest"))
        self.another_link = button("Test another board", "link")
        self.another_link.clicked.connect(lambda: self.action.emit("another"))
        self.home_link = button("Go back to the start", "link")
        self.home_link.clicked.connect(lambda: self.action.emit("home"))
        for link in (self.retest_link, self.another_link, self.home_link):
            links.addWidget(link)
        links.addStretch(1)
        self.content.addLayout(links)

        self.rows: List[Tuple[str, str, str]] = []
        self.set_primary("Save certificate (PDF)", "save_certificate")
        self.set_secondary("Save evidence (ZIP)", "save_evidence")
        self.set_back(False)

    # ------------------------------------------------------------------ api

    def show_report(
        self,
        report: object,
        verdict: Advice,
        tester: str,
        primary: Optional[Tuple[str, str]],
        secondary: Optional[Tuple[str, str]],
        extra: Sequence[ActionSpec] = (),
        recovery: Sequence[ActionSpec] = (),
        retest: bool = True,
        another_words: str = "Test another board",
        files_title: str = "Certificate and evidence",
    ) -> None:
        """Everything about ``report``; the buttons are (words, action) for the footer,
        ``extra`` more save buttons, ``recovery`` the way on (on the result card)."""
        passed = getattr(report, "passed", False)
        failed = getattr(report, "hardware_fault", False)
        subtitle = (
            "The board passed."
            if passed
            else "Do not use this board." if failed else "Read what happened below."
        )
        self.set_title("The test result", subtitle)
        self.verdict.set_advice(verdict, recovery)
        if self.files_card.title_label is not None:
            self.files_card.title_label.setText(files_title)
        self.problem.hide()
        self.tester.setText(tester)
        self._show_tests(report)
        self.facts.set_rows(self._facts(report))
        self.saved.hide()
        clear_layout(self._extra)
        for words, name, kind in extra:
            widget = button(words, kind)
            widget.clicked.connect(lambda _checked=False, value=name: self.action.emit(value))
            self._extra.addWidget(widget)
        self._extra.addStretch(1)
        if primary:
            self.set_primary(primary[0], primary[1])
        else:
            self.set_primary(None)
        if secondary:
            self.set_secondary(secondary[0], secondary[1])
        else:
            self.set_secondary(None)
        self.retest_link.setVisible(retest)
        self.another_link.setText(another_words)
        self.scroll_to_top()

    def tester_name(self) -> str:
        return self.tester.text().strip()

    def _show_tests(self, report: object) -> None:
        clear_layout(self._grid)
        self.rows = []
        findings = tuple(getattr(report, "findings", ()) or ())
        for index, finding in enumerate(findings):
            stage = str(getattr(finding, "stage", "") or "")
            result = str(getattr(finding, "result", "") or "")
            name = wording.TEST_STAGE_WORDS.get(stage, stage.capitalize() or "-")
            title = str(getattr(finding, "title", "") or "")
            words = wording.test_result_words(result)
            icon = StateIcon(24)
            icon.set_state(_ICON.get(result, StateIcon.SKIPPED))
            self._grid.addWidget(icon, index, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(text(name, "h3"), index, 1, Qt.AlignmentFlag.AlignTop)
            shown = f"{words}. {title}" if title and result != "ok" else (title or words)
            self._grid.addWidget(text(shown, "value"), index, 2)
            self.rows.append((name, result, shown))
        if not findings:
            self._grid.addWidget(text("No test results.", "muted"), 0, 0, 1, 3)

    @staticmethod
    def _facts(report: object) -> List[Tuple[str, str]]:
        mac = str(getattr(report, "mac", "") or "")
        rows = [("Board ID", naming.board_id(mac) if mac else "-")]
        rows.append(("Tested on", _when(getattr(report, "finished_at", ""))))
        unchanged = getattr(report, "efuses_unchanged", None)
        erased = getattr(report, "erased", None)
        board = getattr(report, "board", None)
        locked = bool(getattr(board, "secure_download_mode", False)) or getattr(
            board, "security", None) in (SecurityState.SECURED_STATE_LIFE,
                                         SecurityState.SECURED_OTHER)
        # The alarm first: a change during the test outranks everything else said here.
        if unchanged is False:
            security = "Changed during the test. Tell head office at once."
        elif getattr(report, "keys_burned", False):
            security = "Locked for State Life by its setup, before this check."
        elif unchanged is True:
            security = "Not changed by the test."
        elif locked:
            security = "Could not be read. The board is locked."
        elif erased is False:
            security = "Could not be checked after the test."
        else:
            security = "Not checked (the test did not get that far)."
        rows.append(("Board's security", security))
        if erased is not None:
            rows.append(
                (
                    "Test program",
                    "Removed from the board."
                    if erased
                    else "Still on the board. Click Test again to remove it.",
                )
            )
        return rows

    # ---------------------------------------------------------------- files

    def set_saving(self) -> None:
        self.saved.set_advice(
            Advice("BOARD_TEST_SAVING", "Saving", "Saving the certificate and the evidence file.",
                   (), Severity.INFO)
        )
        self.saved.show()

    def show_saved(self, folder: str) -> None:
        self.saved.set_advice(
            wording.board_test_saved(folder), [("Open the folder", "open_folder", "secondary")]
        )
        self.saved.show()

    def show_copied(self, path: str) -> None:
        self.saved.set_advice(
            wording.board_test_copied(path), [("Open the folder", "open_folder", "secondary")]
        )
        self.saved.show()
        self.reveal(self.saved)

    def show_save_problem(self, advice: Advice) -> None:
        self.saved.set_advice(advice, [("Try again", "save_again", "secondary")])
        self.saved.show()
        self.reveal(self.saved)

    def show_problem(self, advice: Advice, actions: Sequence[ActionSpec] = ()) -> None:
        self.problem.set_advice(advice, actions)
        self.problem.show()
        self.reveal(self.problem)

    def first_focus(self) -> Optional[QWidget]:
        return self.verdict.first_button() or self.primary_button
