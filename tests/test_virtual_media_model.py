from __future__ import annotations

import unittest

from PySide6.QtCore import Qt

from reencode.virtual_media_model import VirtualMediaTableModel, recommendation_color


class VirtualMediaTableModelTests(unittest.TestCase):
    def setUp(self):
        self.model = VirtualMediaTableModel(
            ["Name", "Size", "Codec", "Recommend", "Estimate", "Path", "Modified"],
            exposure_chunk=2,
        )

    def test_append_lookup_update(self):
        self.model.append_rows(
            [
                {
                    "name": "a.mp3",
                    "size_bytes": 100,
                    "size_text": "100 B",
                    "codec": "Probing...",
                    "recommend": "Pending probe",
                    "rec_reason": "Pending",
                    "rec_color": recommendation_color("pending"),
                    "estimate_text": "Pending probe",
                    "estimate_sort": -1,
                    "path": "C:/tmp/a.mp3",
                    "modified": "2026-01-01 10:00",
                }
            ]
        )

        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.row_for_path("C:/tmp/a.mp3"), 0)
        self.assertEqual(self.model.size_for_path("C:/tmp/a.mp3"), 100)

        changed = self.model.update_row(
            "C:/tmp/a.mp3",
            {
                "codec": "AAC",
                "recommend": "H.265/HEVC",
                "estimate_text": "80 B (-20%)",
                "estimate_sort": 80,
            },
        )
        self.assertTrue(changed)

        codec = self.model.data(self.model.index(0, 2), Qt.ItemDataRole.DisplayRole)
        estimate = self.model.data(self.model.index(0, 4), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(codec, "AAC")
        self.assertEqual(estimate, "80 B (-20%)")

    def test_pagination_row_count_and_page_switch(self):
        self.model.append_rows(
            [
                {
                    "name": f"f{idx}.mp3",
                    "size_bytes": idx,
                    "size_text": f"{idx} B",
                    "codec": "AAC",
                    "recommend": "Good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "-",
                    "estimate_sort": -1,
                    "path": f"C:/tmp/f{idx}.mp3",
                    "modified": "2026-01-01 10:00",
                }
                for idx in range(5)
            ]
        )

        self.assertEqual(self.model.rowCount(), 5)
        self.assertEqual(self.model.total_row_count(), 5)
        self.assertFalse(self.model.canFetchMore())

        self.model.set_pagination(page=1, page_size=2)
        self.assertEqual(self.model.rowCount(), 2)
        page_name = self.model.data(self.model.index(0, 0), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(page_name, "f2.mp3")

        self.model.set_pagination(page=2, page_size=2)
        self.assertEqual(self.model.rowCount(), 1)
        final_name = self.model.data(self.model.index(0, 0), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(final_name, "f4.mp3")

    def test_update_rows_applies_to_hidden_rows(self):
        self.model.append_rows(
            [
                {
                    "name": f"f{idx}.mp3",
                    "size_bytes": idx,
                    "size_text": f"{idx} B",
                    "codec": "AAC",
                    "recommend": "Good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "-",
                    "estimate_sort": -1,
                    "path": f"C:/tmp/f{idx}.mp3",
                    "modified": "2026-01-01 10:00",
                }
                for idx in range(3)
            ]
        )

        updated = self.model.update_rows({"C:/tmp/f2.mp3": {"codec": "FLAC"}})
        self.assertEqual(updated, 1)

        self.model.set_pagination(page=1, page_size=2)
        codec = self.model.data(self.model.index(2, 2), Qt.ItemDataRole.DisplayRole)
        self.assertIsNone(codec)

        page_codec = self.model.data(self.model.index(0, 2), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(page_codec, "FLAC")

    def test_sort_size_numeric(self):
        self.model.append_rows(
            [
                {
                    "name": "large.mp3",
                    "size_bytes": 500,
                    "size_text": "500 B",
                    "codec": "AAC",
                    "recommend": "Good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "400 B",
                    "estimate_sort": 400,
                    "path": "C:/tmp/large.mp3",
                    "modified": "2026-01-01 10:00",
                },
                {
                    "name": "small.mp3",
                    "size_bytes": 100,
                    "size_text": "100 B",
                    "codec": "AAC",
                    "recommend": "Good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "80 B",
                    "estimate_sort": 80,
                    "path": "C:/tmp/small.mp3",
                    "modified": "2026-01-01 10:01",
                },
            ]
        )

        self.model.sort(1, Qt.SortOrder.AscendingOrder)
        first_path = self.model.data(self.model.index(0, 5), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(first_path, "C:/tmp/small.mp3")

    def test_row_for_path_is_page_relative_when_visible(self):
        self.model.append_rows(
            [
                {
                    "name": f"f{idx}.mp3",
                    "size_bytes": idx,
                    "size_text": f"{idx} B",
                    "codec": "AAC",
                    "recommend": "Good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "-",
                    "estimate_sort": -1,
                    "path": f"C:/tmp/f{idx}.mp3",
                    "modified": "2026-01-01 10:00",
                }
                for idx in range(6)
            ]
        )

        self.model.set_pagination(page=1, page_size=2)
        self.assertEqual(self.model.row_for_path("C:/tmp/f2.mp3"), 0)
        self.assertEqual(self.model.row_for_path("C:/tmp/f3.mp3"), 1)
        self.assertIsNone(self.model.row_for_path("C:/tmp/f1.mp3"))

    def test_filter_name_path_and_codec(self):
        self.model.append_rows(
            [
                {
                    "name": "keep-me.mp3",
                    "size_bytes": 100,
                    "size_text": "100 B",
                    "codec": "AAC",
                    "recommend": "Original",
                    "rec_status": "good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "80 B",
                    "estimate_sort": 80,
                    "estimate_change_pct": 20.0,
                    "path": "C:/tmp/keep-me.mp3",
                    "modified": "2026-01-01 10:00",
                },
                {
                    "name": "ignore-me.mp3",
                    "size_bytes": 100,
                    "size_text": "100 B",
                    "codec": "FLAC",
                    "recommend": "Original",
                    "rec_status": "good",
                    "rec_reason": "Good",
                    "rec_color": recommendation_color("good"),
                    "estimate_text": "90 B",
                    "estimate_sort": 90,
                    "estimate_change_pct": 10.0,
                    "path": "C:/tmp/ignore-me.mp3",
                    "modified": "2026-01-01 10:01",
                },
            ]
        )

        self.model.set_filter_state({"name_path": "keep", "codec": "aac"})

        self.assertEqual(self.model.filtered_row_count(), 1)
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.data(self.model.index(0, 0), Qt.ItemDataRole.DisplayRole), "keep-me.mp3")

    def test_filter_recommendation_size_and_change_range(self):
        self.model.append_rows(
            [
                {
                    "name": "reencode-target.mp3",
                    "size_bytes": 2 * 1024 * 1024,
                    "size_text": "2.0 MB",
                    "codec": "AAC",
                    "recommend": "H.265/HEVC",
                    "rec_status": "reencode",
                    "rec_reason": "Re-encode",
                    "rec_color": recommendation_color("reencode"),
                    "estimate_text": "1.0 MB (-50%)",
                    "estimate_sort": 1 * 1024 * 1024,
                    "estimate_change_pct": 50.0,
                    "path": "C:/tmp/reencode-target.mp3",
                    "modified": "2026-01-01 10:00",
                },
                {
                    "name": "keep-target.mp3",
                    "size_bytes": 2 * 1024 * 1024,
                    "size_text": "2.0 MB",
                    "codec": "AAC",
                    "recommend": "AAC",
                    "rec_status": "optimal",
                    "rec_reason": "Keep",
                    "rec_color": recommendation_color("optimal"),
                    "estimate_text": "1.9 MB (-5%)",
                    "estimate_sort": int(1.9 * 1024 * 1024),
                    "estimate_change_pct": 5.0,
                    "path": "C:/tmp/keep-target.mp3",
                    "modified": "2026-01-01 10:01",
                },
            ]
        )

        self.model.set_filter_state(
            {
                "recommendation": "reencode",
                "min_size_mb": 1.0,
                "max_size_mb": 3.0,
                "min_change_pct": 25.0,
                "max_change_pct": 60.0,
            }
        )

        self.assertEqual(self.model.filtered_row_count(), 1)
        self.assertEqual(self.model.data(self.model.index(0, 0), Qt.ItemDataRole.DisplayRole), "reencode-target.mp3")

    def test_filter_applies_before_pagination(self):
        self.model.append_rows(
            [
                {
                    "name": f"f{idx}.mp3",
                    "size_bytes": idx + 100,
                    "size_text": f"{idx + 100} B",
                    "codec": "AAC",
                    "recommend": "H.265/HEVC" if idx % 2 == 0 else "AAC",
                    "rec_status": "reencode" if idx % 2 == 0 else "optimal",
                    "rec_reason": "reason",
                    "rec_color": recommendation_color("reencode" if idx % 2 == 0 else "optimal"),
                    "estimate_text": "-",
                    "estimate_sort": 50,
                    "estimate_change_pct": 50.0,
                    "path": f"C:/tmp/f{idx}.mp3",
                    "modified": "2026-01-01 10:00",
                }
                for idx in range(8)
            ]
        )

        self.model.set_filter_state({"recommendation": "reencode"})
        self.model.set_pagination(page=1, page_size=2)

        self.assertEqual(self.model.filtered_row_count(), 4)
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.row_for_path("C:/tmp/f4.mp3"), 0)
        self.assertEqual(self.model.row_for_path("C:/tmp/f6.mp3"), 1)
        self.assertIsNone(self.model.row_for_path("C:/tmp/f2.mp3"))


if __name__ == "__main__":
    unittest.main()
