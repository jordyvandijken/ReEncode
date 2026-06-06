from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from reencode.main_window import MainWindow
from reencode.scan_contracts import ScanState
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

    def test_file_found_populates_rows_before_metadata_phase(self):
        path = str(Path(self._tmp.name) / "video.mp4")

        self.window._on_file_found(
            scan_token=42,
            media_type="Videos",
            path=path,
        )
        self.window._flush_metadata_rows(limit=0)

        self.assertEqual(self.window._panels["Videos"].file_count(), 1)
        self.assertEqual(self.window._scan_state, ScanState.IDLE)

    def test_discovery_finished_starts_metadata_worker_after_discovery(self):
        class _DummySignal:
            def connect(self, _slot):
                return None

        class _DummyWorker:
            def __init__(self, *args, **kwargs):
                self.row_ready = _DummySignal()
                self.failed_item = _DummySignal()
                self.progress = _DummySignal()
                self.completed = _DummySignal()
                self.fatal_error = _DummySignal()
                self.submitted: list[tuple[str, str]] = []
                self.expected_total = -1
                self.started = False
                self.cancelled = False

            def submit(self, media_type: str, path: str):
                self.submitted.append((media_type, path))

            def finish(self, expected_total: int):
                self.expected_total = expected_total

            def start(self):
                self.started = True

            def isRunning(self):
                return False

            def cancel(self):
                self.cancelled = True

            def wait(self):
                return True

        path_a = str(Path(self._tmp.name) / "a.mp4")
        path_b = str(Path(self._tmp.name) / "b.mp4")
        self.window._scan_state = ScanState.QUICKSCAN
        self.window._discovered_files = [("Videos", path_a), ("Audio", path_b)]

        with mock.patch("reencode.main_window._MetadataProbeWorker", _DummyWorker):
            self.window._on_discovery_finished(
                scan_token=42,
                _phase="discovery",
                count=2,
                cancelled=False,
            )

        worker = self.window._metadata_probe_worker
        self.assertIsNotNone(worker)
        self.assertEqual(self.window._scan_state, ScanState.METADATA)
        self.assertEqual(worker.submitted, [("Videos", path_a), ("Audio", path_b)])
        self.assertEqual(worker.expected_total, 2)
        self.assertTrue(worker.started)


if __name__ == "__main__":
    unittest.main()
