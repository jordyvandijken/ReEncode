from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QTabWidget, QStatusBar,
)

from reencode.constants import MEDIA_TYPES
from reencode.media_panel import MediaPanel
from reencode.scanner import ScannerThread
from reencode.sources_panel import SourcesPanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ReEncode — Media Scanner")
        self.resize(1100, 650)

        self._scanner: ScannerThread | None = None
        self._total_found = 0

        self._setup_ui()

    def _setup_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)

        # --- Left panel ---
        self._sources_panel = SourcesPanel()
        self._sources_panel.setMinimumWidth(220)
        self._sources_panel.setMaximumWidth(360)
        self._sources_panel.scan_requested.connect(self._start_scan)
        splitter.addWidget(self._sources_panel)

        # --- Right tabs (one MediaPanel per media type) ---
        self._tab_widget = QTabWidget()
        self._panels: dict[str, MediaPanel] = {}
        for media_type in MEDIA_TYPES:
            panel = MediaPanel(media_type)
            self._panels[media_type] = panel
            self._tab_widget.addTab(panel, media_type)
        splitter.addWidget(self._tab_widget)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # --- Status bar ---
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready. Add folders and click Scan.")

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _start_scan(self, folders: list[str]):
        # Stop any running scan first
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait()

        # Clear all panels
        for panel in self._panels.values():
            panel.clear()

        self._total_found = 0
        self._sources_panel.set_scanning(True)
        self._status_bar.showMessage("Scanning…")

        self._scanner = ScannerThread(folders, MEDIA_TYPES, parent=self)
        self._scanner.file_found.connect(self._on_file_found)
        self._scanner.scan_finished.connect(self._on_scan_finished)
        self._scanner.start()

    def _on_file_found(self, media_type: str, path: str):
        panel = self._panels.get(media_type)
        if panel:
            panel.add_file(path)
            self._total_found += 1
            # Update status bar periodically (every 25 files) to avoid UI churn
            if self._total_found % 25 == 0:
                self._status_bar.showMessage(f"Scanning… {self._total_found} files found so far")

    def _on_scan_finished(self, count: int):
        self._sources_panel.set_scanning(False)
        noun = "file" if count == 1 else "files"
        self._status_bar.showMessage(f"Scan complete — {count} {noun} found.")
        self._scanner = None

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait()
        super().closeEvent(event)
