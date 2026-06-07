from __future__ import annotations

import unittest

from reencode import size_estimator


class SizeEstimatorTests(unittest.TestCase):
    def test_video_uses_bitrate_duration_when_available(self):
        details = size_estimator.estimate_output_details(
            size_bytes=2_000_000,
            media_type="Videos",
            path="C:/tmp/video.mp4",
            probe_info={
                "video_codec": "h264",
                "duration": 10.0,
                "video_bitrate": 800_000,
            },
        )

        self.assertEqual(details.mode, "bitrate")
        self.assertEqual(details.estimated_size, 580_000)
        self.assertAlmostEqual(details.savings_ratio or 0.0, 0.71, places=2)

    def test_unknown_audio_codec_uses_low_confidence_fallback(self):
        details = size_estimator.estimate_output_details(
            size_bytes=1_000,
            media_type="Audio",
            path="C:/tmp/song.abc",
            probe_info={"audio_codec": "mystery"},
        )

        self.assertEqual(details.mode, "factor")
        self.assertEqual(details.confidence, "low")
        self.assertTrue(details.fallback_used)
        self.assertEqual(details.estimated_size, 750)

        formatted = size_estimator.format_estimate("750 B", details.savings_ratio, low_confidence=True)
        self.assertEqual(formatted, "750 B (-25%) ?")

    def test_image_estimate_applies_size_tier_adjustment(self):
        details = size_estimator.estimate_output_details(
            size_bytes=1_000,
            media_type="Images",
            path="C:/tmp/image.png",
            probe_info=None,
        )

        self.assertEqual(details.mode, "factor")
        self.assertEqual(details.estimated_size, 630)

    def test_estimate_output_back_compatibility(self):
        est_size, savings = size_estimator.estimate_output(
            size_bytes=1_000,
            media_type="Audio",
            path="C:/tmp/song.mp3",
            probe_info={"audio_codec": "mp3"},
        )

        self.assertEqual(est_size, 720)
        self.assertAlmostEqual(savings or 0.0, 0.28, places=2)


if __name__ == "__main__":
    unittest.main()