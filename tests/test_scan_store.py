from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reencode.scan_store import ScanStore


class ScanStoreTests(unittest.TestCase):
    def test_prune_scope_removes_only_stale_records_for_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "scan.db"
            store = ScanStore(db_path=db_path)
            try:
                root_a = str(Path(tmp) / "rootA")
                root_b = str(Path(tmp) / "rootB")
                stale_path = str(Path(root_a) / "old.mp4")
                fresh_path = str(Path(root_a) / "new.mp4")
                other_scope_path = str(Path(root_b) / "keep.mp4")

                store.upsert_record(
                    absolute_path=stale_path,
                    source_root=root_a,
                    media_type="Videos",
                    file_size=100,
                    last_modified=10,
                    scanned_at=100,
                    encoding="h264",
                    probe={"video_codec": "h264"},
                )
                store.upsert_record(
                    absolute_path=fresh_path,
                    source_root=root_a,
                    media_type="Videos",
                    file_size=101,
                    last_modified=11,
                    scanned_at=200,
                    encoding="h265",
                    probe={"video_codec": "hevc"},
                )
                store.upsert_record(
                    absolute_path=other_scope_path,
                    source_root=root_b,
                    media_type="Audio",
                    file_size=12,
                    last_modified=12,
                    scanned_at=100,
                    encoding="aac",
                    probe={"audio_codec": "aac"},
                )

                removed = store.prune_scan_scope([root_a], scan_started_at=150)

                self.assertEqual(removed, 1)
                self.assertIsNone(store.get_record(stale_path))
                self.assertIsNotNone(store.get_record(fresh_path))
                self.assertIsNotNone(store.get_record(other_scope_path))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
