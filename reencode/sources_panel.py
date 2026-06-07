from PySide6.QtCore import Signal, QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
    QPushButton, QLabel, QFileDialog, QListWidgetItem,
)

from reencode import presets as presets_data


class _PresetDialog(QDialog):
    def __init__(self, presets: list[presets_data.Preset], selected_preset_id: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Preset")
        self.resize(560, 360)
        self._presets = presets

        layout = QVBoxLayout(self)

        self._preset_combo = QComboBox()
        for preset in presets:
            self._preset_combo.addItem(preset.name, preset.id)
        self._preset_combo.currentIndexChanged.connect(self._render_current)
        layout.addWidget(self._preset_combo)

        self._description = QLabel()
        self._description.setWordWrap(True)
        layout.addWidget(self._description)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(divider)

        self._media_form = QFormLayout()
        self._image_label = QLabel()
        self._video_label = QLabel()
        self._audio_label = QLabel()
        for label in (self._image_label, self._video_label, self._audio_label):
            label.setWordWrap(True)
        self._media_form.addRow("Image", self._image_label)
        self._media_form.addRow("Video", self._video_label)
        self._media_form.addRow("Audio", self._audio_label)
        layout.addLayout(self._media_form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if selected_preset_id:
            index = self._preset_combo.findData(selected_preset_id)
            if index >= 0:
                self._preset_combo.setCurrentIndex(index)

        self._render_current()

    def selected_preset_id(self) -> str | None:
        value = self._preset_combo.currentData()
        return value if isinstance(value, str) and value else None

    def _render_current(self):
        preset_id = self.selected_preset_id()
        selected = next((item for item in self._presets if item.id == preset_id), None)
        if selected is None:
            self._description.setText("No preset selected.")
            self._image_label.setText("-")
            self._video_label.setText("-")
            self._audio_label.setText("-")
            return

        self._description.setText(selected.description)
        self._image_label.setText(self._entry_text(selected.media.get("image")))
        self._video_label.setText(self._entry_text(selected.media.get("video")))
        self._audio_label.setText(self._entry_text(selected.media.get("audio")))

    @staticmethod
    def _entry_text(entry: presets_data.PresetMediaEntry | None) -> str:
        if entry is None:
            return "-"

        parts = [f"Codec: {entry.codec}"]
        if entry.mode:
            parts.append(f"Mode: {entry.mode}")
        if entry.info:
            parts.append(f"Info: {entry.info}")
        return "\n".join(parts)


class SourcesPanel(QWidget):
    """Panel that manages the list of folders to scan."""

    scan_requested = Signal(list)   # list[str] of folder paths
    cancel_requested = Signal()
    preset_selected = Signal(str)
    _SETTINGS_KEY = "sources/folders"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = QSettings()
        self._presets = presets_data.load_presets()
        self._selected_preset_id: str | None = None
        self._setup_ui()
        self._load_folders()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        layout.addWidget(QLabel("<b>Scan Folders</b>"))

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list, stretch=1)

        btn_layout = QHBoxLayout()
        self._btn_add = QPushButton("Add Folder…")
        self._btn_remove = QPushButton("Remove Selected")
        self._btn_remove.setEnabled(False)
        self._btn_add.clicked.connect(self._on_add)
        self._btn_remove.clicked.connect(self._on_remove)
        btn_layout.addWidget(self._btn_add)
        btn_layout.addWidget(self._btn_remove)
        layout.addLayout(btn_layout)

        self._btn_scan = QPushButton("Scan")
        self._btn_scan.setEnabled(False)
        self._btn_scan.setStyleSheet("font-weight: bold; padding: 6px;")
        self._btn_scan.clicked.connect(self._on_scan)
        layout.addWidget(self._btn_scan)

        self._btn_cancel = QPushButton("Cancel Scan")
        self._btn_cancel.setStyleSheet("font-weight: bold; padding: 6px;")
        self._btn_cancel.setVisible(False)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel)
        layout.addWidget(self._btn_cancel)

        self._btn_preset = QPushButton("Preset")
        self._btn_preset.clicked.connect(self._on_preset)
        layout.addWidget(self._btn_preset)

    def _on_add(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Scan")
        if not folder:
            return
        # Avoid duplicates
        for i in range(self._list.count()):
            if self._list.item(i).text() == folder:
                return
        self._list.addItem(QListWidgetItem(folder))
        self._btn_scan.setEnabled(True)
        self._save_folders()

    def _on_remove(self):
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))
        if self._list.count() == 0:
            self._btn_scan.setEnabled(False)
        self._save_folders()

    def _on_selection_changed(self):
        self._btn_remove.setEnabled(bool(self._list.selectedItems()))

    def _on_scan(self):
        folders = [self._list.item(i).text() for i in range(self._list.count())]
        if folders:
            self.scan_requested.emit(folders)

    def _on_cancel(self):
        self.cancel_requested.emit()

    def _on_preset(self):
        if not self._presets:
            return

        dialog = _PresetDialog(self._presets, self._selected_preset_id, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dialog.selected_preset_id()
        if not selected:
            return

        self._selected_preset_id = selected
        self.preset_selected.emit(selected)

    def set_selected_preset(self, preset_id: str | None):
        self._selected_preset_id = preset_id

    def _load_folders(self):
        saved = self._settings.value(self._SETTINGS_KEY, [])
        if isinstance(saved, str):
            saved = [saved]
        if not isinstance(saved, list):
            saved = []

        seen = set()
        for folder in saved:
            if not isinstance(folder, str) or not folder or folder in seen:
                continue
            self._list.addItem(QListWidgetItem(folder))
            seen.add(folder)

        self._btn_scan.setEnabled(self._list.count() > 0)

    def _save_folders(self):
        folders = [self._list.item(i).text() for i in range(self._list.count())]
        self._settings.setValue(self._SETTINGS_KEY, folders)

    def set_scanning(self, scanning: bool):
        """Lock/unlock controls while a scan is running."""
        self._btn_add.setEnabled(not scanning)
        self._btn_remove.setEnabled(not scanning and bool(self._list.selectedItems()))
        self._btn_scan.setEnabled(not scanning and self._list.count() > 0)
        self._btn_scan.setText("Scanning…" if scanning else "Scan")
        self._btn_cancel.setVisible(scanning)
        self._btn_cancel.setEnabled(scanning)
        self._btn_preset.setEnabled(not scanning and bool(self._presets))
