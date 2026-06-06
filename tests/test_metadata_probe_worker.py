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


if __name__ == "__main__":
    unittest.main()
