from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from reencode import codec_probe
from reencode.media_panel import _ConversionThread
from reencode.subprocess_util import popen_hidden, run_hidden


class SubprocessHiddenWindowsTests(unittest.TestCase):
    def test_run_hidden_applies_windows_no_window_flag(self):
        with mock.patch("reencode.subprocess_util.subprocess.run") as run_mock:
            run_hidden(["ffprobe", "-version"], capture_output=True)

        self.assertEqual(run_mock.call_count, 1)
        kwargs = run_mock.call_args.kwargs

        if sys.platform == "win32":
            expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if expected_flag:
                self.assertIn("creationflags", kwargs)
                self.assertTrue(kwargs["creationflags"] & expected_flag)
        else:
            self.assertNotIn("creationflags", kwargs)

    def test_popen_hidden_applies_windows_no_window_flag(self):
        with mock.patch("reencode.subprocess_util.subprocess.Popen") as popen_mock:
            popen_hidden(["ffmpeg", "-version"], stdout=subprocess.PIPE)

        self.assertEqual(popen_mock.call_count, 1)
        kwargs = popen_mock.call_args.kwargs

        if sys.platform == "win32":
            expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if expected_flag:
                self.assertIn("creationflags", kwargs)
                self.assertTrue(kwargs["creationflags"] & expected_flag)
        else:
            self.assertNotIn("creationflags", kwargs)

    def test_codec_probe_uses_hidden_runner(self):
        codec_probe.probe_media_info.cache_clear()
        completed = mock.Mock(stdout="{}")

        with mock.patch("reencode.codec_probe.run_hidden", return_value=completed) as run_hidden_mock:
            result = codec_probe.probe_media_info("C:/tmp/file.mp4")

        self.assertEqual(run_hidden_mock.call_count, 1)
        self.assertIsInstance(result, dict)

    def test_conversion_thread_uses_hidden_popen(self):
        thread = _ConversionThread(
            jobs=[("Audio", "C:/tmp/in.mp3", "C:/tmp/out.m4a", "")],
            replace_originals=False,
        )

        finished_payloads: list[tuple[bool, str]] = []
        thread.finished.connect(lambda *args: finished_payloads.append(args))

        with mock.patch("reencode.media_panel.codec_probe.probe_media_info", return_value={"duration": 1.0}):
            with mock.patch("reencode.media_panel.popen_hidden", side_effect=FileNotFoundError):
                thread.run()

        self.assertEqual(len(finished_payloads), 1)
        success, message = finished_payloads[0]
        self.assertFalse(success)
        self.assertIn("ffmpeg was not found", message)


if __name__ == "__main__":
    unittest.main()
