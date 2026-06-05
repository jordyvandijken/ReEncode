from PySide6.QtCore import Signal, QSettings
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
    QPushButton, QLabel, QFileDialog, QListWidgetItem,
)


class SourcesPanel(QWidget):
    """Panel that manages the list of folders to scan."""

    scan_requested = Signal(list)   # list[str] of folder paths
    _SETTINGS_KEY = "sources/folders"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = QSettings()
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
