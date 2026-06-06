from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reencode.main_window import _MetadataProbeWorker
from reencode.scan_store import ScanStore


class MetadataProbeWorkerTests(unittest.TestCase):
    def test_sqlite_reuse_skips_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir(parents=True, exist_ok=True)
            media_file = root / "video.mp4"
            media_file.write_bytes(b"test-bytes")

            stat = media_file.stat()
            store = ScanStore(Path(tmp) / "scan.db")
            try:
                store.upsert_record(
                    absolute_path=str(media_file),
                    source_root=str(root),
                    media_type="Videos",
                    file_size=stat.st_size,
                    last_modified=int(stat.st_mtime),
                    scan_id=1,
                    encoding="h264",
                    probe={"video_codec": "h264"},
                )
            finally:
                store.close()

            worker = _MetadataProbeWorker(
                scan_id=2,
                store_path=str(Path(tmp) / "scan.db"),
                source_roots=[str(root)],
            )

            completed_payloads: list[tuple[int, str, bool, int, int]] = []
            worker.completed.connect(lambda *args: completed_payloads.append(args))

            worker.submit("Videos", str(media_file))
            worker.finish(expected_total=1)

            with mock.patch("reencode.codec_probe.probe_media_info", side_effect=AssertionError("ffprobe should not be called")):
                worker.run()

            self.assertEqual(len(completed_payloads), 1)
            scan_id, phase, cancelled, processed, probed = completed_payloads[0]
            self.assertEqual(scan_id, 2)
            self.assertEqual(phase, "metadata")
            self.assertFalse(cancelled)
            self.assertEqual(processed, 1)
            self.assertEqual(probed, 1)

    def test_storage_upsert_failure_is_non_fatal_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir(parents=True, exist_ok=True)

            bad_file = root / "bad.jpg"
            good_file = root / "good.jpg"
            bad_file.write_bytes(b"bad")
            good_file.write_bytes(b"good")

            class _FakeStore:
                def __init__(self):
                    self.committed = False

                def find_reusable_probe(self, absolute_path: str, file_size: int, last_modified: int):
                    return None

                def upsert_record(self, **kwargs):
                    path = kwargs.get("absolute_path", "")
                    if str(path).endswith("bad.jpg"):
                        raise RuntimeError("write blocked")

                def commit(self):
                    self.committed = True

                def close(self):
                    return None

            fake_store = _FakeStore()
            worker = _MetadataProbeWorker(
                scan_id=7,
                store_path=str(Path(tmp) / "scan.db"),
                source_roots=[str(root)],
            )

            failed_payloads: list[tuple[int, str, str, str, str]] = []
            completed_payloads: list[tuple[int, str, bool, int, int]] = []
            row_payloads: list[tuple] = []
            worker.failed_item.connect(lambda *args: failed_payloads.append(args))
            worker.completed.connect(lambda *args: completed_payloads.append(args))
            worker.row_ready.connect(lambda *args: row_payloads.append(args))

            worker.submit("Images", str(bad_file))
            worker.submit("Images", str(good_file))
            worker.finish(expected_total=2)

            with mock.patch("reencode.main_window.ScanStore", return_value=fake_store):
                worker.run()

            self.assertEqual(len(row_payloads), 2)
            self.assertEqual(len(failed_payloads), 1)
            self.assertIn("Storage upsert failed", failed_payloads[0][3])
            self.assertEqual(failed_payloads[0][4], "storage")

            self.assertEqual(len(completed_payloads), 1)
            scan_id, phase, cancelled, processed, probed = completed_payloads[0]
            self.assertEqual(scan_id, 7)
            self.assertEqual(phase, "metadata")
            self.assertFalse(cancelled)
            self.assertEqual(processed, 2)
            self.assertEqual(probed, 0)
            self.assertTrue(fake_store.committed)

    def test_cancel_stops_processing_remaining_queued_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir(parents=True, exist_ok=True)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            worker = _MetadataProbeWorker(
                scan_id=9,
                store_path=str(Path(tmp) / "scan.db"),
                source_roots=[str(root)],
            )

            row_payloads: list[tuple] = []
            completed_payloads: list[tuple[int, str, bool, int, int]] = []

            def _on_row_ready(*args):
                row_payloads.append(args)
                if len(row_payloads) == 1:
                    worker.cancel()

            worker.row_ready.connect(_on_row_ready)
            worker.completed.connect(lambda *args: completed_payloads.append(args))

            worker.submit("Videos", str(first))
            worker.submit("Videos", str(second))
            worker.finish(expected_total=2)

            with mock.patch(
                "reencode.codec_probe.probe_media_info",
                return_value={"video_codec": "h264", "duration": 1.0},
            ):
                worker.run()

            self.assertEqual(len(row_payloads), 1)
            self.assertEqual(len(completed_payloads), 1)
            scan_id, phase, cancelled, processed, _probed = completed_payloads[0]
            self.assertEqual(scan_id, 9)
            self.assertEqual(phase, "metadata")
            self.assertTrue(cancelled)
            self.assertEqual(processed, 1)


if __name__ == "__main__":
    unittest.main()
