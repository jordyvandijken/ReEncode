"""Probe video/audio codec info via ffprobe and return encoding recommendations."""

import json
import subprocess
from functools import lru_cache


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Codec display names  (ffprobe codec_name → human label)
# ---------------------------------------------------------------------------
_CODEC_LABELS: dict[str, str] = {
    "h264":        "H.264/AVC",
    "hevc":        "H.265/HEVC",
    "av1":         "AV1",
    "vp9":         "VP9",
    "vp8":         "VP8",
    "mpeg4":       "MPEG-4",
    "mpeg2video":  "MPEG-2",
    "mpeg1video":  "MPEG-1",
    "wmv1":        "WMV1",
    "wmv2":        "WMV2",
    "wmv3":        "WMV3",
    "flv1":        "FLV/Sorenson",
    "theora":      "Theora",
    "xvid":        "Xvid",
    "divx":        "DivX",
    "mjpeg":       "M-JPEG",
    "prores":      "ProRes",
    "dnxhd":       "DNxHD",
    "h263":        "H.263",
}


# ---------------------------------------------------------------------------
# Recommendation table
# ---------------------------------------------------------------------------
# Status values: "optimal" | "good" | "reencode"
_RECS: dict[str, tuple[str, str, str]] = {
    # codec_name   → (status,    recommended_label,    reason)
    "av1":        ("optimal", "AV1",        "Best compression + streaming available."),
    "hevc":       ("good",    "H.265/HEVC", "Already efficient. Re-encode to AV1 for maximum size reduction."),
    "vp9":        ("good",    "VP9",        "Good compression. H.265/HEVC has wider hardware-decode support."),
    "h264":       ("reencode","H.265/HEVC", "Re-encoding to H.265 saves ~40–50 % with identical visual quality."),
    "vp8":        ("reencode","H.265/HEVC", "VP8 is obsolete; H.265 is far more efficient."),
    "mpeg4":      ("reencode","H.265/HEVC", "MPEG-4 is outdated; H.265 gives much better compression."),
    "xvid":       ("reencode","H.265/HEVC", "Xvid is outdated; H.265 gives much better compression."),
    "divx":       ("reencode","H.265/HEVC", "DivX is outdated; H.265 gives much better compression."),
    "mpeg2video": ("reencode","H.265/HEVC", "MPEG-2 is very inefficient; H.265 cuts size by 60–75 %."),
    "mpeg1video": ("reencode","H.265/HEVC", "MPEG-1 is very inefficient; H.265 cuts size by 60–75 %."),
    "wmv1":       ("reencode","H.265/HEVC", "WMV is proprietary and inefficient; H.265 is the better choice."),
    "wmv2":       ("reencode","H.265/HEVC", "WMV is proprietary and inefficient; H.265 is the better choice."),
    "wmv3":       ("reencode","H.265/HEVC", "WMV is proprietary and inefficient; H.265 is the better choice."),
    "flv1":       ("reencode","H.265/HEVC", "FLV/Sorenson is obsolete; H.265 is the better choice."),
    "theora":     ("reencode","H.265/HEVC", "Theora is outdated; H.265 gives much better compression."),
    "h263":       ("reencode","H.265/HEVC", "H.263 is very old; H.265 gives much better compression."),
    "mjpeg":      ("reencode","H.265/HEVC", "M-JPEG is a lossless-frame format; H.265 is far more compact for delivery."),
    # Broadcast / post-production codecs — keep as-is for editing, re-encode for delivery
    "prores":     ("reencode","H.265/HEVC", "ProRes is a post-production codec; re-encode to H.265 for delivery/streaming."),
    "dnxhd":      ("reencode","H.265/HEVC", "DNxHD is a post-production codec; re-encode to H.265 for delivery/streaming."),
}

_DEFAULT_REC = ("reencode", "H.265/HEVC", "H.265/HEVC is the best general-purpose choice for streaming and size.")


def codec_label(codec_name: str) -> str:
    """Return a human-readable label for a raw ffprobe codec name."""
    return _CODEC_LABELS.get(codec_name.lower(), codec_name.upper())


def recommendation(codec_name: str) -> tuple[str, str, str]:
    """Return (status, recommended_label, reason) for *codec_name*.

    status is one of:
        'optimal'  – no action needed
        'good'     – acceptable but improvement possible
        'reencode' – re-encoding is strongly recommended
    """
    return _RECS.get(codec_name.lower(), _DEFAULT_REC)


@lru_cache(maxsize=2048)
def probe_media_info(path: str) -> dict | None:
    """Run ffprobe and return normalized media metadata.

    Returns None if ffprobe is unavailable or response is invalid.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if not result.stdout:
            return None
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    return {
        "video_codec": (video_stream or {}).get("codec_name"),
        "audio_codec": (audio_stream or {}).get("codec_name"),
        "duration": _to_float(fmt.get("duration")) or _to_float((video_stream or {}).get("duration")),
        "format_bitrate": _to_int(fmt.get("bit_rate")),
        "video_bitrate": _to_int((video_stream or {}).get("bit_rate")),
        "audio_bitrate": _to_int((audio_stream or {}).get("bit_rate")),
        "width": _to_int((video_stream or {}).get("width")),
        "height": _to_int((video_stream or {}).get("height")),
        "channels": _to_int((audio_stream or {}).get("channels")),
        "sample_rate": _to_int((audio_stream or {}).get("sample_rate")),
    }


@lru_cache(maxsize=2048)
def probe_video_codec(path: str) -> str | None:
    """Run ffprobe and return the raw codec_name for the first video stream.

    Returns None if ffprobe is not available or the file has no video stream.
    """
    info = probe_media_info(path)
    if not info:
        return None
    return info.get("video_codec")
