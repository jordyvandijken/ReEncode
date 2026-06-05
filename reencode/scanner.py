import os
from PySide6.QtCore import QThread, Signal


class ScannerThread(QThread):
    """Background thread that walks folders and emits a signal for each media file found."""

    file_found = Signal(str, str)   # (media_type, absolute_path)
    discovery_finished = Signal(int)   # total files found during quick discovery

    def __init__(self, folders: list[str], media_types: dict[str, set[str]], parent=None):
        super().__init__(parent)
        self._folders = folders
        self._media_types = media_types
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        count = 0
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
                            self.file_found.emit(media_type, full_path)
                            count += 1
                            break  # a file matches only one type

        self.discovery_finished.emit(count)
