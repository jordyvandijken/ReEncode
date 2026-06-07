from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from reencode.sources_panel import SourcesPanel


class SourcesPanelPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = SourcesPanel()

    def tearDown(self):
        self.panel.close()

    def test_preset_button_exists(self):
        self.assertTrue(hasattr(self.panel, "_btn_preset"))
        self.assertTrue(self.panel._btn_preset.isEnabled())

    def test_set_scanning_disables_preset_button(self):
        self.panel.set_scanning(True)
        self.assertFalse(self.panel._btn_preset.isEnabled())

        self.panel.set_scanning(False)
        self.assertTrue(self.panel._btn_preset.isEnabled())

    def test_accepting_dialog_emits_preset_selected(self):
        emitted: list[str] = []
        self.panel.preset_selected.connect(lambda value: emitted.append(value))

        class _FakeDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected_preset_id(self):
                return "streaming"

        with mock.patch("reencode.sources_panel._PresetDialog", _FakeDialog):
            self.panel._on_preset()

        self.assertEqual(emitted, ["streaming"])


if __name__ == "__main__":
    unittest.main()
