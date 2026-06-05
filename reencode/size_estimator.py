"""Estimate output file size and savings percentage by media type."""

from __future__ import annotations

import os


_VIDEO_FACTORS = {
    "av1": 0.95,
    "hevc": 0.80,
    "vp9": 0.82,
    "h264": 0.58,
    "vp8": 0.52,
    "mpeg4": 0.45,
    "xvid": 0.45,
    "divx": 0.45,
    "mpeg2video": 0.30,
    "mpeg1video": 0.30,
    "wmv1": 0.35,
    "wmv2": 0.35,
    "wmv3": 0.35,
    "flv1": 0.35,
    "theora": 0.40,
    "h263": 0.40,
    "mjpeg": 0.20,
    "prores": 0.18,
    "dnxhd": 0.20,
}

_AUDIO_FACTORS = {
    "opus": 0.95,
    "aac": 0.90,
    "vorbis": 0.90,
    "mp3": 0.72,
    "wma": 0.70,
    "flac": 0.55,
    "wav": 0.20,
    "pcm_s16le": 0.20,
    "pcm_s24le": 0.20,
    "aiff": 0.22,
}

_IMAGE_FACTORS = {
    ".jpg": 0.95,
    ".jpeg": 0.95,
    ".webp": 0.95,
    ".heic": 0.94,
    ".heif": 0.94,
    ".png": 0.60,
    ".bmp": 0.40,
    ".gif": 0.65,
    ".tif": 0.55,
    ".tiff": 0.55,
    ".svg": 0.90,
    ".ico": 0.85,
}


def _clamp_factor(value: float) -> float:
    if value < 0.05:
        return 0.05
    if value > 0.99:
        return 0.99
    return value


def estimate_output(size_bytes: int, media_type: str, path: str, probe_info: dict | None) -> tuple[int | None, float | None]:
    """Return estimated output size bytes and savings ratio (0-1).

    savings ratio is the fractional reduction: 0.45 means 45% smaller.
    """
    if size_bytes <= 0:
        return None, None

    factor = None
    media_key = media_type.lower()

    if media_key == "videos":
        codec = ((probe_info or {}).get("video_codec") or "").lower()
        factor = _VIDEO_FACTORS.get(codec, 0.60)
    elif media_key == "audio":
        codec = ((probe_info or {}).get("audio_codec") or "").lower()
        factor = _AUDIO_FACTORS.get(codec, 0.75)
    elif media_key == "images":
        ext = os.path.splitext(path)[1].lower()
        factor = _IMAGE_FACTORS.get(ext, 0.80)

    if factor is None:
        return None, None

    factor = _clamp_factor(factor)
    est_size = max(1, int(size_bytes * factor))
    savings = max(0.0, min(0.99, 1.0 - (est_size / size_bytes)))
    return est_size, savings


def format_estimate(human_size_text: str, savings_ratio: float | None) -> str:
    """Return display text for estimate cell."""
    if savings_ratio is None:
        return human_size_text
    return f"{human_size_text} ({savings_ratio * 100:.0f}% smaller)"
