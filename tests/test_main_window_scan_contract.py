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

    def test_scan_state_has_converting(self):
        self.assertEqual(ScanState.CONVERTING.value, "converting")

    def test_cancel_scan_requests_worker_cancellation(self):
        class _DummyRunner:
            def __init__(self):
                self.cancelled = False

            def isRunning(self):
                return True

            def cancel(self):
                self.cancelled = True

            def wait(self):
                return True

        scanner = _DummyRunner()
        worker = _DummyRunner()
        self.window._scan_state = ScanState.QUICKSCAN
        self.window._scanner = scanner
        self.window._metadata_probe_worker = worker

        self.window._cancel_scan()

        self.assertTrue(self.window._worker_cancelled)
        self.assertTrue(scanner.cancelled)
        self.assertTrue(worker.cancelled)

    def test_file_found_persists_discovery_record(self):
        root = Path(self._tmp.name)
        path = str(root / "video.mp4")
        self.window._active_source_roots = [str(root)]

        self.window._on_file_found(
            scan_token=42,
            media_type="Videos",
            path=path,
        )

        record = self.window._scan_store.get_record(path)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["media_type"], "Videos")
        self.assertEqual(record["last_scanned"], 42)

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

    def test_metadata_flush_uses_stat_prioritization(self):
        class _FakePanel:
            def __init__(self):
                self.prioritize_called = False
                self.batch = None

            def prioritize_stat_updates(self, updates: list[tuple[str, int, str]]):
                self.prioritize_called = True
                return list(reversed(updates))

            def update_file_stats(self, batch: list[tuple[str, int, str]]):
                self.batch = batch

        fake = _FakePanel()
        self.window._panels["Videos"] = fake
        self.window._pending_metadata_updates = {
            "Videos": [
                ("first.mp4", 100, "1"),
                ("second.mp4", 200, "2"),
            ]
        }

        self.window._flush_metadata_rows(limit=1)

        self.assertTrue(fake.prioritize_called)
        self.assertEqual(fake.batch, [("second.mp4", 200, "2")])
        self.assertEqual(self.window._pending_metadata_updates["Videos"], [("first.mp4", 100, "1")])

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

    def test_scan_lifecycle_finalizes_and_prunes_stale_records(self):
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

            def submit(self, media_type: str, path: str):
                return None

            def finish(self, expected_total: int):
                return None

            def start(self):
                return None

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def wait(self):
                return True

        root = Path(self._tmp.name) / "source"
        root.mkdir(parents=True, exist_ok=True)
        stale_path = str(root / "stale.mp4")
        fresh_path = str(root / "fresh.mp4")

        self.window._active_source_roots = [str(root)]
        self.window._scan_store.upsert_record(
            absolute_path=stale_path,
            source_root=str(root),
            media_type="Videos",
            file_size=123,
            last_modified=1,
            scan_id=41,
        )

        self.window._scan_state = ScanState.QUICKSCAN
        self.window._on_file_found(scan_token=42, media_type="Videos", path=fresh_path)

        with mock.patch("reencode.main_window._MetadataProbeWorker", _DummyWorker):
            self.window._on_discovery_finished(
                scan_token=42,
                _phase="discovery",
                count=1,
                cancelled=False,
            )

        self.window._on_worker_completed(
            scan_token=42,
            _phase="metadata",
            cancelled=False,
            processed=1,
            probed=0,
        )

        self.assertEqual(self.window._scan_state, ScanState.IDLE)
        self.assertIsNone(self.window._scan_store.get_record(stale_path))
        fresh_record = self.window._scan_store.get_record(fresh_path)
        self.assertIsNotNone(fresh_record)
        assert fresh_record is not None
        self.assertEqual(fresh_record["last_scanned"], 42)
        self.assertEqual(self.window._sources_panel._btn_scan.text(), "Scan")
        message = self.window._status_bar.currentMessage()
        self.assertIn("1 file found", message)
        self.assertIn("1 stale removed", message)

    def test_discovery_cancelled_finalizes_without_worker(self):
        self.window._scan_state = ScanState.QUICKSCAN
        self.window._active_source_roots = [self._tmp.name]
        self.window._sources_panel.set_scanning(True)

        self.window._on_discovery_finished(
            scan_token=42,
            _phase="discovery",
            count=0,
            cancelled=True,
        )

        self.assertEqual(self.window._scan_state, ScanState.IDLE)
        self.assertIsNone(self.window._metadata_probe_worker)
        self.assertTrue(self.window._worker_cancelled)
        self.assertEqual(self.window._sources_panel._btn_scan.text(), "Scan")
        self.assertIn("cancelled", self.window._status_bar.currentMessage())


if __name__ == "__main__":
    unittest.main()
