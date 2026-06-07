from __future__ import annotations

import unittest

from reencode import presets


class PresetLoadingTests(unittest.TestCase):
    def test_load_presets_contains_expected_entries(self):
        items = presets.load_presets()

        self.assertGreaterEqual(len(items), 1)
        ids = {item.id for item in items}
        self.assertIn("archive", ids)
        self.assertIn("streaming", ids)

    def test_lookup_by_id_and_media_entry(self):
        items = presets.load_presets()
        by_id = presets.presets_by_id(items)

        archive = by_id.get("archive")
        self.assertIsNotNone(archive)
        assert archive is not None

        video_entry = presets.media_entry_for_type(archive, "Videos")
        audio_entry = presets.media_entry_for_type(archive, "Audio")
        image_entry = presets.media_entry_for_type(archive, "Images")

        self.assertIsNotNone(video_entry)
        self.assertIsNotNone(audio_entry)
        self.assertIsNotNone(image_entry)
        assert video_entry is not None
        assert audio_entry is not None
        assert image_entry is not None

        self.assertEqual(video_entry.codec, "AV1")
        self.assertEqual(audio_entry.codec, "FLAC")
        self.assertEqual(image_entry.codec, "JPEG XL")


if __name__ == "__main__":
    unittest.main()
