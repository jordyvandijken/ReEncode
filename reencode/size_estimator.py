"""Estimate output file size and savings percentage by media type."""

from __future__ import annotations

from dataclasses import dataclass
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

_VIDEO_UNKNOWN_FACTOR = 0.60
_AUDIO_UNKNOWN_FACTOR = 0.75
_IMAGE_UNKNOWN_FACTOR = 0.80


@dataclass(frozen=True)
class EstimateDetails:
    estimated_size: int | None
    savings_ratio: float | None
    mode: str
    confidence: str
    fallback_used: bool
    clamped: bool
    reason: str | None


def _clamp_factor(value: float) -> float:
    if value < 0.05:
        return 0.05
    if value > 0.99:
        return 0.99
    return value


def _estimate_from_bitrate(duration_seconds: float, bitrate_bps: int, factor: float) -> int:
    source_size = (bitrate_bps / 8.0) * duration_seconds
    return max(1, int(source_size * factor))


def _image_tier_adjustment(size_bytes: int) -> float:
    # Small images often have less redundant data; very large images usually have more room to shrink.
    if size_bytes <= 512 * 1024:
        return 1.05
    if size_bytes >= 5 * 1024 * 1024:
        return 0.92
    return 1.00


def _safe_positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def estimate_output_details(size_bytes: int, media_type: str, path: str, probe_info: dict | None) -> EstimateDetails:
    """Return estimate details including confidence and fallback metadata."""
    if size_bytes <= 0:
        return EstimateDetails(None, None, "unavailable", "none", False, False, "Invalid source size.")

    factor: float | None = None
    fallback_used = False
    media_key = media_type.lower()
    clamped = False

    if media_key == "videos":
        codec = ((probe_info or {}).get("video_codec") or "").lower()
        factor = _VIDEO_FACTORS.get(codec)
        if factor is None:
            factor = _VIDEO_UNKNOWN_FACTOR
            fallback_used = True
    elif media_key == "audio":
        codec = ((probe_info or {}).get("audio_codec") or "").lower()
        factor = _AUDIO_FACTORS.get(codec)
        if factor is None:
            factor = _AUDIO_UNKNOWN_FACTOR
            fallback_used = True
    elif media_key == "images":
        ext = os.path.splitext(path)[1].lower()
        factor = _IMAGE_FACTORS.get(ext)
        if factor is None:
            factor = _IMAGE_UNKNOWN_FACTOR
            fallback_used = True
        factor *= _image_tier_adjustment(size_bytes)
    else:
        return EstimateDetails(None, None, "unavailable", "none", False, False, "Unsupported media type.")

    clamped_factor = _clamp_factor(factor)
    if clamped_factor != factor:
        clamped = True
    factor = clamped_factor

    estimate_mode = "factor"
    reason = "Codec or extension factor estimate."
    est_size = max(1, int(size_bytes * factor))

    if media_key in {"videos", "audio"}:
        duration = _safe_positive_float((probe_info or {}).get("duration"))
        if media_key == "videos":
            bitrate = _safe_positive_int((probe_info or {}).get("video_bitrate"))
            if bitrate is None:
                bitrate = _safe_positive_int((probe_info or {}).get("format_bitrate"))
        else:
            bitrate = _safe_positive_int((probe_info or {}).get("audio_bitrate"))
            if bitrate is None:
                bitrate = _safe_positive_int((probe_info or {}).get("format_bitrate"))

        if duration is not None and bitrate is not None:
            est_size = _estimate_from_bitrate(duration, bitrate, factor)
            estimate_mode = "bitrate"
            reason = "Bitrate-duration estimate adjusted by codec factor."
        elif fallback_used:
            reason = "Fallback factor estimate due to unknown codec and missing bitrate context."

    savings = 1.0 - (est_size / size_bytes)
    bounded_savings = max(0.0, min(0.99, savings))
    if bounded_savings != savings:
        clamped = True

    if fallback_used:
        confidence = "low"
    elif estimate_mode == "bitrate":
        confidence = "medium"
    else:
        confidence = "medium"

    return EstimateDetails(
        estimated_size=est_size,
        savings_ratio=bounded_savings,
        mode=estimate_mode,
        confidence=confidence,
        fallback_used=fallback_used,
        clamped=clamped,
        reason=reason,
    )


def estimate_output(size_bytes: int, media_type: str, path: str, probe_info: dict | None) -> tuple[int | None, float | None]:
    """Return estimated output size bytes and savings ratio (0-1).

    savings ratio is the fractional reduction: 0.45 means 45% smaller.
    """
    details = estimate_output_details(size_bytes, media_type, path, probe_info)
    return details.estimated_size, details.savings_ratio


def format_estimate(human_size_text: str, savings_ratio: float | None, low_confidence: bool = False) -> str:
    """Return display text for estimate cell."""
    if savings_ratio is None:
        return human_size_text
    change_pct = -savings_ratio * 100
    sign = "+" if change_pct >= 0 else ""
    display = f"{human_size_text} ({sign}{change_pct:.0f}%)"
    if low_confidence:
        return f"{display} ?"
    return display
