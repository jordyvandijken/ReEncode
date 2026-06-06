import os
from queue import Queue

from PySide6.QtCore import QThread, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QTabWidget, QStatusBar,
)

from reencode import codec_probe
from reencode.constants import MEDIA_TYPES
from reencode.media_panel import MediaPanel
from reencode.probe_cache import ProbeCache
from reencode.scanner import ScannerThread
from reencode.sources_panel import SourcesPanel


class _ProbeThread(QThread):
    file_ready = Signal(int, str, object)
    progress = Signal(int, int, int)
    completed = Signal(int, int)

    def __init__(self, scan_token: int, jobs: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._scan_token = scan_token
        self._jobs = jobs
        self._cancelled = False
        self._cache: ProbeCache | None = None

    def cancel(self):
        self._cancelled = True

    def run(self):
        # Load cache inside worker thread to avoid blocking the UI thread.
        self._cache = ProbeCache()
        total = len(self._jobs)
        completed = 0

        for _media_type, path in self._jobs:
            if self._cancelled:
                break

            probe_info = self._cache.get_valid_probe(path)
            if probe_info is None:
                probe_info = codec_probe.probe_media_info(path)
                if probe_info is not None:
                    self._cache.upsert_probe(path, probe_info)

            self.file_ready.emit(self._scan_token, path, probe_info)
            completed += 1
            self.progress.emit(self._scan_token, completed, total)

        self._cache.save()
        self.completed.emit(self._scan_token, completed)


class _MetadataThread(QThread):
    file_ready = Signal(int, str, str, object, str)
    completed = Signal(int)

    def __init__(self, scan_token: int, parent=None):
        super().__init__(parent)
        self._scan_token = scan_token
        self._jobs: Queue[tuple[str, str] | None] = Queue()
        self._cancelled = False

    def submit(self, media_type: str, path: str):
        self._jobs.put((media_type, path))

    def finish(self):
        self._jobs.put(None)

    def cancel(self):
        self._cancelled = True
        self._jobs.put(None)

    def run(self):
        while True:
            job = self._jobs.get()
            if job is None or self._cancelled:
                break

            media_type, path = job
            try:
                stat = os.stat(path)
                size_bytes = stat.st_size
                modified = str(int(stat.st_mtime))
            except OSError:
                size_bytes = 0
                modified = ""

            self.file_ready.emit(self._scan_token, media_type, path, size_bytes, modified)

        self.completed.emit(self._scan_token)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ReEncode — Media Scanner")
        self.resize(1100, 650)

        self._scanner: ScannerThread | None = None
        self._metadata_thread: _MetadataThread | None = None
        self._probe_thread: _ProbeThread | None = None
        self._scan_token = 0
        self._total_found = 0
        self._discovery_count: int | None = None
        self._probe_jobs: list[tuple[str, str]] = []
        self._probe_media_types: dict[str, str] = {}
        self._pending_metadata_rows: dict[str, list[tuple[str, int, str]]] = {}
        self._metadata_processed = 0

        self._metadata_flush_timer = QTimer(self)
        self._metadata_flush_timer.setInterval(30)
        self._metadata_flush_timer.timeout.connect(self._flush_metadata_rows)

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
            panel.conversion_status_changed.connect(self._on_conversion_status_changed)
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
        self._scan_token += 1

        if self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.stop()

        # Stop any running scan first
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait()

        if self._metadata_thread and self._metadata_thread.isRunning():
            self._metadata_thread.cancel()
            self._metadata_thread.wait()

        if self._probe_thread and self._probe_thread.isRunning():
            self._probe_thread.cancel()

        # Clear all panels
        for panel in self._panels.values():
            panel.clear()

        self._total_found = 0
        self._discovery_count = None
        self._probe_jobs = []
        self._probe_media_types = {}
        self._pending_metadata_rows = {}
        self._metadata_processed = 0
        self._sources_panel.set_scanning(True)
        self._status_bar.showMessage("Scanning…")

        self._metadata_thread = _MetadataThread(self._scan_token, parent=self)
        self._metadata_thread.file_ready.connect(self._on_metadata_ready)
        self._metadata_thread.completed.connect(self._on_metadata_completed)
        self._metadata_thread.start()

        self._scanner = ScannerThread(folders, MEDIA_TYPES, parent=self)
        self._scanner.file_found.connect(self._on_file_found)
        self._scanner.discovery_finished.connect(self._on_discovery_finished)
        self._scanner.start()

    def _on_file_found(self, media_type: str, path: str):
        if self._metadata_thread is not None:
            self._metadata_thread.submit(media_type, path)

        self._total_found += 1
        if media_type in {"Videos", "Audio"}:
            self._probe_jobs.append((media_type, path))
            self._probe_media_types[path] = media_type
        if self._total_found % 25 == 0:
            self._status_bar.showMessage(f"Scanning… {self._total_found} files found so far")

    def _on_metadata_ready(self, scan_token: int, media_type: str, path: str, size_bytes: int, modified_timestamp: str):
        if scan_token != self._scan_token:
            return

        self._pending_metadata_rows.setdefault(media_type, []).append((path, size_bytes, modified_timestamp))
        self._metadata_processed += 1

        if not self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.start()

    def _on_discovery_finished(self, count: int):
        if self._scanner is not None:
            self._scanner.deleteLater()
            self._scanner = None

        self._discovery_count = count

        if self._metadata_thread is not None:
            self._update_metadata_status()
            self._metadata_thread.finish()
            return

        self._begin_probe_phase()

    def _on_metadata_completed(self, scan_token: int):
        sender = self.sender()
        if isinstance(sender, _MetadataThread):
            sender.deleteLater()

        if scan_token != self._scan_token:
            return

        if self._metadata_flush_timer.isActive():
            self._metadata_flush_timer.stop()
        self._flush_metadata_rows(limit=0)

        self._metadata_thread = None
        self._begin_probe_phase()

    def _flush_metadata_rows(self, limit: int = 250):
        total_added = 0

        for media_type, rows in self._pending_metadata_rows.items():
            if not rows:
                continue

            panel = self._panels.get(media_type)
            if panel is None:
                rows.clear()
                continue

            if limit == 0:
                take = len(rows)
            else:
                remaining = limit - total_added
                if remaining <= 0:
                    break
                take = min(remaining, len(rows))

            batch = rows[:take]
            del rows[:take]
            panel.add_files(batch)
            total_added += take

        if total_added:
            self._update_metadata_status()

        if self._metadata_thread is not None:
            has_pending = any(rows for rows in self._pending_metadata_rows.values())
            if has_pending and not self._metadata_flush_timer.isActive():
                self._metadata_flush_timer.start()
            elif not has_pending and self._metadata_flush_timer.isActive():
                self._metadata_flush_timer.stop()

    def _update_metadata_status(self):
        if self._discovery_count is None:
            self._status_bar.showMessage(f"Finalizing file list… {self._metadata_processed} files prepared")
            return

        total = max(1, self._discovery_count)
        processed = min(self._metadata_processed, self._discovery_count)
        percent = int((processed / total) * 100)
        self._status_bar.showMessage(f"Finalizing file list… {processed}/{self._discovery_count} ({percent}%)")

    def _begin_probe_phase(self):
        if self._discovery_count is None:
            return

        count = self._discovery_count
        self._discovery_count = None

        noun = "file" if count == 1 else "files"
        if not self._probe_jobs:
            self._sources_panel.set_scanning(False)
            self._status_bar.showMessage(f"Scan complete — {count} {noun} found.")
            return

        self._status_bar.showMessage(f"Discovery complete — probing encodings for {len(self._probe_jobs)} files…")
        self._probe_thread = _ProbeThread(self._scan_token, list(self._probe_jobs), parent=self)
        self._probe_thread.file_ready.connect(self._on_probe_file_ready)
        self._probe_thread.progress.connect(self._on_probe_progress)
        self._probe_thread.completed.connect(self._on_probe_completed)
        self._probe_thread.start()

    def _on_probe_file_ready(self, scan_token: int, path: str, probe_info: dict | None):
        if scan_token != self._scan_token:
            return

        media_type = self._probe_media_types.get(path)
        if media_type is None:
            return

        panel = self._panels.get(media_type)
        if panel:
            panel.update_probe(path, probe_info)

    def _on_probe_progress(self, scan_token: int, completed: int, total: int):
        if scan_token != self._scan_token:
            return

        if completed % 25 != 0 and completed != total:
            return

        self._status_bar.showMessage(f"Probing encodings… {completed}/{total} files analyzed")

    def _on_probe_completed(self, scan_token: int, completed: int):
        sender = self.sender()
        if isinstance(sender, _ProbeThread):
            sender.deleteLater()

        if scan_token != self._scan_token:
            return

        if self._probe_thread is not None:
            self._probe_thread = None

        self._sources_panel.set_scanning(False)
        noun = "file" if self._total_found == 1 else "files"
        self._status_bar.showMessage(f"Scan complete — {self._total_found} {noun} found, {completed} probed.")

    def _on_conversion_status_changed(self, message: str, active: bool):
        if active:
            self._status_bar.showMessage(message)
            return

        self._status_bar.showMessage(message or "Ready. Add folders and click Scan.")

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait()
        if self._metadata_thread and self._metadata_thread.isRunning():
            self._metadata_thread.cancel()
            self._metadata_thread.wait()
        if self._probe_thread and self._probe_thread.isRunning():
            self._probe_thread.cancel()
            self._probe_thread.wait()
        super().closeEvent(event)
