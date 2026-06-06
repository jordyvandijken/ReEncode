from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from reencode.media_panel import FailedPanel, MediaPanel


class MediaPanelPathIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        settings = QSettings()
        settings.remove("pagination/videos")
        settings.remove("pagination/failed")
        self.panel = MediaPanel("Videos")

    def tearDown(self):
        self.panel.close()

    def test_missing_path_does_not_rebuild_when_index_is_fresh(self):
        self.panel.add_files([
            ("C:/tmp/one.mp4", 100, "1"),
            ("C:/tmp/two.mp4", 200, "2"),
        ])
        self.panel._rebuild_path_rows()

        rebuild_calls = 0
        original = self.panel._rebuild_path_rows

        def _wrapped_rebuild():
            nonlocal rebuild_calls
            rebuild_calls += 1
            original()

        self.panel._rebuild_path_rows = _wrapped_rebuild
        try:
            row = self.panel._row_for_path("C:/tmp/missing.mp4")
        finally:
            self.panel._rebuild_path_rows = original

        self.assertIsNone(row)
        self.assertEqual(rebuild_calls, 0)

    def test_dirty_index_rebuilds_once_then_serves_lookups(self):
        self.panel.add_files([
            ("C:/tmp/a.mp4", 100, "1"),
            ("C:/tmp/b.mp4", 200, "2"),
        ])

        rebuild_calls = 0
        original = self.panel._rebuild_path_rows

        def _wrapped_rebuild():
            nonlocal rebuild_calls
            rebuild_calls += 1
            original()

        self.panel._rebuild_path_rows = _wrapped_rebuild
        try:
            row_a = self.panel._row_for_path("C:/tmp/a.mp4")
            row_b = self.panel._row_for_path("C:/tmp/b.mp4")
        finally:
            self.panel._rebuild_path_rows = original

        self.assertIsNotNone(row_a)
        self.assertIsNotNone(row_b)
        self.assertEqual(rebuild_calls, 1)

    def test_clear_resets_index_state(self):
        self.panel.add_file("C:/tmp/video.mp4", 100, "1")
        self.panel.clear()

        self.assertEqual(self.panel.file_count(), 0)
        self.assertEqual(self.panel._path_rows, {})
        self.assertFalse(self.panel._path_rows_dirty)

    def test_pagination_hides_rows_but_lookup_still_resolves(self):
        self.panel.add_files(
            [
                (f"C:/tmp/video-{idx}.mp4", 100 + idx, str(idx))
                for idx in range(80)
            ]
        )

        self.assertEqual(self.panel.file_count(), 80)
        self.assertEqual(self.panel._pagination_page_size, 50)
        self.assertEqual(self.panel._page_label.text(), "Page 1 of 2")

        visible = [row for row in range(self.panel._table.rowCount()) if not self.panel._table.isRowHidden(row)]
        self.assertEqual(len(visible), 50)

        hidden_row = self.panel._row_for_path("C:/tmp/video-75.mp4")
        self.assertIsNotNone(hidden_row)
        assert hidden_row is not None
        self.assertTrue(self.panel._table.isRowHidden(hidden_row))

    def test_pagination_next_page_changes_visibility(self):
        self.panel.add_files(
            [
                (f"C:/tmp/video-{idx}.mp4", 100 + idx, str(idx))
                for idx in range(65)
            ]
        )

        self.assertEqual(self.panel._page_label.text(), "Page 1 of 2")
        self.panel._on_next_page()
        self.assertEqual(self.panel._page_label.text(), "Page 2 of 2")

        hidden_row = self.panel._row_for_path("C:/tmp/video-0.mp4")
        self.assertIsNotNone(hidden_row)
        assert hidden_row is not None
        self.assertTrue(self.panel._table.isRowHidden(hidden_row))


class FailedPanelPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        settings = QSettings()
        settings.remove("pagination/failed")
        self.panel = FailedPanel()

    def tearDown(self):
        self.panel.close()

    def test_failed_panel_pagination_and_navigation(self):
        self.panel.add_failures(
            [
                (f"file-{idx}.mp4", "probe failed", f"C:/tmp/file-{idx}.mp4")
                for idx in range(65)
            ]
        )

        self.assertEqual(self.panel.file_count(), 65)
        self.assertEqual(self.panel._page_label.text(), "Page 1 of 2")

        visible_first_page = [
            row for row in range(self.panel._table.rowCount()) if not self.panel._table.isRowHidden(row)
        ]
        self.assertEqual(len(visible_first_page), 50)

        self.panel._on_next_page()
        self.assertEqual(self.panel._page_label.text(), "Page 2 of 2")

        visible_second_page = [
            row for row in range(self.panel._table.rowCount()) if not self.panel._table.isRowHidden(row)
        ]
        self.assertEqual(len(visible_second_page), 15)


if __name__ == "__main__":
    unittest.main()
