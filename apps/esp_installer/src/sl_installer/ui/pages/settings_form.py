"""The board's settings, as one form for "Details" and "Fix settings".

Only ZONE ID (and, for Hikvision, the machine's address, password, model and
serial number) is in view; everything else sits under Advanced with the owner's defaults
(services.defaults). The checks mirror the server's, so mistakes show up here,
next to the field, in plain words.

In Fix settings (``fix_mode``) the board's own settings cannot be read back, so
the values that live on the attendance machine are asked for instead of
defaulted: the ZKTeco comm key moves out of Advanced and starts empty, the
Hikvision model starts at "Choose the model", and Advanced is open so the
values that will be written can be seen (review E3, F2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core import api
from ...core.api import BundleInfo, Family, FieldDefaults
from .. import naming, theme, wording
from ..widgets import (
    Badge,
    Card,
    Collapsible,
    FieldBlock,
    Notice,
    PasswordInput,
    UpperCaseEdit,
    button,
    text,
)

TRANSPORTS = (("http_digest", "Normal (HTTP)"), ("https", "Secure (HTTPS)"))
_TRANSPORT_PORTS = {"http_digest": 80, "https": 443}

CHOOSE_MODEL = "Choose the model"
_COMM_KEY_HINT = "Shown in the machine's menu, under Comm."
_FIX_COMM_KEY_HINT = (
    "On the attendance machine, open the menu and look under Comm. Type the comm key shown "
    "there. The board's own key cannot be read."
)
_HIK_PASSWORD_HINT = "The password you use to open the machine's settings."
_FIX_HIK_PASSWORD_HINT = (
    "The password you use to open the machine's settings. The board's own copy cannot be "
    "read, so type it again."
)


@dataclass(frozen=True)
class FormValues:
    family: Family
    zone_id: str
    zone_name: str
    device_id: str
    wifi_ssid: str
    wifi_password: str
    bundle_id: Optional[str] = None
    zkt_port: int = 4370
    zkt_comm_key: int = 0
    zkt_preferred_ip: str = "0.0.0.0"
    hik_host: str = ""
    hik_password: str = ""
    hik_username: str = "admin"
    hik_port: int = 80
    hik_transport: str = "http_digest"
    hik_serial: str = ""
    hik_model_key: str = api.HIK_MODELS[0].key


def model_for(key: str) -> api.HikModel:
    for model in api.HIK_MODELS:
        if model.key == key:
            return model
    return api.HIK_MODELS[-1]


def to_config(values: FormValues, bundle_id: str, hik_source_epoch: str = "") -> api.InstallConfig:
    """The engine's InstallConfig. The Hikvision source epoch is left empty for
    the engine to fill (<zone id>-<date>) unless ADD already has one."""
    zkt = hik = None
    if values.family is Family.HIKVISION:
        hik = api.HikSettings(
            host=values.hik_host,
            password=values.hik_password,
            username=values.hik_username,
            port=values.hik_port,
            transport=values.hik_transport,
            expected_serial=values.hik_serial,
            model_key=values.hik_model_key,
            profile=model_for(values.hik_model_key).profile,
            source_epoch=hik_source_epoch or "",
        )
    else:
        zkt = api.ZktSettings(
            port=values.zkt_port,
            comm_key=values.zkt_comm_key,
            preferred_ip=values.zkt_preferred_ip,
        )
    return api.InstallConfig(
        family=values.family,
        zone_id=values.zone_id,
        zone_name=values.zone_name,
        device_id=values.device_id,
        wifi_ssid=values.wifi_ssid,
        wifi_password=values.wifi_password,
        bundle_id=bundle_id,
        zkt=zkt,
        hik=hik,
    )


def sorted_bundles(bundles: Sequence[BundleInfo]) -> List[BundleInfo]:
    """Recommended first, then newest first."""
    return sorted(
        bundles,
        key=lambda item: (
            not item.recommended,
            tuple(-part for part in naming.version_tuple(item.version)),
        ),
    )


def bundle_words(bundle: BundleInfo) -> str:
    return f"{bundle.label} (recommended)" if bundle.recommended else bundle.label


class SettingsForm(QWidget):
    changed = Signal()

    def __init__(
        self, show_bundles: bool = True, fix_mode: bool = False, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._fix_mode = fix_mode
        self._family = Family.ZKT
        self._defaults = FieldDefaults()
        self._zones: List[api.ZoneSummary] = []
        self._name_custom = False
        self._device_custom = False
        self._port_custom = False
        self._wanted_bundle: Optional[str] = None
        self._show_bundles = show_bundles
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        # ------------------------------------------------------------ place
        place = Card(padding=22)
        self.zone_id = UpperCaseEdit()
        self.zone_id.setMaxLength(64)
        self.zone_id.setPlaceholderText("For example LF-ZONE-BLD9-03")
        self.zone_id_field = FieldBlock("ZONE ID", self.zone_id, "Head office gives you this.")
        self._zone_list = QStringListModel(self)
        self._completer = QCompleter(self._zone_list, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.zone_id.setCompleter(self._completer)

        name_row = QWidget()
        name_layout = QHBoxLayout(name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(10)
        self.zone_name = QLineEdit()
        self.zone_name.setMaxLength(120)
        self.zone_name.setReadOnly(True)
        self.change_name = button("Change", "link")
        self.change_name.setAccessibleName("Change the ZONE NAME")
        name_layout.addWidget(self.zone_name, 1)
        name_layout.addWidget(self.change_name)
        self.zone_name_field = FieldBlock(
            "ZONE NAME", name_row, "Filled in from the ZONE ID.", target=self.zone_name
        )
        names = QGridLayout()
        names.setHorizontalSpacing(24)
        names.setColumnStretch(0, 1)
        names.setColumnStretch(1, 1)
        names.addWidget(self.zone_id_field, 0, 0, Qt.AlignmentFlag.AlignTop)
        names.addWidget(self.zone_name_field, 0, 1, Qt.AlignmentFlag.AlignTop)
        place.body.addLayout(names)

        preview = Card(soft=True, padding=16, spacing=2)
        preview_row = QHBoxLayout()
        preview_row.setSpacing(14)
        preview_words = QVBoxLayout()
        preview_words.setSpacing(2)
        preview_words.addWidget(text("Name on the public status page", "muted"))
        self.preview_name = text("", "big")
        preview_words.addWidget(self.preview_name)
        preview_row.addLayout(preview_words, 1)
        self.preview_group = Badge("", "groupLife")
        self.preview_group.setToolTip(
            "Names that start with LF are in the Life group. Other names are in the Health group."
        )
        preview_row.addWidget(self.preview_group, 0, Qt.AlignmentFlag.AlignVCenter)
        preview.body.addLayout(preview_row)
        place.body.addWidget(preview)
        self.preview = preview
        layout.addWidget(place)

        # --------------------------------------------------- Hikvision machine
        machine = Card("The attendance machine")
        self.hik_host = QLineEdit()
        self.hik_host.setMaxLength(15)
        self.hik_host.setPlaceholderText("For example 192.168.1.20")
        self.hik_host_field = FieldBlock(
            "Machine address", self.hik_host, "Find it in the machine's menu, under Network."
        )
        self.hik_password = PasswordInput()
        self.hik_password.line_edit.setMaxLength(127)
        self.hik_password_field = FieldBlock(
            "Machine password",
            self.hik_password,
            _FIX_HIK_PASSWORD_HINT if fix_mode else _HIK_PASSWORD_HINT,
        )
        self.hik_model = QComboBox()
        if fix_mode:
            # No guess: the wrong model would give the machine the wrong profile.
            self.hik_model.addItem(CHOOSE_MODEL, None)
        for model in api.HIK_MODELS:
            self.hik_model.addItem(model.label, model.key)
        self.hik_model_field = FieldBlock(
            "Machine model", self.hik_model, "Printed on a label on the back of the machine."
        )
        # Required and with no default: the board checks it against the machine's own
        # answer, letter for letter, before it reads any punches (and ADD binds it).
        self.hik_serial = QLineEdit()
        self.hik_serial.setMaxLength(119)
        self.hik_serial_field = FieldBlock(
            "Machine serial number",
            self.hik_serial,
            "Find it in the machine's menu. Type it exactly, letter by letter: the board works "
            "only with the machine that has this number.",
        )
        self.model_notice = Notice()
        self.model_notice.hide()
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(16)
        top = Qt.AlignmentFlag.AlignTop
        grid.addWidget(self.hik_host_field, 0, 0, top)
        grid.addWidget(self.hik_password_field, 0, 1, top)
        grid.addWidget(self.hik_model_field, 1, 0, top)
        grid.addWidget(self.hik_serial_field, 1, 1, top)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._machine_grid = grid
        machine.body.addLayout(grid)
        machine.body.addWidget(self.model_notice)
        self.machine_card = machine
        layout.addWidget(machine)

        # ----------------------------------------------------- board software
        software = Card("Board software")
        self.bundle = QComboBox()
        self.bundle_field = FieldBlock(
            "Software version", self.bundle, "The recommended version is chosen for you."
        )
        software.body.addWidget(self.bundle_field)
        self.software_card = software
        software.setVisible(show_bundles)
        layout.addWidget(software)

        # ----------------------------------------------------------- advanced
        advanced = Card(padding=22)
        advanced.body.addWidget(text("Change these only if head office tells you to.", "muted"))
        self.wifi_ssid = QLineEdit()
        self.wifi_ssid.setMaxLength(32)
        self.wifi_password = PasswordInput(revealed=True)
        self.wifi_password.line_edit.setMaxLength(64)
        self.device_id = QLineEdit()
        self.device_id.setMaxLength(31)
        self.zkt_port = QLineEdit()
        self.zkt_comm_key = QLineEdit()
        self.zkt_preferred_ip = QLineEdit()
        self.hik_username = QLineEdit()
        self.hik_username.setMaxLength(63)
        self.hik_port = QLineEdit()
        self.hik_transport = QComboBox()
        for key, words in TRANSPORTS:
            self.hik_transport.addItem(words, key)
        self.wifi_ssid_field = FieldBlock(
            "Wi-Fi name",
            self.wifi_ssid,
            "The board tries this Wi-Fi first. You choose the site's Wi-Fi later with a phone.",
        )
        self.wifi_password_field = FieldBlock("Wi-Fi password", self.wifi_password)
        self.device_id_field = FieldBlock(
            "Device number", self.device_id, "The next free number in this zone is filled in."
        )
        self.zkt_port_field = FieldBlock("Machine port", self.zkt_port)
        self.zkt_comm_key_field = FieldBlock(
            "Machine comm key",
            self.zkt_comm_key,
            _FIX_COMM_KEY_HINT if fix_mode else _COMM_KEY_HINT,
        )
        self.zkt_preferred_ip_field = FieldBlock(
            "Machine address",
            self.zkt_preferred_ip,
            "0.0.0.0 lets the board find the machine by itself.",
        )
        self.hik_username_field = FieldBlock("Machine user name", self.hik_username)
        self.hik_port_field = FieldBlock("Machine port", self.hik_port)
        self.hik_transport_field = FieldBlock("Connection", self.hik_transport)
        advanced_grid = QGridLayout()
        advanced_grid.setHorizontalSpacing(24)
        advanced_grid.setVerticalSpacing(16)
        advanced_grid.setColumnStretch(0, 1)
        advanced_grid.setColumnStretch(1, 1)
        self._advanced_grid = advanced_grid
        advanced.body.addLayout(advanced_grid)
        self.advanced = Collapsible("Advanced settings", advanced)
        layout.addWidget(self.advanced)

        self._wire()
        self._layout_fields()

    # ---------------------------------------------------------------- wiring

    def _wire(self) -> None:
        self.zone_id.textChanged.connect(self._zone_changed)
        self.change_name.clicked.connect(self._toggle_name)
        self.zone_name.textEdited.connect(self._name_edited)
        self.zone_name.textChanged.connect(lambda _value: self._update_preview())
        self.device_id.textEdited.connect(self._device_edited)
        self.hik_model.currentIndexChanged.connect(lambda _index: self._model_changed())
        self.hik_transport.currentIndexChanged.connect(lambda _index: self._transport_changed())
        self.hik_port.textEdited.connect(lambda _value: setattr(self, "_port_custom", True))
        for field in self._all_fields():
            editor = field.focus_target
            if isinstance(editor, QLineEdit):
                editor.textEdited.connect(lambda _value, block=field: self._edited(block))
            elif isinstance(editor, QComboBox):
                editor.currentIndexChanged.connect(lambda _index, block=field: self._edited(block))

    def _edited(self, block: FieldBlock) -> None:
        if block is not self.zone_id_field:
            block.set_error(None)
        self.changed.emit()

    def _all_fields(self) -> List[FieldBlock]:
        return [
            self.zone_id_field,
            self.zone_name_field,
            self.hik_host_field,
            self.hik_password_field,
            self.hik_model_field,
            self.hik_serial_field,
            self.bundle_field,
            self.wifi_ssid_field,
            self.wifi_password_field,
            self.device_id_field,
            self.zkt_port_field,
            self.zkt_comm_key_field,
            self.zkt_preferred_ip_field,
            self.hik_username_field,
            self.hik_port_field,
            self.hik_transport_field,
        ]

    def _layout_fields(self) -> None:
        """This machine type's fields: the machine card on top, the rest under Advanced."""
        top = Qt.AlignmentFlag.AlignTop
        advanced, machine = self._advanced_grid, self._machine_grid
        for field in self._advanced_fields(include_all=True):
            advanced.removeWidget(field)
            machine.removeWidget(field)
            field.hide()
        fields = self._advanced_fields()
        for index, field in enumerate(fields):
            advanced.addWidget(field, index // 2, index % 2, top)
            field.show()
        hikvision = self._family is Family.HIKVISION
        for field in self._hik_machine_fields():
            field.setVisible(hikvision)
        key_on_top = self._fix_mode and not hikvision
        if key_on_top:
            # The comm key must match the machine: in Fix settings it is asked for in view.
            machine.addWidget(self.zkt_comm_key_field, 2, 0, top)
            self.zkt_comm_key_field.show()
        self.machine_card.setVisible(hikvision or key_on_top)

    def _hik_machine_fields(self) -> List[FieldBlock]:
        return [
            self.hik_host_field,
            self.hik_password_field,
            self.hik_model_field,
            self.hik_serial_field,
        ]

    def _advanced_fields(self, include_all: bool = False) -> List[FieldBlock]:
        common = [self.wifi_ssid_field, self.wifi_password_field, self.device_id_field]
        zkt = [self.zkt_port_field, self.zkt_comm_key_field, self.zkt_preferred_ip_field]
        hik = [self.hik_username_field, self.hik_transport_field, self.hik_port_field]
        if include_all:
            return common + zkt + hik
        if self._family is Family.HIKVISION:
            return common + hik
        if self._fix_mode:
            return common + [self.zkt_port_field, self.zkt_preferred_ip_field]
        return common + zkt

    # ------------------------------------------------------------------ setup

    @property
    def family(self) -> Family:
        return self._family

    @property
    def fix_mode(self) -> bool:
        return self._fix_mode

    def set_family(self, family: Family) -> None:
        """Switch between ZKTeco and Hikvision fields without losing what was typed."""
        self._family = family
        self._layout_fields()
        self._model_changed()

    def reset(self, family: Family, defaults: FieldDefaults) -> None:
        """Empty place details and the owner's defaults for everything else.

        In fix mode the comm key and the Hikvision model start empty instead: only the
        attendance machine can say what they are.
        """
        self._defaults = defaults
        self._family = family
        self._name_custom = False
        self._device_custom = False
        self._port_custom = False
        self._wanted_bundle = None
        self.bundle.clear()
        self.zone_id.clear()
        self.zone_name.clear()
        self._set_name_editable(False)
        self.wifi_ssid.setText(defaults.wifi_ssid)
        self.wifi_password.setText(defaults.wifi_password)
        self.wifi_password.set_revealed(True)
        self.device_id.setText(defaults.device_id)
        self.zkt_port.setText(str(defaults.zkt_port))
        if self._fix_mode:
            self.zkt_comm_key.clear()
        else:
            self.zkt_comm_key.setText(str(defaults.zkt_comm_key))
        self.zkt_preferred_ip.setText(defaults.zkt_preferred_ip)
        self.hik_host.clear()
        self.hik_password.clear()
        self.hik_password.set_revealed(False)
        self.hik_username.setText(defaults.hik_username)
        self._select(self.hik_transport, defaults.hik_transport)
        self.hik_port.setText(str(defaults.hik_port))
        self.hik_serial.clear()
        self.hik_model.setCurrentIndex(0)  # "Choose the model" in fix mode
        for field in self._all_fields():
            field.set_error(None)
        # In fix mode every value that will be written is in view.
        self.advanced.set_expanded(self._fix_mode)
        self._layout_fields()
        self._model_changed()
        self._update_preview()

    def load_record(
        self, record: api.DeviceRecord, family: Family, defaults: FieldDefaults
    ) -> None:
        """Start from what ADD knows about the board (Fix settings)."""
        self.reset(family, defaults)
        self.zone_id.setText(record.zone_id or "")
        if record.zone_name and record.zone_name != (record.zone_id or "").upper():
            self._name_custom = True
            self._set_name_editable(True)
        self.zone_name.setText(record.zone_name or (record.zone_id or "").upper())
        if record.device_id:
            self._device_custom = True
            self.device_id.setText(record.device_id)
        self._update_preview()

    def set_zones(self, zones: Sequence[api.ZoneSummary]) -> None:
        self._zones = list(zones)
        self._zone_list.setStringList(sorted(zone.zone_id for zone in self._zones))
        self._zone_changed(self.zone_id.text(), live=False)

    def set_bundles(self, bundles: Sequence[BundleInfo]) -> None:
        current = self.bundle.currentData() or self._wanted_bundle
        self.bundle.blockSignals(True)
        self.bundle.clear()
        for bundle in sorted_bundles(bundles):
            self.bundle.addItem(bundle_words(bundle), bundle.bundle_id)
        if current is not None:
            index = self.bundle.findData(current)
            if index >= 0:
                self.bundle.setCurrentIndex(index)
        self.bundle.blockSignals(False)
        self.bundle.setEnabled(self.bundle.count() > 0)
        self.bundle_field.set_hint(
            "The recommended version is chosen for you."
            if self.bundle.count()
            else "No software is ready yet."
        )

    def set_bundles_loading(self) -> None:
        # Keep the person's choice while the list is fetched again.
        self._wanted_bundle = self.selected_bundle_id() or self._wanted_bundle
        self.bundle.clear()
        self.bundle.addItem("Getting the list from head office...", None)
        self.bundle.setEnabled(False)

    def selected_bundle_id(self) -> Optional[str]:
        value = self.bundle.currentData()
        return str(value) if value else None

    @staticmethod
    def _select(combo: QComboBox, key: str) -> None:
        index = combo.findData(key)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # ---------------------------------------------------------- live updates

    def _zone_changed(self, value: str, live: bool = True) -> None:
        zone_id = value.strip().upper()
        if not self._name_custom:
            known = naming.zone_for(zone_id, self._zones)
            self.zone_name.setText(known.zone_name if known and known.zone_name else zone_id)
        if not self._device_custom:
            self.device_id.setText(
                naming.next_free_device_id(zone_id, self._zones, self._defaults.device_id)
            )
        if live:
            self.zone_id_field.set_error(naming.zone_id_problem(zone_id) if zone_id else None)
        self._update_preview()
        self.changed.emit()

    def _toggle_name(self) -> None:
        if self._name_custom:
            self._name_custom = False
            self._set_name_editable(False)
            self.zone_name_field.set_error(None)
            self._zone_changed(self.zone_id.text(), live=False)
        else:
            self._name_custom = True
            self._set_name_editable(True)
            self.zone_name.setFocus()
            self.zone_name.selectAll()

    def _set_name_editable(self, editable: bool) -> None:
        self.zone_name.setReadOnly(not editable)
        theme.repolish(self.zone_name)
        self.change_name.setText("Use the ZONE ID" if editable else "Change")
        self.change_name.setAccessibleName(
            "Fill the ZONE NAME from the ZONE ID" if editable else "Change the ZONE NAME"
        )
        self.zone_name_field.set_hint(
            "Type the name head office gave you." if editable else "Filled in from the ZONE ID."
        )

    def _name_edited(self, _value: str) -> None:
        self.zone_name_field.set_error(None)
        self._update_preview()

    def _device_edited(self, _value: str) -> None:
        self._device_custom = True

    def _model_changed(self) -> None:
        key = self.hik_model.currentData()
        model = model_for(key or "")
        if self._family is Family.HIKVISION and key is not None and not model.tested:
            advice = wording.untested_model(
                f"the {model.label}" if model.key != "other" else "this model"
            )
            self.model_notice.set_notice(
                advice.title, " ".join((advice.what_happened,) + advice.what_to_do), advice.severity
            )
            self.model_notice.show()
        else:
            self.model_notice.hide()

    def _transport_changed(self) -> None:
        if self._port_custom:
            return
        port = _TRANSPORT_PORTS.get(self.hik_transport.currentData() or "", self._defaults.hik_port)
        self.hik_port.setText(str(port))

    def _update_preview(self) -> None:
        name = self.zone_name.text().strip()
        if not name:
            self.preview_name.setText("Type the ZONE ID first")
            self.preview_group.hide()
            return
        others = [zone.zone_name or zone.zone_id for zone in self._zones]
        shown = naming.public_name(name, others)
        group = naming.group_of(name)
        self.preview_name.setText(shown)
        self.preview_group.set_badge(
            f"{group} group", "groupLife" if group == naming.LIFE else "groupHealth"
        )
        self.preview_group.show()

    # ------------------------------------------------------------- checking

    def problems(self) -> Dict[FieldBlock, str]:
        found: Dict[FieldBlock, str] = {}

        def check(field: FieldBlock, problem: Optional[str]) -> None:
            if problem:
                found[field] = problem

        check(self.zone_id_field, naming.zone_id_problem(self.zone_id.text().strip()))
        check(self.zone_name_field, naming.zone_name_problem(self.zone_name.text()))
        if self._show_bundles and not self.selected_bundle_id():
            check(self.bundle_field, "There is no board software to choose yet.")
        check(self.wifi_ssid_field, naming.wifi_name_problem(self.wifi_ssid.text()))
        check(self.wifi_password_field, naming.wifi_password_problem(self.wifi_password.text()))
        check(self.device_id_field, naming.device_id_problem(self.device_id.text().strip()))
        if self._family is Family.HIKVISION:
            check(self.hik_host_field, naming.machine_ip_problem(self.hik_host.text()))
            check(
                self.hik_password_field,
                naming.text_problem(self.hik_password.text(), "machine password", 127),
            )
            check(
                self.hik_username_field,
                naming.text_problem(self.hik_username.text().strip(), "machine user name", 63),
            )
            check(self.hik_port_field, naming.port_problem(self.hik_port.text()))
            check(
                self.hik_serial_field,
                naming.text_problem(self.hik_serial.text().strip(), "machine serial number", 119),
            )
            if self.hik_model.currentData() is None:
                check(
                    self.hik_model_field,
                    "Choose the machine model. It is printed on a label on the back of the "
                    "machine.",
                )
        else:
            check(self.zkt_port_field, naming.port_problem(self.zkt_port.text()))
            key = self.zkt_comm_key.text()
            check(
                self.zkt_comm_key_field,
                "Type the comm key from the attendance machine."
                if self._fix_mode and not key.strip()
                else naming.comm_key_problem(key),
            )
            check(
                self.zkt_preferred_ip_field,
                naming.preferred_ip_problem(self.zkt_preferred_ip.text()),
            )
        return found

    def validate(self) -> bool:
        """Show every problem next to its field; open Advanced if one is there."""
        found = self.problems()
        for field in self._all_fields():
            field.set_error(found.get(field))
        if not found:
            return True
        advanced = set(self._advanced_fields())
        if any(field in advanced for field in found):
            self.advanced.set_expanded(True)
        first = next(field for field in self._all_fields() if field in found)
        first.focus_target.setFocus(Qt.FocusReason.OtherFocusReason)
        return False

    def first_problem_field(self) -> Optional[FieldBlock]:
        for field in self._all_fields():
            if field.error.text():
                return field
        return None

    def values(self) -> FormValues:
        """The form as typed. Call ``validate`` first."""
        number = naming.number_in_range
        transport = self.hik_transport.currentData() or "http_digest"
        return FormValues(
            family=self._family,
            zone_id=self.zone_id.text().strip().upper(),
            zone_name=self.zone_name.text(),
            device_id=self.device_id.text().strip(),
            wifi_ssid=self.wifi_ssid.text(),
            wifi_password=self.wifi_password.text(),
            bundle_id=self.selected_bundle_id(),
            zkt_port=number(self.zkt_port.text(), 1, 65535) or 4370,
            zkt_comm_key=number(self.zkt_comm_key.text(), 0, 0xFFFFFFFF) or 0,
            zkt_preferred_ip=self.zkt_preferred_ip.text().strip() or "0.0.0.0",
            hik_host=self.hik_host.text().strip(),
            hik_password=self.hik_password.text(),
            hik_username=self.hik_username.text().strip(),
            hik_port=number(self.hik_port.text(), 1, 65535) or _TRANSPORT_PORTS.get(transport, 80),
            hik_transport=transport,
            hik_serial=self.hik_serial.text().strip(),
            hik_model_key=self.hik_model.currentData() or api.HIK_MODELS[0].key,
        )

    def fill(self, values: FormValues) -> None:
        """Put earlier answers back (going back to the form keeps what was typed).

        In fix mode the comm key is not carried over: the Details screen offers the usual
        one under Advanced, but only the attendance machine knows the right one.
        """
        self._family = values.family
        self._layout_fields()
        self._name_custom = True
        self._device_custom = True
        self.zone_id.setText(values.zone_id)
        self.zone_name.setText(values.zone_name)
        self._name_custom = values.zone_name != values.zone_id
        self._set_name_editable(self._name_custom)
        self.device_id.setText(values.device_id)
        self.wifi_ssid.setText(values.wifi_ssid)
        self.wifi_password.setText(values.wifi_password)
        self.zkt_port.setText(str(values.zkt_port))
        if self._fix_mode:
            self.zkt_comm_key.clear()
        else:
            self.zkt_comm_key.setText(str(values.zkt_comm_key))
        self.zkt_preferred_ip.setText(values.zkt_preferred_ip)
        self.hik_host.setText(values.hik_host)
        self.hik_password.setText(values.hik_password)
        self.hik_username.setText(values.hik_username)
        self._select(self.hik_transport, values.hik_transport)
        self.hik_port.setText(str(values.hik_port))
        self.hik_serial.setText(values.hik_serial)
        self._select(self.hik_model, values.hik_model_key)
        if values.bundle_id:
            index = self.bundle.findData(values.bundle_id)
            if index >= 0:
                self.bundle.setCurrentIndex(index)
        self._model_changed()
        self._update_preview()
