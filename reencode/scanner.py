import os
from PySide6.QtCore import QThread, Signal

from reencode.scan_contracts import ScanPhase


class ScannerThread(QThread):
    """Background thread that walks folders and emits a signal for each media file found."""

    file_found = Signal(int, str, str)   # (scan_id, media_type, absolute_path)
    progress = Signal(int, str, int, int)   # (scan_id, phase, completed, total)
    discovery_finished = Signal(int, str, int, bool)   # (scan_id, phase, count, cancelled)
    fatal_error = Signal(int, str, str)   # (scan_id, phase, message)

    def __init__(self, scan_id: int, folders: list[str], media_types: dict[str, set[str]], parent=None):
        super().__init__(parent)
        self._scan_id = scan_id
        self._folders = folders
        self._media_types = media_types
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        count = 0
        try:
            for folder in self._folders:
                if self._cancelled:
                    break
                for root, _dirs, files in os.walk(folder):
                    if self._cancelled:
                        break
                    for filename in files:
                        if self._cancelled:
                            break
                        ext = os.path.splitext(filename)[1].lower()
                        for media_type, extensions in self._media_types.items():
                            if ext in extensions:
                                full_path = os.path.join(root, filename)
                                self.file_found.emit(self._scan_id, media_type, full_path)
                                count += 1
                                if count % 25 == 0:
                                    self.progress.emit(self._scan_id, ScanPhase.DISCOVERY.value, count, 0)
                                break  # a file matches only one type
        except Exception as exc:  # pragma: no cover
            self.fatal_error.emit(self._scan_id, ScanPhase.DISCOVERY.value, str(exc))
            self._cancelled = True

        self.discovery_finished.emit(self._scan_id, ScanPhase.DISCOVERY.value, count, self._cancelled)
