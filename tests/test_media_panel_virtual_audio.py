from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from reencode.media_panel import MediaPanel


class MediaPanelVirtualAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_flag = os.environ.get("REENCODE_VIRTUAL_AUDIO_TABLE")
        os.environ["REENCODE_VIRTUAL_AUDIO_TABLE"] = "1"
        settings = QSettings()
        settings.remove("pagination/audio")
        self.panel = MediaPanel("Audio")

    def tearDown(self):
        self.panel.close()
        if self._old_flag is None:
            os.environ.pop("REENCODE_VIRTUAL_AUDIO_TABLE", None)
        else:
            os.environ["REENCODE_VIRTUAL_AUDIO_TABLE"] = self._old_flag

    def test_virtual_audio_path_add_and_probe_update(self):
        self.panel.add_files([
            ("C:/tmp/song.mp3", 1000, "1"),
        ])

        self.assertEqual(self.panel.file_count(), 1)
        self.assertIsNotNone(self.panel._virtual_model)

        self.panel.update_probes([
            ("C:/tmp/song.mp3", {"audio_codec": "aac", "audio_bitrate": 128000}),
        ])

        model = self.panel._virtual_model
        assert model is not None

        codec_text = model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole)
        rec_text = model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole)

        self.assertEqual(codec_text, "AAC")
        self.assertNotEqual(rec_text, "Pending probe")

    def test_virtual_audio_metadata_update_uses_estimate(self):
        self.panel.add_files([
            ("C:/tmp/song.mp3", 1000, "1"),
        ])

        self.panel.update_file_stats([
            ("C:/tmp/song.mp3", 1000, "1", 750),
        ])

        model = self.panel._virtual_model
        assert model is not None

        estimate_text = model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(estimate_text, "750 B (-25%)")

    def test_virtual_audio_probe_fallback_marks_low_confidence(self):
        self.panel.add_files([
            ("C:/tmp/song.mp3", 1000, "1"),
        ])

        self.panel.update_probes([
            ("C:/tmp/song.mp3", {"audio_codec": "unknown_codec"}),
        ])

        model = self.panel._virtual_model
        assert model is not None

        estimate_text = model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole)
        estimate_tip = model.data(model.index(0, 4), Qt.ItemDataRole.ToolTipRole)

        self.assertEqual(estimate_text, "750 B (-25%) ?")
        self.assertEqual(estimate_tip, "Fallback factor estimate due to unknown codec and missing bitrate context.")

    def test_virtual_audio_file_count_tracks_total_rows(self):
        rows = [(f"C:/tmp/song-{idx}.mp3", 1000 + idx, "1") for idx in range(1500)]
        self.panel.add_files(rows)

        self.assertEqual(self.panel.file_count(), 1500)
        model = self.panel._virtual_model
        assert model is not None
        self.assertEqual(model.rowCount(), 50)
        self.assertEqual(self.panel._page_label.text(), "Page 1 of 30")

    def test_virtual_audio_pagination_navigation(self):
        rows = [(f"C:/tmp/song-{idx:03d}.mp3", 1000 + idx, "1") for idx in range(80)]
        self.panel.add_files(rows)

        model = self.panel._virtual_model
        assert model is not None

        self.panel._table.sortByColumn(0, Qt.SortOrder.AscendingOrder)

        first_page_name = model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(first_page_name, "song-000.mp3")

        self.panel._on_next_page()
        self.assertEqual(self.panel._page_label.text(), "Page 2 of 2")

        second_page_name = model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(second_page_name, "song-050.mp3")

    def test_virtual_audio_filter_dialog_updates_visible_count_live(self):
        rows = [(f"C:/tmp/song-{idx:03d}.mp3", 1000 + idx, "1") for idx in range(80)]
        self.panel.add_files(rows)

        self.panel._show_filter_dialog()
        assert self.panel._filter_dialog is not None
        self.panel._filter_dialog.name_path_edit.setText("song-050")

        self.assertEqual(self.panel.file_count(), 1)
        self.assertEqual(self.panel._page_label.text(), "Page 1 of 1")
        self.assertEqual(self.panel._label.text(), "1/80 file")

        self.panel._filter_dialog.name_path_edit.clear()
        self.assertEqual(self.panel.file_count(), 80)
        self.assertEqual(self.panel._page_label.text(), "Page 1 of 2")

    def test_virtual_audio_filter_dialog_size_range(self):
        rows = [
            ("C:/tmp/small.mp3", 500 * 1024, "1"),
            ("C:/tmp/medium.mp3", 2 * 1024 * 1024, "1"),
            ("C:/tmp/large.mp3", 5 * 1024 * 1024, "1"),
        ]
        self.panel.add_files(rows)

        self.panel._show_filter_dialog()
        assert self.panel._filter_dialog is not None
        self.panel._filter_dialog.min_size_mb_edit.setText("1")
        self.panel._filter_dialog.max_size_mb_edit.setText("3")

        self.assertEqual(self.panel.file_count(), 1)
        model = self.panel._virtual_model
        assert model is not None
        self.assertEqual(model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole), "medium.mp3")


if __name__ == "__main__":
    unittest.main()
