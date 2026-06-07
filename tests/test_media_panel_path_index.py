from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView

from reencode.media_panel import FailedPanel, MediaPanel, VCOL_CODEC, VCOL_ESTIMATE, VCOL_REC, VCOL_SELECT


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
        self.panel._invalidate_path_rows()

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

    def test_scan_lock_disables_table_and_pagination_controls(self):
        self.panel.add_file("C:/tmp/locked.mp4", 100, "1")

        self.panel.set_scan_locked(True)
        self.assertTrue(self.panel._table.isEnabled())
        self.assertFalse(self.panel._page_size_combo.isEnabled())
        self.assertFalse(self.panel._prev_page_button.isEnabled())
        self.assertFalse(self.panel._next_page_button.isEnabled())
        self.assertEqual(self.panel._table.selectionMode(), QAbstractItemView.SelectionMode.NoSelection)

        self.panel.set_scan_locked(False)
        self.assertTrue(self.panel._table.isEnabled())
        self.assertTrue(self.panel._page_size_combo.isEnabled())


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

    def test_failed_panel_scan_lock_disables_table_and_pagination(self):
        self.panel.add_failures([("bad.mp4", "probe failed", "C:/tmp/bad.mp4")])

        self.panel.set_scan_locked(True)
        self.assertTrue(self.panel._table.isEnabled())
        self.assertFalse(self.panel._page_size_combo.isEnabled())
        self.assertFalse(self.panel._prev_page_button.isEnabled())
        self.assertFalse(self.panel._next_page_button.isEnabled())
        self.assertEqual(self.panel._table.selectionMode(), QAbstractItemView.SelectionMode.NoSelection)

        self.panel.set_scan_locked(False)
        self.assertTrue(self.panel._table.isEnabled())
        self.assertTrue(self.panel._page_size_combo.isEnabled())


class MediaPanelConversionParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_audio_panel_exposes_selection_and_convert_controls(self):
        panel = MediaPanel("Audio")
        try:
            panel.add_file("C:/tmp/song.mp3", 1000, "1")
            select_item = panel._table.item(0, VCOL_SELECT)
            self.assertIsNotNone(select_item)
            assert select_item is not None
            select_item.setCheckState(Qt.CheckState.Checked)

            self.assertTrue(hasattr(panel, "_convert_button"))
            self.assertTrue(panel._convert_button.isEnabled())
        finally:
            panel.close()

    def test_images_panel_forces_copy_mode(self):
        panel = MediaPanel("Images")
        try:
            panel.add_file("C:/tmp/picture.jpg", 2048, "1")
            select_item = panel._table.item(0, VCOL_SELECT)
            self.assertIsNotNone(select_item)
            assert select_item is not None
            select_item.setCheckState(Qt.CheckState.Checked)

            self.assertTrue(hasattr(panel, "_do_not_replace"))
            self.assertTrue(panel._do_not_replace.isChecked())
            self.assertFalse(panel._do_not_replace.isEnabled())
            self.assertTrue(panel._convert_button.isEnabled())
        finally:
            panel.close()

    def test_images_panel_uses_type_and_recommended_extension(self):
        panel = MediaPanel("Images")
        try:
            image_path = "C:/tmp/picture.PNG"
            panel.add_file(image_path, 2048, "1")

            header_type = panel._table.horizontalHeaderItem(VCOL_CODEC)
            self.assertIsNotNone(header_type)
            assert header_type is not None
            self.assertEqual(header_type.text(), "Type")

            type_item = panel._table.item(0, VCOL_CODEC)
            recommend_item = panel._table.item(0, VCOL_REC)
            self.assertIsNotNone(type_item)
            self.assertIsNotNone(recommend_item)
            assert type_item is not None
            assert recommend_item is not None
            self.assertEqual(type_item.text(), ".png")
            self.assertEqual(recommend_item.text(), ".webp")

            panel.update_probes([(image_path, {"video_codec": "h264"})])

            updated_type_item = panel._table.item(0, VCOL_CODEC)
            updated_recommend_item = panel._table.item(0, VCOL_REC)
            self.assertIsNotNone(updated_type_item)
            self.assertIsNotNone(updated_recommend_item)
            assert updated_type_item is not None
            assert updated_recommend_item is not None
            self.assertEqual(updated_type_item.text(), ".png")
            self.assertEqual(updated_recommend_item.text(), ".webp")
        finally:
            panel.close()

    def test_video_recommendation_updates_when_preset_changes(self):
        panel = MediaPanel("Videos")
        try:
            path = "C:/tmp/video.mp4"
            panel.add_file(path, 2048, "1")
            panel.update_probes([(path, {"video_codec": "h264", "duration": 10.0})])

            panel.set_active_preset("compatibility")
            rec_item = panel._table.item(0, VCOL_REC)
            estimate_compat = panel._table.item(0, VCOL_ESTIMATE)
            self.assertIsNotNone(rec_item)
            self.assertIsNotNone(estimate_compat)
            assert rec_item is not None
            assert estimate_compat is not None
            self.assertEqual(rec_item.text(), "H.264")
            estimate_compat_text = estimate_compat.text()

            panel.set_active_preset("archive")
            rec_item = panel._table.item(0, VCOL_REC)
            estimate_archive = panel._table.item(0, VCOL_ESTIMATE)
            self.assertIsNotNone(rec_item)
            self.assertIsNotNone(estimate_archive)
            assert rec_item is not None
            assert estimate_archive is not None
            self.assertEqual(rec_item.text(), "AV1")
            self.assertNotEqual(estimate_compat_text, estimate_archive.text())
        finally:
            panel.close()

    def test_audio_keep_original_recommendation_uses_source_codec_label(self):
        panel = MediaPanel("Audio")
        try:
            path = "C:/tmp/audio.mp3"
            panel.add_file(path, 1024, "1")
            panel.update_probes([(path, {"audio_codec": "aac", "duration": 3.0})])
            panel.set_active_preset("keep_original")

            rec_item = panel._table.item(0, VCOL_REC)
            self.assertIsNotNone(rec_item)
            assert rec_item is not None
            self.assertEqual(rec_item.text(), "AAC")
        finally:
            panel.close()


if __name__ == "__main__":
    unittest.main()
