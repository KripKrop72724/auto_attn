"""The board is ready: what was set, and how to choose the site's Wi-Fi."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ...core.api import Advice, Severity
from .. import naming, wording
from ..widgets import (
    AdviceCard,
    BasePage,
    Card,
    FactList,
    HotspotDetails,
    Notice,
    Picture,
    button,
    clear_layout,
    text,
)
from . import DONE
from .connect import numbered_steps


class DonePage(BasePage):
    page_id = DONE
    help_topic = "done"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "The board is ready", "Take the board to the site. Then follow the steps below.", parent
        )
        # What the person needs at the site comes first; what was set comes after.
        self.hik_notice = Notice()
        self.hik_notice.hide()
        self.content.addWidget(self.hik_notice)

        site = Card("At the site: give the board the site's Wi-Fi")
        site.body.addWidget(
            text(
                "If the board cannot join a Wi-Fi at the site, it opens its own Wi-Fi after "
                "2 minutes. Use a phone and these details:",
                "muted",
            )
        )
        self.credentials = HotspotDetails()
        site.body.addWidget(self.credentials)
        site_row = QHBoxLayout()
        site_row.setSpacing(28)
        self._steps_holder = QVBoxLayout()
        site_row.addLayout(self._steps_holder, 1)
        site_row.addWidget(Picture("hotspot", QSize(200, 150)), 0, Qt.AlignmentFlag.AlignTop)
        site.body.addLayout(site_row)
        site.body.addWidget(
            text(
                "If the test fails, the board keeps its old Wi-Fi: check the password and try "
                "again. To open the board's Wi-Fi at any time, hold the BOOT button for "
                "5 seconds while the board is on.",
                "muted",
            )
        )
        self.content.addWidget(site)

        summary = Card()
        top = QHBoxLayout()
        top.setSpacing(28)
        top.addWidget(Picture("success", QSize(160, 120)), 0, Qt.AlignmentFlag.AlignTop)
        facts = QVBoxLayout()
        facts.setSpacing(12)
        facts.addWidget(text("What was set", "h2"))
        self.facts = FactList(key_width=170)
        facts.addWidget(self.facts)
        top.addLayout(facts, 1)
        summary.body.addLayout(top)
        self.content.addWidget(summary)

        self.saved = AdviceCard()
        self.saved.hide()
        self.saved.action.connect(self.action)
        self.content.addWidget(self.saved)

        links = QHBoxLayout()
        self.home_link = button("Go back to the start", "link")
        self.home_link.clicked.connect(lambda: self.action.emit("home"))
        links.addWidget(self.home_link)
        links.addStretch(1)
        self.content.addLayout(links)

        self.set_primary("Set up another board", "another")
        self.set_secondary("Save a record", "save_record")
        self.set_back(False)

    def show_done(
        self,
        rows: Sequence[Tuple[str, str]],
        mac: str,
        hotspot_password: str,
        hikvision: bool,
        title: str = "The board is ready",
    ) -> None:
        self.set_title(title)
        self.facts.set_rows(rows)
        hotspot = naming.hotspot_name(mac) or "SLIC ATTENDANCE"
        while self._steps_holder.count():
            item = self._steps_holder.takeAt(0)
            if item.layout() is not None:
                clear_layout(item.layout())
        self._steps_holder.addLayout(numbered_steps(wording.portal_steps(hotspot), role=None))
        self.credentials.set_hotspot(naming.hotspot_name(mac), hotspot_password)
        if hikvision:
            self.hik_notice.set_notice(
                "Tell head office this board is ready",
                "Head office must switch on attendance for this Hikvision machine before punches "
                "reach "
                "the HR system.",
                Severity.INFO,
            )
            self.hik_notice.show()
        else:
            self.hik_notice.hide()
        self.saved.hide()
        self.set_secondary("Save a record", "save_record")

    def show_saved(self, path: str) -> None:
        advice = Advice(
            "RECORD_SAVED", "Record saved", f"The record is saved in {path}", (), Severity.SUCCESS
        )
        self.saved.set_advice(advice, [("Open the folder", "open_folder", "secondary")])
        self.saved.show()
        self.reveal(self.saved)

    def show_save_problem(self, advice: Advice) -> None:
        self.saved.set_advice(advice, [("Try again", "save_record", "secondary")])
        self.saved.show()
        self.reveal(self.saved)
