from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from reencode.main_window import MainWindow
from reencode.scan_store import ScanStore


class MainWindowScanContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.window = MainWindow()
        old_store = self.window._scan_store
        old_store.close()
        self.window._scan_store = ScanStore(Path(self._tmp.name) / "scan.db")
        self.window._scan_token = 42

    def tearDown(self):
        self.window.close()
        self._tmp.cleanup()

    def test_stale_row_ready_is_ignored(self):
        self.window._on_row_ready(
            scan_token=999,
            media_type="Videos",
            path=str(Path(self._tmp.name) / "video.mp4"),
            size_bytes=123,
            modified_timestamp="1",
            probe_info={"video_codec": "h264"},
            encoding="h264",
            _estimate=100,
            _recommend="H.265/HEVC",
        )

        self.assertEqual(self.window._pending_metadata_rows, {})
        self.assertEqual(self.window._pending_probe_updates, {})

    def test_failure_routing_buffers_and_flushes(self):
        bad_path = str(Path(self._tmp.name) / "bad.mp4")

        self.window._on_failed_item(
            scan_token=42,
            media_type="Videos",
            path=bad_path,
            reason="Probe failed",
            phase="probe",
        )
        self.window._flush_failed_rows(limit=0)

        self.assertEqual(self.window._failed_panel.file_count(), 1)

    def test_stale_failure_event_is_ignored(self):
        bad_path = str(Path(self._tmp.name) / "bad.mp4")

        self.window._on_failed_item(
            scan_token=999,
            media_type="Videos",
            path=bad_path,
            reason="Probe failed",
            phase="probe",
        )
        self.window._flush_failed_rows(limit=0)

        self.assertEqual(self.window._failed_panel.file_count(), 0)

    def test_stale_discovery_completed_is_ignored(self):
        self.window._discovery_finished = False
        self.window._discovery_count = 0

        self.window._on_discovery_finished(
            scan_token=999,
            _phase="discovery",
            count=10,
            cancelled=False,
        )

        self.assertFalse(self.window._discovery_finished)
        self.assertEqual(self.window._discovery_count, 0)


if __name__ == "__main__":
    unittest.main()
