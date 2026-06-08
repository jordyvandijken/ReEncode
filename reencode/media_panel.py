import os
import subprocess
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, QPoint, QSettings, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from reencode import codec_probe
from reencode import presets as presets_data
from reencode import size_estimator
from reencode.subprocess_util import popen_hidden, run_hidden
from reencode.virtual_media_model import VirtualMediaTableModel, recommendation_color


def _bold_font(font: QFont) -> QFont:
    f = QFont(font)
    f.setBold(True)
    return f


def _human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.1f} MB"
    else:
        return f"{num_bytes / 1024 ** 3:.2f} GB"


def _parse_ffmpeg_timestamp(value: str) -> float | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None

    return (hours * 3600) + (minutes * 60) + seconds


def _ffmpeg_progress_seconds(progress_data: dict[str, str]) -> float | None:
    out_time = progress_data.get("out_time")
    if out_time:
        parsed = _parse_ffmpeg_timestamp(out_time)
        if parsed is not None:
            return parsed

    out_time_us = progress_data.get("out_time_us")
    if out_time_us:
        try:
            return int(out_time_us) / 1_000_000
        except ValueError:
            pass

    out_time_ms = progress_data.get("out_time_ms")
    if out_time_ms:
        try:
            return int(out_time_ms) / 1_000
        except ValueError:
            return None

    return None


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically (used for the raw-byte size column)."""

    def __init__(self, display: str, sort_value: float):
        super().__init__(display)
        self._sort_value = sort_value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _NumericItem):
            return self._sort_value < other._sort_value
        return super().__lt__(other)


BASE_COLUMNS = ["Name", "Size", "Codec", "Recommend", "Estimate", "Path", "Modified"]
SELECTABLE_COLUMNS = ["", "Name", "Size", "Codec", "Recommended", "Estimate", "Path", "Modified"]
IMAGE_BASE_COLUMNS = ["Name", "Size", "Type", "Recommend", "Estimate", "Path", "Modified"]
IMAGE_SELECTABLE_COLUMNS = ["", "Name", "Size", "Type", "Recommend", "Estimate", "Path", "Modified"]
CONVERTIBLE_MEDIA_TYPES = {"Videos", "Audio", "Images"}

COL_NAME, COL_SIZE, COL_CODEC, COL_REC, COL_ESTIMATE, COL_PATH, COL_MODIFIED = range(7)
VCOL_SELECT, VCOL_NAME, VCOL_SIZE, VCOL_CODEC, VCOL_REC, VCOL_ESTIMATE, VCOL_PATH, VCOL_MODIFIED = range(8)

# Colours for the Recommended cell
_COLOR_OPTIMAL  = QColor("#2e7d32")   # dark green
_COLOR_GOOD     = QColor("#1565c0")   # dark blue
_COLOR_REENCODE = QColor("#e65100")   # dark orange
_COLOR_PENDING  = QColor("#616161")   # neutral gray
_PAGE_SIZE_OPTIONS = (25, 50, 100, 250)
_FILTER_RECOMMEND_OPTIONS = (
    ("All", "all"),
    ("Re-encode", "reencode"),
    ("Keep", "keep"),
)
_GPU_SETTING_KEY = "conversion/use_gpu"

_VIDEO_CODEC_ENCODER_ARGS: dict[str, list[str]] = {
    "av1": ["-c:v", "libaom-av1", "-crf", "32", "-b:v", "0", "-cpu-used", "6"],
    "vp9": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0"],
    "h264": ["-c:v", "libx264", "-crf", "23", "-preset", "medium"],
    "hevc": ["-c:v", "libx265", "-crf", "28", "-preset", "medium"],
    "ffv1": ["-c:v", "ffv1"],
}

_AUDIO_CODEC_ENCODER_ARGS: dict[str, list[str]] = {
    "aac": ["-c:a", "aac", "-b:a", "192k"],
    "opus": ["-c:a", "libopus", "-b:a", "128k"],
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
}

_IMAGE_CODEC_ENCODER_ARGS: dict[str, list[str]] = {
    "webp": ["-c:v", "libwebp", "-quality", "80"],
    "jpeg": ["-c:v", "mjpeg", "-q:v", "3"],
    "jxl": ["-c:v", "libjxl", "-distance", "1.0"],
}

_IMAGE_EXTENSION_TO_CODEC: dict[str, str] = {
    ".webp": "webp",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".jxl": "jxl",
    ".png": "png",
}

_AUDIO_CODEC_TO_EXTENSION: dict[str, str] = {
    "aac": ".m4a",
    "opus": ".opus",
    "flac": ".flac",
    "mp3": ".mp3",
}

_IMAGE_CODEC_TO_EXTENSION: dict[str, str] = {
    "webp": ".webp",
    "jpeg": ".jpg",
    "jxl": ".jxl",
    "png": ".png",
}


def _normalize_codec_name(value: str | None) -> str:
    if not value:
        return ""
    label = value.strip().lower()
    if "original" in label:
        return "original"
    if "av1" in label:
        return "av1"
    if "vp9" in label:
        return "vp9"
    if "h.264" in label or "avc" in label or label == "h264":
        return "h264"
    if "h.265" in label or "hevc" in label or label == "h265":
        return "hevc"
    if "ffv1" in label:
        return "ffv1"
    if "aac" in label:
        return "aac"
    if "opus" in label:
        return "opus"
    if "flac" in label:
        return "flac"
    if "mp3" in label:
        return "mp3"
    if "jpeg xl" in label or label == "jxl":
        return "jxl"
    if "webp" in label:
        return "webp"
    if label in {"jpeg", "jpg", "mjpeg"}:
        return "jpeg"
    return label


def _image_extension_for_recommendation(recommended_label: str, source_path: str) -> str:
    codec_name = _normalize_codec_name(recommended_label)
    if codec_name == "original":
        return Path(source_path).suffix.lower() or ".webp"
    return _IMAGE_CODEC_TO_EXTENSION.get(codec_name, ".webp")


def _columns_for_media_type(media_type: str, supports_conversion: bool) -> list[str]:
    if media_type == "Images":
        return IMAGE_SELECTABLE_COLUMNS if supports_conversion else IMAGE_BASE_COLUMNS
    return SELECTABLE_COLUMNS if supports_conversion else BASE_COLUMNS


def _estimate_change_pct(size_bytes: int, estimate_sort: float) -> float | None:
    if size_bytes <= 0 or estimate_sort < 0:
        return None
    return ((float(size_bytes) - float(estimate_sort)) / float(size_bytes)) * 100.0


def _parse_optional_float(value: str) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class _FilterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Table filters")
        self.setModal(False)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_path_edit = QLineEdit()
        self.name_path_edit.setPlaceholderText("Contains text in name or path")
        form.addRow("Name/path", self.name_path_edit)

        self.codec_edit = QLineEdit()
        self.codec_edit.setPlaceholderText("Contains codec text")
        form.addRow("Codec", self.codec_edit)

        self.recommend_combo = QComboBox()
        for label, value in _FILTER_RECOMMEND_OPTIONS:
            self.recommend_combo.addItem(label, value)
        form.addRow("Recommendation", self.recommend_combo)

        self.min_size_mb_edit = QLineEdit()
        self.min_size_mb_edit.setPlaceholderText("Any")
        form.addRow("Min size (MB)", self.min_size_mb_edit)

        self.max_size_mb_edit = QLineEdit()
        self.max_size_mb_edit.setPlaceholderText("Any")
        form.addRow("Max size (MB)", self.max_size_mb_edit)

        self.min_change_pct_edit = QLineEdit()
        self.min_change_pct_edit.setPlaceholderText("Any")
        form.addRow("Min change (%)", self.min_change_pct_edit)

        self.max_change_pct_edit = QLineEdit()
        self.max_change_pct_edit.setPlaceholderText("Any")
        form.addRow("Max change (%)", self.max_change_pct_edit)

        layout.addLayout(form)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Reset | QDialogButtonBox.StandardButton.Close)
        self._reset_button = button_box.button(QDialogButtonBox.StandardButton.Reset)
        button_box.rejected.connect(self.close)
        layout.addWidget(button_box)

    def as_filter_state(self) -> dict:
        return {
            "name_path": self.name_path_edit.text(),
            "codec": self.codec_edit.text(),
            "recommendation": self.recommend_combo.currentData() or "all",
            "min_size_mb": _parse_optional_float(self.min_size_mb_edit.text()),
            "max_size_mb": _parse_optional_float(self.max_size_mb_edit.text()),
            "min_change_pct": _parse_optional_float(self.min_change_pct_edit.text()),
            "max_change_pct": _parse_optional_float(self.max_change_pct_edit.text()),
        }

    def clear_fields(self):
        self.name_path_edit.clear()
        self.codec_edit.clear()
        self.recommend_combo.setCurrentIndex(0)
        self.min_size_mb_edit.clear()
        self.max_size_mb_edit.clear()
        self.min_change_pct_edit.clear()
        self.max_change_pct_edit.clear()


def _recommended_ffmpeg_args(recommended_label: str, source_codec: str | None = None) -> list[str]:
    codec_name = _normalize_codec_name(recommended_label)
    if codec_name == "original":
        codec_name = _normalize_codec_name(source_codec)
    return _VIDEO_CODEC_ENCODER_ARGS.get(codec_name, _VIDEO_CODEC_ENCODER_ARGS["hevc"])


@lru_cache(maxsize=1)
def _available_ffmpeg_encoders() -> set[str]:
    try:
        result = run_hidden(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return set()

    if result.returncode != 0:
        return set()

    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("------"):
            continue
        parts = text.split()
        if len(parts) < 2:
            continue
        if parts[0].startswith("V"):
            encoders.add(parts[1].lower())

    return encoders


def _gpu_encoder_candidates(codec_name: str) -> list[str]:
    if codec_name == "h264":
        return ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"]
    if codec_name == "hevc":
        return ["hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_videotoolbox"]
    if codec_name == "av1":
        return ["av1_nvenc", "av1_qsv", "av1_amf"]
    return []


def _recommended_video_ffmpeg_args(
    recommended_label: str,
    source_codec: str | None = None,
    use_gpu: bool = False,
) -> list[str]:
    software_args = _recommended_ffmpeg_args(recommended_label, source_codec=source_codec)
    if not use_gpu:
        return software_args

    codec_name = _normalize_codec_name(recommended_label)
    if codec_name == "original":
        codec_name = _normalize_codec_name(source_codec)

    if codec_name not in {"h264", "hevc", "av1"}:
        return software_args

    available_encoders = _available_ffmpeg_encoders()
    if not available_encoders:
        return software_args

    for encoder in _gpu_encoder_candidates(codec_name):
        if encoder in available_encoders:
            return ["-c:v", encoder]

    return software_args


def _recommended_audio_ffmpeg_args(recommended_label: str, source_codec: str | None = None) -> list[str]:
    codec_name = _normalize_codec_name(recommended_label)
    if codec_name == "original":
        codec_name = _normalize_codec_name(source_codec)
    return _AUDIO_CODEC_ENCODER_ARGS.get(codec_name, _AUDIO_CODEC_ENCODER_ARGS["aac"])


def _recommended_image_ffmpeg_args(recommended_label: str, source_path: str) -> list[str]:
    codec_name = _normalize_codec_name(recommended_label)
    if codec_name == "original":
        codec_name = _IMAGE_EXTENSION_TO_CODEC.get(Path(source_path).suffix.lower(), "webp")
    return _IMAGE_CODEC_ENCODER_ARGS.get(codec_name, _IMAGE_CODEC_ENCODER_ARGS["webp"])


def _recommended_video_output_path(path: str, recommended_label: str) -> str:
    source = Path(path)
    suffix = source.suffix.lower()
    output_suffix = suffix if suffix in {".mkv", ".webm"} else ".mkv"
    safe_label = recommended_label.lower().replace("/", "-").replace(" ", "-")
    candidate = source.with_name(f"{source.stem}.{safe_label}.reencoded{output_suffix}")

    if not candidate.exists():
        return str(candidate)

    index = 2
    while True:
        next_candidate = source.with_name(f"{source.stem}.{safe_label}.reencoded-{index}{output_suffix}")
        if not next_candidate.exists():
            return str(next_candidate)
        index += 1


def _recommended_audio_output_path(path: str, recommended_label: str) -> str:
    source = Path(path)
    codec_name = _normalize_codec_name(recommended_label)
    output_suffix = _AUDIO_CODEC_TO_EXTENSION.get(codec_name, ".m4a")
    candidate = source.with_name(f"{source.stem}.reencoded{output_suffix}")

    if not candidate.exists():
        return str(candidate)

    index = 2
    while True:
        next_candidate = source.with_name(f"{source.stem}.reencoded-{index}{output_suffix}")
        if not next_candidate.exists():
            return str(next_candidate)
        index += 1


def _recommended_image_output_path(path: str, recommended_label: str) -> str:
    source = Path(path)
    codec_name = _normalize_codec_name(recommended_label)
    if codec_name == "original":
        codec_name = _IMAGE_EXTENSION_TO_CODEC.get(source.suffix.lower(), "webp")
    output_suffix = _IMAGE_CODEC_TO_EXTENSION.get(codec_name, ".webp")
    candidate = source.with_name(f"{source.stem}.reencoded{output_suffix}")

    if not candidate.exists():
        return str(candidate)

    index = 2
    while True:
        next_candidate = source.with_name(f"{source.stem}.reencoded-{index}{output_suffix}")
        if not next_candidate.exists():
            return str(next_candidate)
        index += 1


def _recommended_output_path(media_type: str, path: str, recommended_label: str) -> str:
    if media_type == "Videos":
        return _recommended_video_output_path(path, recommended_label)
    if media_type == "Audio":
        return _recommended_audio_output_path(path, recommended_label)
    return _recommended_image_output_path(path, recommended_label)


def _temporary_output_path(final_output_path: str) -> str:
    final_output = Path(final_output_path)
    suffix = final_output.suffix or ".mkv"
    token = uuid4().hex[:8]
    temp_name = f"{final_output.stem}.reencode-temp-{token}{suffix}"
    return str(final_output.with_name(temp_name))


class _ConvertOptionsDialog(QDialog):
    def __init__(self, media_type: str, default_do_not_replace: bool, default_use_gpu: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert selected")

        layout = QVBoxLayout(self)

        self._do_not_replace = QCheckBox("Do not replace original")
        self._do_not_replace.setChecked(default_do_not_replace)
        if media_type == "Images":
            self._do_not_replace.setChecked(True)
            self._do_not_replace.setEnabled(False)
            self._do_not_replace.setToolTip("Images are always written as new files.")
        else:
            self._do_not_replace.setToolTip("When checked, conversion creates a separate .reencoded output file.")
        layout.addWidget(self._do_not_replace)

        self._use_gpu = QCheckBox("Use GPU")
        self._use_gpu.setChecked(default_use_gpu)
        self._use_gpu.setToolTip("Auto-detect available hardware video encoders and fall back to CPU if unavailable.")
        layout.addWidget(self._use_gpu)

        helper = QLabel("GPU mode applies to video only and automatically falls back to software encoding when needed.")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def do_not_replace(self) -> bool:
        return self._do_not_replace.isChecked()

    def use_gpu(self) -> bool:
        return self._use_gpu.isChecked()


class _ConversionThread(QThread):
    progress = Signal(str, int, int, object)
    finished = Signal(bool, str)

    def __init__(self, jobs: list[tuple[str, str, str, str]], replace_originals: bool, use_gpu: bool, parent=None):
        super().__init__(parent)
        self._jobs = jobs
        self._replace_originals = replace_originals
        self._use_gpu = use_gpu

    def _job_conversion_inputs(self, media_type: str, source_path: str, recommended_label: str) -> tuple[list[str], float | None]:
        probe_info = codec_probe.probe_media_info(source_path) or {}
        source_video_codec = str(probe_info.get("video_codec") or "")
        source_audio_codec = str(probe_info.get("audio_codec") or "")

        if media_type == "Videos":
            codec_name = source_video_codec.lower()
            if not recommended_label:
                _status, recommended_label, _reason = codec_probe.recommendation(codec_name)
            ffmpeg_args = _recommended_video_ffmpeg_args(
                recommended_label,
                source_codec=source_video_codec,
                use_gpu=self._use_gpu,
            )
            duration_seconds = probe_info.get("duration")
        elif media_type == "Audio":
            ffmpeg_args = _recommended_audio_ffmpeg_args(recommended_label, source_codec=source_audio_codec)
            duration_seconds = probe_info.get("duration")
        else:
            ffmpeg_args = _recommended_image_ffmpeg_args(recommended_label, source_path)
            duration_seconds = None

        if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
            duration_seconds = None

        return ffmpeg_args, duration_seconds

    def run(self):
        total_jobs = len(self._jobs)

        for job_index, (media_type, source_path, final_output_path, recommended_label) in enumerate(self._jobs, start=1):
            temp_output_path = _temporary_output_path(final_output_path)
            ffmpeg_args, duration_seconds = self._job_conversion_inputs(media_type, source_path, recommended_label)

            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-nostats",
                "-i",
                source_path,
            ]

            if media_type == "Videos":
                command.extend([
                    "-map",
                    "0",
                    *ffmpeg_args,
                    "-c:a",
                    "copy",
                    "-c:s",
                    "copy",
                    temp_output_path,
                ])
            elif media_type == "Audio":
                command.extend([
                    "-vn",
                    *ffmpeg_args,
                    temp_output_path,
                ])
            else:
                command.extend([
                    "-frames:v",
                    "1",
                    *ffmpeg_args,
                    temp_output_path,
                ])

            try:
                process = popen_hidden(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except FileNotFoundError:
                self.finished.emit(False, "ffmpeg was not found in PATH.")
                return
            except OSError as exc:
                self.finished.emit(False, str(exc))
                return

            self.progress.emit(source_path, job_index - 1, total_jobs, 0 if duration_seconds else None)

            progress_data: dict[str, str] = {}
            last_emitted_percent: int | None = 0 if duration_seconds else None

            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                progress_data[key] = value

                if key != "progress" or not duration_seconds:
                    continue

                current_seconds = _ffmpeg_progress_seconds(progress_data)
                if current_seconds is None:
                    continue

                percent = max(0, min(100, int((current_seconds / duration_seconds) * 100)))
                if percent != last_emitted_percent:
                    self.progress.emit(source_path, job_index - 1, total_jobs, percent)
                    last_emitted_percent = percent

            stderr_output = ""
            if process.stderr is not None:
                stderr_output = process.stderr.read().strip()

            return_code = process.wait()

            if return_code != 0:
                if os.path.exists(temp_output_path):
                    try:
                        os.remove(temp_output_path)
                    except OSError:
                        pass
                details = stderr_output or f"ffmpeg failed for {os.path.basename(source_path)}"
                self.finished.emit(False, details)
                return

            if duration_seconds and last_emitted_percent != 100:
                self.progress.emit(source_path, job_index - 1, total_jobs, 100)

            if not os.path.exists(temp_output_path):
                self.finished.emit(False, f"Conversion output was not produced for {os.path.basename(source_path)}")
                return

            try:
                os.replace(temp_output_path, final_output_path)
            except OSError as exc:
                self.finished.emit(False, f"Failed to finalize {os.path.basename(source_path)}: {exc}")
                return

        if self._replace_originals:
            self.finished.emit(True, f"Replaced {total_jobs} original file(s).")
        else:
            self.finished.emit(True, f"Created {total_jobs} converted file(s).")


class MediaPanel(QWidget):
    """A table that lists media files of one type."""

    conversion_status_changed = Signal(str, bool)

    def __init__(self, media_type: str, parent=None):
        super().__init__(parent)
        self._media_type = media_type
        self._is_video = media_type == "Videos"
        self._use_virtual_table = (
            not self._is_video
            and os.getenv("REENCODE_VIRTUAL_AUDIO_TABLE", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._supports_conversion = media_type in CONVERTIBLE_MEDIA_TYPES and not self._use_virtual_table
        self._conversion_thread: _ConversionThread | None = None
        self._probe_updates_active = False
        self._scan_locked = False
        self._default_selection_mode: QAbstractItemView.SelectionMode | None = None
        self._path_rows: dict[str, int] = {}
        self._path_rows_dirty = False
        self._virtual_model: VirtualMediaTableModel | None = None
        self._suspend_check_updates = False
        self._settings = QSettings()
        self._pagination_settings_prefix = f"pagination/{self._media_type.lower()}"
        self._pagination_page_size = self._load_page_size_setting()
        self._pagination_page = self._load_page_setting()
        self._pagination_restoring = False
        self._pagination_last_state: tuple[int, int, int] | None = None
        self._probe_by_path: dict[str, dict | None] = {}
        self._presets_by_id = presets_data.presets_by_id(presets_data.load_presets())
        self._active_preset_id: str | None = None
        self._filter_dialog: _FilterDialog | None = None
        self._filter_state: dict = {}
        self._filtered_widget_rows: list[int] = []
        self._setup_ui()

    def set_active_preset(self, preset_id: str | None):
        self._active_preset_id = preset_id
        self._refresh_recommendations_for_active_preset()

    def _active_preset_entry(self) -> presets_data.PresetMediaEntry | None:
        preset = self._presets_by_id.get(self._active_preset_id or "")
        return presets_data.media_entry_for_type(preset, self._media_type)

    def _preset_reason(self, entry: presets_data.PresetMediaEntry | None, default_text: str) -> str:
        if entry is None:
            return default_text

        parts: list[str] = []
        if entry.mode:
            parts.append(entry.mode)
        if entry.info:
            parts.append(entry.info)
        return " ".join(parts) if parts else default_text

    def _refresh_recommendations_for_active_preset(self):
        if self.file_count() == 0:
            return

        if self._use_virtual_table:
            assert self._virtual_model is not None
            row_updates: dict[str, dict] = {}
            for row_data in self._virtual_model._rows:
                path = str(row_data.get("path") or "")
                if not path:
                    continue
                probe_info = self._probe_by_path.get(path)
                codec_text, rec_label, reason, status = self._base_codec_recommend_values(path, probe_info)
                size_bytes = int(row_data.get("size_bytes") or 0)
                estimate_text, estimate_sort, estimate_tip = self._audio_estimate_values(
                    path,
                    size_bytes,
                    probe_info,
                    recommended_label=rec_label,
                )
                estimate_change_pct = _estimate_change_pct(size_bytes, float(estimate_sort)) if estimate_sort >= 0 else None
                row_updates[path] = {
                    "codec": codec_text,
                    "recommend": rec_label,
                    "rec_status": status,
                    "rec_reason": reason,
                    "rec_color": recommendation_color(status),
                    "estimate_text": estimate_text,
                    "estimate_sort": estimate_sort,
                    "estimate_change_pct": estimate_change_pct,
                    "estimate_tip": estimate_tip,
                }
            self._virtual_model.update_rows(row_updates)
            return

        self._table.setSortingEnabled(False)
        self._table.setUpdatesEnabled(False)
        try:
            path_col = self._path_column()
            for row in range(self._table.rowCount()):
                path_item = self._table.item(row, path_col)
                if path_item is None:
                    continue
                path = path_item.text()
                self._apply_probe_update(path, self._probe_by_path.get(path))
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.setSortingEnabled(True)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        top_bar = QHBoxLayout()
        self._label = QLabel("No files found yet.")
        top_bar.addWidget(self._label)
        top_bar.addStretch(1)

        self._page_size_label = QLabel("Items:")
        top_bar.addWidget(self._page_size_label)

        self._page_size_combo = QComboBox()
        for size in _PAGE_SIZE_OPTIONS:
            self._page_size_combo.addItem(str(size), size)
        size_index = self._page_size_combo.findData(self._pagination_page_size)
        if size_index < 0:
            size_index = self._page_size_combo.findData(_PAGE_SIZE_OPTIONS[1])
            self._pagination_page_size = int(_PAGE_SIZE_OPTIONS[1])
        self._page_size_combo.setCurrentIndex(size_index)
        self._page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        top_bar.addWidget(self._page_size_combo)

        self._prev_page_button = QPushButton("Prev")
        self._prev_page_button.clicked.connect(self._on_prev_page)
        top_bar.addWidget(self._prev_page_button)

        self._next_page_button = QPushButton("Next")
        self._next_page_button.clicked.connect(self._on_next_page)
        top_bar.addWidget(self._next_page_button)

        self._page_label = QLabel("Page 0 of 0")
        top_bar.addWidget(self._page_label)

        self._filter_button = QPushButton("Filters...")
        self._filter_button.clicked.connect(self._show_filter_dialog)
        top_bar.addWidget(self._filter_button)

        if self._supports_conversion:
            self._select_all = QCheckBox("Select all")
            self._select_all.setTristate(True)
            self._select_all.stateChanged.connect(self._on_select_all_changed)
            top_bar.addWidget(self._select_all)

            self._convert_button = QPushButton("Convert selected")
            self._convert_button.setEnabled(False)
            self._convert_button.clicked.connect(self._convert_selected)
            top_bar.addWidget(self._convert_button)

        layout.addLayout(top_bar)

        columns = _columns_for_media_type(self._media_type, self._supports_conversion)

        if self._use_virtual_table:
            self._virtual_model = VirtualMediaTableModel(columns, parent=self)
            self._table = QTableView()
            self._table.setModel(self._virtual_model)
        else:
            self._table = QTableWidget(0, len(columns))
            self._table.setHorizontalHeaderLabels(columns)

        if self._supports_conversion:
            self._table.horizontalHeader().setSectionResizeMode(VCOL_SELECT, QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_NAME, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_SIZE, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_CODEC, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_REC, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_ESTIMATE, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_PATH, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_MODIFIED, QHeaderView.ResizeMode.Stretch)
        else:
            self._table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_SIZE, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_CODEC, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_REC, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_ESTIMATE, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_PATH, QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_MODIFIED, QHeaderView.ResizeMode.Stretch)

        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.verticalHeader().setVisible(False)
        self._default_selection_mode = self._table.selectionMode()
        if not self._use_virtual_table:
            self._table.horizontalHeader().sortIndicatorChanged.connect(self._on_table_sort_changed)
        # Keep path data in the model for internal lookups, but hide it from the UI.
        self._table.setColumnHidden(self._path_column(), True)
        if self._supports_conversion and not self._use_virtual_table:
            self._table.itemChanged.connect(self._on_table_item_changed)
        layout.addWidget(self._table)

        self._apply_pagination(save_settings=False)
        if self._supports_conversion:
            self._refresh_selection_controls()

    def _on_select_all_changed(self, state: int):
        if not self._supports_conversion or self._suspend_check_updates:
            return

        if state == Qt.CheckState.PartiallyChecked.value:
            return

        checked = state == Qt.CheckState.Checked.value
        self._suspend_check_updates = True
        try:
            for row in range(self._table.rowCount()):
                item = self._table.item(row, VCOL_SELECT)
                if item is not None:
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        finally:
            self._suspend_check_updates = False

        self._refresh_selection_controls()

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if not self._supports_conversion or self._suspend_check_updates:
            return

        if self._table.indexFromItem(item).column() != VCOL_SELECT:
            return

        self._refresh_selection_controls()

    def _refresh_selection_controls(self):
        if not self._supports_conversion:
            return

        total = self._table.rowCount()
        selected = self._selected_row_count()

        self._suspend_check_updates = True
        try:
            if total == 0:
                self._select_all.setCheckState(Qt.CheckState.Unchecked)
                self._select_all.setEnabled(False)
            elif selected == 0:
                self._select_all.setCheckState(Qt.CheckState.Unchecked)
                self._select_all.setEnabled(True)
            elif selected == total:
                self._select_all.setCheckState(Qt.CheckState.Checked)
                self._select_all.setEnabled(True)
            else:
                self._select_all.setCheckState(Qt.CheckState.PartiallyChecked)
                self._select_all.setEnabled(True)
        finally:
            self._suspend_check_updates = False

        enabled = not self._scan_locked and self._conversion_thread is None
        self._convert_button.setEnabled(selected > 0 and enabled)
        self._select_all.setEnabled(total > 0 and enabled)
        self._refresh_pagination_controls()
        self._update_label()

    def _load_page_size_setting(self) -> int:
        raw = self._settings.value(f"{self._pagination_settings_prefix}/page_size", _PAGE_SIZE_OPTIONS[1])
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return int(_PAGE_SIZE_OPTIONS[1])
        if parsed not in _PAGE_SIZE_OPTIONS:
            return int(_PAGE_SIZE_OPTIONS[1])
        return parsed

    def _load_page_setting(self) -> int:
        raw = self._settings.value(f"{self._pagination_settings_prefix}/page", 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _save_pagination_settings(self):
        self._settings.setValue(f"{self._pagination_settings_prefix}/page_size", int(self._pagination_page_size))
        self._settings.setValue(f"{self._pagination_settings_prefix}/page", int(self._pagination_page))

    def _total_pages(self) -> int:
        count = self._visible_file_count()
        if count <= 0:
            return 0
        return (count + self._pagination_page_size - 1) // self._pagination_page_size

    def _clamp_page(self):
        total_pages = self._total_pages()
        if total_pages <= 0:
            self._pagination_page = 0
            return
        self._pagination_page = max(0, min(self._pagination_page, total_pages - 1))

    def _apply_pagination(self, *, save_settings: bool = True):
        self._clamp_page()

        if self._use_virtual_table:
            assert self._virtual_model is not None
            self._virtual_model.set_pagination(self._pagination_page, self._pagination_page_size)
        else:
            self._rebuild_filtered_widget_rows()
            start = self._pagination_page * self._pagination_page_size
            end = start + self._pagination_page_size
            row_count = self._table.rowCount()
            visible_rows = set(self._filtered_widget_rows[start:end])
            for row in range(row_count):
                self._table.setRowHidden(row, row not in visible_rows)
            self._pagination_last_state = (start, end, len(self._filtered_widget_rows))

        self._refresh_pagination_controls()
        if save_settings and not self._pagination_restoring:
            self._save_pagination_settings()

    def _refresh_pagination_controls(self):
        total_pages = self._total_pages()
        if total_pages <= 0:
            self._page_label.setText("Page 0 of 0")
            self._prev_page_button.setEnabled(False)
            self._next_page_button.setEnabled(False)
            return

        self._page_label.setText(f"Page {self._pagination_page + 1} of {total_pages}")
        if self._scan_locked:
            self._prev_page_button.setEnabled(False)
            self._next_page_button.setEnabled(False)
            return
        self._prev_page_button.setEnabled(self._pagination_page > 0)
        self._next_page_button.setEnabled(self._pagination_page + 1 < total_pages)

    def _on_page_size_changed(self, _index: int):
        data = self._page_size_combo.currentData()
        try:
            page_size = int(data)
        except (TypeError, ValueError):
            return

        if page_size <= 0:
            return

        if page_size != self._pagination_page_size:
            self._pagination_page_size = page_size
            self._pagination_page = 0
            self._apply_pagination(save_settings=True)

    def _on_prev_page(self):
        if self._pagination_page <= 0:
            return
        self._pagination_page -= 1
        self._apply_pagination(save_settings=True)

    def _on_next_page(self):
        if self._pagination_page + 1 >= self._total_pages():
            return
        self._pagination_page += 1
        self._apply_pagination(save_settings=True)

    def _selected_row_count(self) -> int:
        selected = 0
        for row in range(self._table.rowCount()):
            item = self._table.item(row, VCOL_SELECT)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected += 1
        return selected

    def _selected_jobs(self, replace_originals: bool) -> list[tuple[str, str, str, str]]:
        jobs: list[tuple[str, str, str, str]] = []
        for row in range(self._table.rowCount()):
            select_item = self._table.item(row, VCOL_SELECT)
            if select_item is None or select_item.checkState() != Qt.CheckState.Checked:
                continue

            path_item = self._table.item(row, VCOL_PATH)
            rec_item = self._table.item(row, VCOL_REC)
            if path_item is None or rec_item is None:
                continue

            source_path = path_item.text()
            if replace_originals and self._media_type != "Images":
                final_output_path = source_path
            else:
                final_output_path = _recommended_output_path(self._media_type, source_path, rec_item.text())

            jobs.append((self._media_type, source_path, final_output_path, rec_item.text()))

        return jobs

    def _path_column(self) -> int:
        return VCOL_PATH if self._supports_conversion else COL_PATH

    def _invalidate_path_rows(self, *_args):
        self._path_rows_dirty = True

    def _on_table_sort_changed(self, *_args):
        self._invalidate_path_rows()
        self._apply_pagination(save_settings=False)

    def _visible_file_count(self) -> int:
        if self._use_virtual_table:
            assert self._virtual_model is not None
            return self._virtual_model.filtered_row_count()
        self._rebuild_filtered_widget_rows()
        return len(self._filtered_widget_rows)

    def total_file_count(self) -> int:
        if self._use_virtual_table:
            assert self._virtual_model is not None
            return self._virtual_model.total_row_count()
        return self._table.rowCount()

    def _normalize_filter_state(self, filter_state: dict) -> dict:
        normalized: dict = {}
        name_path = str(filter_state.get("name_path") or "").strip().casefold()
        if name_path:
            normalized["name_path"] = name_path

        codec = str(filter_state.get("codec") or "").strip().casefold()
        if codec:
            normalized["codec"] = codec

        recommendation = str(filter_state.get("recommendation") or "all").strip().lower()
        if recommendation in {"reencode", "keep"}:
            normalized["recommendation"] = recommendation

        min_size_mb = filter_state.get("min_size_mb")
        if isinstance(min_size_mb, (int, float)):
            normalized["min_size_mb"] = max(0.0, float(min_size_mb))

        max_size_mb = filter_state.get("max_size_mb")
        if isinstance(max_size_mb, (int, float)):
            normalized["max_size_mb"] = max(0.0, float(max_size_mb))

        min_change_pct = filter_state.get("min_change_pct")
        if isinstance(min_change_pct, (int, float)):
            normalized["min_change_pct"] = float(min_change_pct)

        max_change_pct = filter_state.get("max_change_pct")
        if isinstance(max_change_pct, (int, float)):
            normalized["max_change_pct"] = float(max_change_pct)

        return normalized

    def _show_filter_dialog(self):
        if self._filter_dialog is None:
            dialog = _FilterDialog(self)
            dialog.name_path_edit.textChanged.connect(self._on_filter_ui_changed)
            dialog.codec_edit.textChanged.connect(self._on_filter_ui_changed)
            dialog.recommend_combo.currentIndexChanged.connect(self._on_filter_ui_changed)
            dialog.min_size_mb_edit.textChanged.connect(self._on_filter_ui_changed)
            dialog.max_size_mb_edit.textChanged.connect(self._on_filter_ui_changed)
            dialog.min_change_pct_edit.textChanged.connect(self._on_filter_ui_changed)
            dialog.max_change_pct_edit.textChanged.connect(self._on_filter_ui_changed)
            dialog._reset_button.clicked.connect(self._on_filter_reset_clicked)
            self._filter_dialog = dialog

        self._filter_dialog.show()
        self._filter_dialog.raise_()
        self._filter_dialog.activateWindow()

    def _on_filter_reset_clicked(self):
        if self._filter_dialog is None:
            return
        self._filter_dialog.clear_fields()
        self._on_filter_ui_changed()

    def _on_filter_ui_changed(self):
        if self._filter_dialog is None:
            return
        self._set_filter_state(self._filter_dialog.as_filter_state())

    def _set_filter_state(self, filter_state: dict):
        normalized = self._normalize_filter_state(filter_state)
        if normalized == self._filter_state:
            return

        self._filter_state = normalized
        self._pagination_page = 0

        if self._use_virtual_table:
            assert self._virtual_model is not None
            self._virtual_model.set_filter_state(self._filter_state)

        self._pagination_last_state = None
        self._apply_pagination(save_settings=False)
        self._update_label()

    def _rebuild_filtered_widget_rows(self):
        if self._use_virtual_table:
            return

        row_count = self._table.rowCount()
        if not self._filter_state:
            self._filtered_widget_rows = list(range(row_count))
            return

        matched_rows: list[int] = []
        for row in range(row_count):
            if self._widget_row_matches_filter(row):
                matched_rows.append(row)
        self._filtered_widget_rows = matched_rows

    def _widget_row_matches_filter(self, row: int) -> bool:
        name_col = VCOL_NAME if self._supports_conversion else COL_NAME
        codec_col = VCOL_CODEC if self._supports_conversion else COL_CODEC
        rec_col = VCOL_REC if self._supports_conversion else COL_REC
        size_col = VCOL_SIZE if self._supports_conversion else COL_SIZE
        path_col = self._path_column()
        estimate_col = VCOL_ESTIMATE if self._supports_conversion else COL_ESTIMATE

        name_item = self._table.item(row, name_col)
        path_item = self._table.item(row, path_col)
        codec_item = self._table.item(row, codec_col)
        rec_item = self._table.item(row, rec_col)
        size_item = self._table.item(row, size_col)
        estimate_item = self._table.item(row, estimate_col)

        name_text = name_item.text() if name_item is not None else ""
        path_text = path_item.text() if path_item is not None else ""
        codec_text = codec_item.text() if codec_item is not None else ""
        rec_status = str(rec_item.data(Qt.ItemDataRole.UserRole) or "").strip().lower() if rec_item is not None else ""
        size_bytes = int(size_item.data(Qt.ItemDataRole.UserRole) or 0) if size_item is not None else 0
        estimate_sort = float(estimate_item.data(Qt.ItemDataRole.UserRole) or -1) if estimate_item is not None else -1

        name_path_filter = self._filter_state.get("name_path")
        if name_path_filter:
            haystack = f"{name_text} {path_text}".casefold()
            if name_path_filter not in haystack:
                return False

        codec_filter = self._filter_state.get("codec")
        if codec_filter and codec_filter not in codec_text.casefold():
            return False

        recommendation_filter = self._filter_state.get("recommendation")
        if recommendation_filter == "reencode" and rec_status != "reencode":
            return False
        if recommendation_filter == "keep" and rec_status not in {"good", "optimal"}:
            return False

        min_size_mb = self._filter_state.get("min_size_mb")
        if min_size_mb is not None and size_bytes < int(float(min_size_mb) * 1024 * 1024):
            return False

        max_size_mb = self._filter_state.get("max_size_mb")
        if max_size_mb is not None and size_bytes > int(float(max_size_mb) * 1024 * 1024):
            return False

        min_change_pct = self._filter_state.get("min_change_pct")
        max_change_pct = self._filter_state.get("max_change_pct")
        if min_change_pct is not None or max_change_pct is not None:
            change_pct = _estimate_change_pct(size_bytes, estimate_sort)
            if change_pct is None:
                return False
            if min_change_pct is not None and change_pct < float(min_change_pct):
                return False
            if max_change_pct is not None and change_pct > float(max_change_pct):
                return False

        return True

    def _rebuild_path_rows(self):
        if self._use_virtual_table:
            return

        self._path_rows.clear()
        path_col = self._path_column()
        for row in range(self._table.rowCount()):
            path_item = self._table.item(row, path_col)
            if path_item is not None:
                self._path_rows[path_item.text()] = row
        self._path_rows_dirty = False

    def _row_for_path(self, path: str) -> int | None:
        if self._use_virtual_table:
            if self._virtual_model is None:
                return None
            return self._virtual_model.row_for_path(path)

        if self._path_rows_dirty:
            self._rebuild_path_rows()

        row = self._path_rows.get(path)
        if row is None:
            return None

        path_item = self._table.item(row, self._path_column())
        if path_item is not None and path_item.text() == path:
            return row

        # A stale row index can occur after table sorting/layout changes.
        self._rebuild_path_rows()
        return self._path_rows.get(path)

    def _video_details_items(self, path: str, size_bytes: int, probe_info: dict | None):
        raw_codec = (probe_info or {}).get("video_codec")
        if raw_codec:
            codec_text = codec_probe.codec_label(raw_codec)
            rec_label, reason, status = self._recommended_values(path, str(raw_codec), codec_text)
        else:
            codec_text = "Probing..."
            rec_label, reason, status = self._recommended_values(path, None, codec_text)

        if probe_info is None:
            estimate_item = _NumericItem("Pending probe", -1)
            estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            estimate_item.setToolTip("Estimate appears after probe completes.")
            codec_item = QTableWidgetItem(codec_text)

            rec_item = QTableWidgetItem(rec_label)
            rec_item.setToolTip(reason)
            rec_item.setData(Qt.ItemDataRole.UserRole, status)
            color = {
                "optimal": _COLOR_OPTIMAL,
                "good": _COLOR_GOOD,
                "pending": _COLOR_PENDING,
            }.get(status, _COLOR_REENCODE)
            rec_item.setForeground(color)
            rec_item.setFont(_bold_font(rec_item.font()))

            for item in (codec_item, rec_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            return codec_item, rec_item, estimate_item

        estimate = size_estimator.estimate_output_details(
            size_bytes,
            self._media_type,
            path,
            self._probe_info_for_estimate(path, probe_info, rec_label),
        )
        if estimate.estimated_size is None:
            estimate_item = _NumericItem("—", -1)
            if estimate.reason:
                estimate_item.setToolTip(estimate.reason)
        else:
            estimate_text = size_estimator.format_estimate(
                _human_size(estimate.estimated_size),
                estimate.savings_ratio,
                low_confidence=estimate.confidence == "low",
            )
            estimate_item = _NumericItem(estimate_text, estimate.estimated_size)
            estimate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if estimate.reason and (estimate.fallback_used or estimate.clamped):
                estimate_item.setToolTip(estimate.reason)

        codec_item = QTableWidgetItem(codec_text)

        rec_item = QTableWidgetItem(rec_label)
        rec_item.setToolTip(reason)
        rec_item.setData(Qt.ItemDataRole.UserRole, status)
        color = {
            "optimal": _COLOR_OPTIMAL,
            "good": _COLOR_GOOD,
            "pending": _COLOR_PENDING,
        }.get(status, _COLOR_REENCODE)
        rec_item.setForeground(color)
        rec_item.setFont(_bold_font(rec_item.font()))

        for item in (codec_item, rec_item, estimate_item):
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        return codec_item, rec_item, estimate_item

    def _audio_estimate_item(self, path: str, size_bytes: int, probe_info: dict | None):
        estimate_text, estimate_sort, estimate_tip = self._audio_estimate_values(path, size_bytes, probe_info)
        estimate_item = _NumericItem(estimate_text, estimate_sort)
        estimate_item.setData(Qt.ItemDataRole.UserRole, estimate_sort)
        if estimate_text == "Pending probe":
            estimate_item.setToolTip("Estimate appears after probe completes.")
        elif estimate_sort >= 0:
            estimate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if estimate_tip:
                estimate_item.setToolTip(estimate_tip)
        elif estimate_tip:
            estimate_item.setToolTip(estimate_tip)

        estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return estimate_item

    def _audio_estimate_values(
        self,
        path: str,
        size_bytes: int,
        probe_info: dict | None,
        recommended_label: str | None = None,
    ) -> tuple[str, float, str | None]:
        if self._media_type.lower() == "audio" and probe_info is None:
            return "Pending probe", -1, "Estimate appears after probe completes."

        if recommended_label is None:
            _codec_text, recommended_label, _reason, _status = self._base_codec_recommend_values(path, probe_info)

        estimate_path = path
        if self._media_type == "Images":
            target_ext = _image_extension_for_recommendation(recommended_label, path)
            estimate_path = str(Path(path).with_suffix(target_ext))

        estimate = size_estimator.estimate_output_details(
            size_bytes,
            self._media_type,
            estimate_path,
            self._probe_info_for_estimate(path, probe_info, recommended_label),
        )
        if estimate.estimated_size is None:
            return "—", -1, estimate.reason
        else:
            estimate_text = size_estimator.format_estimate(
                _human_size(estimate.estimated_size),
                estimate.savings_ratio,
                low_confidence=estimate.confidence == "low",
            )
            tip = estimate.reason if (estimate.fallback_used or estimate.clamped) else None
            return estimate_text, estimate.estimated_size, tip

    def _base_codec_recommend_items(self, path: str, probe_info: dict | None):
        codec_text, rec_label, reason, status = self._base_codec_recommend_values(path, probe_info)

        codec_item = QTableWidgetItem(codec_text)
        rec_item = QTableWidgetItem(rec_label)
        rec_item.setToolTip(reason)
        rec_item.setData(Qt.ItemDataRole.UserRole, status)

        color = {
            "optimal": _COLOR_OPTIMAL,
            "good": _COLOR_GOOD,
            "pending": _COLOR_PENDING,
        }.get(status, _COLOR_REENCODE)
        rec_item.setForeground(color)
        rec_item.setFont(_bold_font(rec_item.font()))

        for item in (codec_item, rec_item):
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        return codec_item, rec_item

    def _base_codec_recommend_values(self, path: str, probe_info: dict | None) -> tuple[str, str, str, str]:
        if self._media_type == "Images":
            file_ext = os.path.splitext(path)[1].lower()
            if file_ext:
                rec_label, reason, status = self._recommended_values(path, file_ext, file_ext)
                return file_ext, rec_label, reason, status
            rec_label, reason, status = self._recommended_values(path, None, "Unknown")
            return "Unknown", rec_label, reason, status

        if self._media_type == "Audio":
            raw_codec = (probe_info or {}).get("audio_codec")
            if raw_codec:
                codec_text = codec_probe.codec_label(raw_codec)
                rec_label, reason, status = self._recommended_values(path, str(raw_codec), codec_text)
            elif probe_info is None:
                codec_text = "Probing..."
                rec_label, reason, status = self._recommended_values(path, None, codec_text)
            else:
                codec_text = "Unknown"
                rec_label, reason, status = self._recommended_values(path, None, codec_text)
        else:
            codec_text = "-"
            status, rec_label, reason = ("pending", "-", "Recommendations are only available for audio/video codecs.")
        return codec_text, rec_label, reason, status

    def _recommended_values(self, path: str, raw_codec: str | None, codec_text: str) -> tuple[str, str, str]:
        entry = self._active_preset_entry()
        if entry is None:
            if self._media_type == "Images":
                file_ext = os.path.splitext(path)[1].lower()
                if file_ext:
                    status = "optimal" if file_ext == ".webp" else "good"
                    return ".webp", "Images convert to .webp by default.", status
                return ".webp", "File extension could not be determined.", "pending"

            if raw_codec:
                status, rec_label, reason = codec_probe.recommendation(raw_codec)
                return rec_label, reason, status
            return "Pending probe", "Codec details will appear after the async probe phase.", "pending"

        target_codec = entry.codec
        target_norm = _normalize_codec_name(target_codec)
        source_norm = _normalize_codec_name(raw_codec)

        if target_norm == "original":
            if raw_codec:
                return codec_text, self._preset_reason(entry, "Uses the source codec."), "optimal"
            return "Original", self._preset_reason(entry, "Uses the source codec."), "pending"

        status = "reencode"
        if source_norm and source_norm == target_norm:
            status = "optimal"
        elif self._media_type == "Images" and raw_codec:
            status = "good"

        return target_codec, self._preset_reason(entry, f"Target codec: {target_codec}"), status

    def _virtual_row(self, path: str, size_bytes: int, modified_timestamp: str, probe_info: dict | None) -> dict:
        codec_text, rec_label, reason, status = self._base_codec_recommend_values(path, probe_info)
        estimate_text, estimate_sort, estimate_tip = self._audio_estimate_values(
            path,
            size_bytes,
            probe_info,
            recommended_label=rec_label,
        )
        estimate_change_pct = _estimate_change_pct(int(size_bytes), float(estimate_sort)) if estimate_sort >= 0 else None
        return {
            "name": os.path.basename(path),
            "size_bytes": int(size_bytes),
            "size_text": _human_size(size_bytes),
            "codec": codec_text,
            "recommend": rec_label,
            "rec_status": status,
            "rec_reason": reason,
            "rec_color": recommendation_color(status),
            "estimate_text": estimate_text,
            "estimate_sort": estimate_sort,
            "estimate_change_pct": estimate_change_pct,
            "estimate_tip": estimate_tip,
            "path": path,
            "modified": self._format_modified(modified_timestamp),
        }

    def _probe_info_for_estimate(
        self,
        path: str,
        probe_info: dict | None,
        recommended_label: str,
    ) -> dict | None:
        if self._media_type not in {"Videos", "Audio"}:
            return probe_info

        info = dict(probe_info or {})
        target_codec = _normalize_codec_name(recommended_label)
        if target_codec == "original" or not target_codec:
            return info if info else probe_info

        if self._media_type == "Videos":
            info["video_codec"] = target_codec
        else:
            info["audio_codec"] = target_codec

        return info

    def _convert_selected(self):
        if self._conversion_thread is not None:
            return

        selected_count = self._selected_row_count()
        if selected_count <= 0:
            QMessageBox.information(self, "Convert selected", "Select at least one file first.")
            return

        default_use_gpu = str(self._settings.value(_GPU_SETTING_KEY, "1")).strip().lower() not in {"0", "false", "no", "off"}
        options_dialog = _ConvertOptionsDialog(
            self._media_type,
            default_do_not_replace=self._media_type == "Images",
            default_use_gpu=default_use_gpu,
            parent=self,
        )
        if options_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        use_gpu = options_dialog.use_gpu()
        self._settings.setValue(_GPU_SETTING_KEY, use_gpu)

        replace_originals = not options_dialog.do_not_replace()
        if self._media_type == "Images":
            replace_originals = False

        jobs = self._selected_jobs(replace_originals)
        if not jobs:
            QMessageBox.information(self, "Convert selected", "Select at least one file first.")
            return

        self._suspend_check_updates = True
        try:
            self._convert_button.setEnabled(False)
            self._select_all.setEnabled(False)
        finally:
            self._suspend_check_updates = False

        self._conversion_thread = _ConversionThread(
            jobs,
            replace_originals=replace_originals,
            use_gpu=use_gpu,
            parent=self,
        )
        self._conversion_thread.progress.connect(self._on_conversion_progress)
        self._conversion_thread.finished.connect(self._on_conversion_finished)
        action = "Replacing originals" if replace_originals else "Creating converted copies"
        self._label.setText(f"{action}: {len(jobs)} file(s)...")
        self.conversion_status_changed.emit(f"{action}: {len(jobs)} file(s)...", True)
        self._conversion_thread.start()

    def _on_conversion_progress(self, source_path: str, completed_jobs: int, total_jobs: int, percent: object):
        current_job = min(total_jobs, completed_jobs + 1)
        status_text = f"Converting {current_job}/{total_jobs}: {os.path.basename(source_path)}"
        if isinstance(percent, int):
            status_text = f"{status_text} ({percent}%)"

        self._label.setText(status_text)
        self.conversion_status_changed.emit(status_text, True)

    def _on_conversion_finished(self, success: bool, message: str):
        if self._conversion_thread is not None:
            self._conversion_thread.deleteLater()
            self._conversion_thread = None

        self._update_label()
        self._refresh_selection_controls()
        self.conversion_status_changed.emit(message, False)

        if success:
            QMessageBox.information(self, "Convert selected", message)
        else:
            QMessageBox.warning(self, "Convert selected", message[:1000])

    def _insert_file_row(self, path: str, size_bytes: int = 0, modified_timestamp: str = ""):
        self._probe_by_path.setdefault(path, None)
        row = self._table.rowCount()
        self._table.insertRow(row)

        name = os.path.basename(path)
        modified = self._format_modified(modified_timestamp)

        name_item = QTableWidgetItem(name)
        name_item.setToolTip(path)

        size_item = _NumericItem(_human_size(size_bytes), size_bytes)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size_item.setData(Qt.ItemDataRole.UserRole, size_bytes)

        path_item = QTableWidgetItem(path)
        modified_item = QTableWidgetItem(modified)

        if self._is_video:
            codec_item, rec_item, estimate_item = self._video_details_items(path, size_bytes, None)

            select_item = QTableWidgetItem()
            select_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for item in (name_item, size_item, codec_item, rec_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self._table.setItem(row, VCOL_SELECT, select_item)
            self._table.setItem(row, VCOL_NAME, name_item)
            self._table.setItem(row, VCOL_SIZE, size_item)
            self._table.setItem(row, VCOL_CODEC, codec_item)
            self._table.setItem(row, VCOL_REC, rec_item)
            self._table.setItem(row, VCOL_ESTIMATE, estimate_item)
            self._table.setItem(row, VCOL_PATH, path_item)
            self._table.setItem(row, VCOL_MODIFIED, modified_item)
        elif self._supports_conversion:
            codec_item, rec_item = self._base_codec_recommend_items(path, None)
            estimate_item = self._audio_estimate_item(path, size_bytes, None)

            select_item = QTableWidgetItem()
            select_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for item in (name_item, size_item, codec_item, rec_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self._table.setItem(row, VCOL_SELECT, select_item)
            self._table.setItem(row, VCOL_NAME, name_item)
            self._table.setItem(row, VCOL_SIZE, size_item)
            self._table.setItem(row, VCOL_CODEC, codec_item)
            self._table.setItem(row, VCOL_REC, rec_item)
            self._table.setItem(row, VCOL_ESTIMATE, estimate_item)
            self._table.setItem(row, VCOL_PATH, path_item)
            self._table.setItem(row, VCOL_MODIFIED, modified_item)
        else:
            codec_item, rec_item = self._base_codec_recommend_items(path, None)
            estimate_item = self._audio_estimate_item(path, size_bytes, None)

            for item in (name_item, size_item, codec_item, rec_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self._table.setItem(row, COL_NAME, name_item)
            self._table.setItem(row, COL_SIZE, size_item)
            self._table.setItem(row, COL_CODEC, codec_item)
            self._table.setItem(row, COL_REC, rec_item)
            self._table.setItem(row, COL_ESTIMATE, estimate_item)
            self._table.setItem(row, COL_PATH, path_item)
            self._table.setItem(row, COL_MODIFIED, modified_item)

        self._path_rows[path] = row
        self._path_rows_dirty = False

    def _format_modified(self, modified_timestamp: str) -> str:
        if not modified_timestamp:
            return "—"

        try:
            return datetime.fromtimestamp(int(modified_timestamp)).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, ValueError):
            return "—"

    def add_file(self, path: str, size_bytes: int = 0, modified_timestamp: str = ""):
        if self._use_virtual_table:
            self.add_files([(path, size_bytes, modified_timestamp)])
            return

        # Disable sorting while inserting to avoid row-index shifting
        self._table.setSortingEnabled(False)
        self._suspend_check_updates = True

        self._insert_file_row(path, size_bytes, modified_timestamp)

        self._suspend_check_updates = False
        self._table.setSortingEnabled(True)
        self._apply_pagination(save_settings=False)
        self._update_label()
        if self._supports_conversion:
            self._refresh_selection_controls()

    def add_files(self, rows: list[tuple[str, int, str]]):
        if not rows:
            return

        if self._use_virtual_table:
            assert self._virtual_model is not None
            self._table.setSortingEnabled(False)
            self._virtual_model.append_rows(
                [self._virtual_row(path, size_bytes, modified_timestamp, None) for path, size_bytes, modified_timestamp in rows]
            )
            self._table.setSortingEnabled(True)
            self._apply_pagination(save_settings=False)
            self._update_label()
            return

        self._table.setSortingEnabled(False)
        self._suspend_check_updates = True
        for path, size_bytes, modified_timestamp in rows:
            self._insert_file_row(path, size_bytes, modified_timestamp)

        self._suspend_check_updates = False
        self._table.setSortingEnabled(True)
        self._apply_pagination(save_settings=False)
        self._update_label()
        if self._supports_conversion:
            self._refresh_selection_controls()

    def update_probe(self, path: str, probe_info: dict | None):
        self.update_probes([(path, probe_info)])

    def update_file_stats(self, rows: list[tuple]):
        if not rows:
            return

        def _unpack_row(row: tuple) -> tuple[str, int, str, int | None]:
            if len(row) >= 4:
                path, size_bytes, modified_timestamp, estimate_bytes = row[:4]
                return str(path), int(size_bytes), str(modified_timestamp), estimate_bytes
            path, size_bytes, modified_timestamp = row[:3]
            return str(path), int(size_bytes), str(modified_timestamp), None

        if self._use_virtual_table:
            assert self._virtual_model is not None
            restore_sorting = False
            if not self._probe_updates_active:
                self._table.setSortingEnabled(False)
                restore_sorting = True

            row_updates: dict[str, dict] = {}
            for raw_row in rows:
                path, size_bytes, modified_timestamp, estimate_bytes = _unpack_row(raw_row)
                if estimate_bytes is not None and estimate_bytes >= 0:
                    savings_ratio = (1.0 - (estimate_bytes / size_bytes)) if size_bytes > 0 else None
                    estimate_text = size_estimator.format_estimate(_human_size(estimate_bytes), savings_ratio)
                    estimate_sort = estimate_bytes
                    estimate_tip = None
                else:
                    estimate_text, estimate_sort, estimate_tip = self._audio_estimate_values(path, size_bytes, None)
                estimate_change_pct = _estimate_change_pct(size_bytes, float(estimate_sort)) if estimate_sort >= 0 else None
                row_updates[path] = {
                    "size_bytes": int(size_bytes),
                    "size_text": _human_size(size_bytes),
                    "modified": self._format_modified(modified_timestamp),
                    "estimate_text": estimate_text,
                    "estimate_sort": estimate_sort,
                    "estimate_change_pct": estimate_change_pct,
                    "estimate_tip": estimate_tip,
                }

            self._virtual_model.update_rows(row_updates)

            if restore_sorting:
                self._table.setSortingEnabled(True)
            return

        restore_sorting = False
        if not self._probe_updates_active:
            self._table.setSortingEnabled(False)
            restore_sorting = True

        self._table.setUpdatesEnabled(False)
        try:
            for raw_row in rows:
                path, size_bytes, modified_timestamp, estimate_bytes = _unpack_row(raw_row)
                table_row = self._row_for_path(path)
                if table_row is None:
                    continue

                size_item = _NumericItem(_human_size(size_bytes), size_bytes)
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                size_item.setData(Qt.ItemDataRole.UserRole, size_bytes)
                size_item.setFlags(size_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                modified_item = QTableWidgetItem(self._format_modified(modified_timestamp))
                modified_item.setFlags(modified_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                if self._supports_conversion:
                    self._table.setItem(table_row, VCOL_SIZE, size_item)
                    self._table.setItem(table_row, VCOL_MODIFIED, modified_item)
                    if estimate_bytes is not None and estimate_bytes >= 0:
                        savings_ratio = (1.0 - (estimate_bytes / size_bytes)) if size_bytes > 0 else None
                        estimate_text = size_estimator.format_estimate(_human_size(estimate_bytes), savings_ratio)
                        estimate_item = _NumericItem(estimate_text, estimate_bytes)
                        estimate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        estimate_item.setData(Qt.ItemDataRole.UserRole, estimate_bytes)
                        estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    else:
                        estimate_item = self._audio_estimate_item(path, size_bytes, None)
                    self._table.setItem(table_row, VCOL_ESTIMATE, estimate_item)
                else:
                    self._table.setItem(table_row, COL_SIZE, size_item)
                    self._table.setItem(table_row, COL_MODIFIED, modified_item)
                    if estimate_bytes is not None and estimate_bytes >= 0:
                        savings_ratio = (1.0 - (estimate_bytes / size_bytes)) if size_bytes > 0 else None
                        estimate_text = size_estimator.format_estimate(_human_size(estimate_bytes), savings_ratio)
                        estimate_item = _NumericItem(estimate_text, estimate_bytes)
                        estimate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        estimate_item.setData(Qt.ItemDataRole.UserRole, estimate_bytes)
                        estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    else:
                        estimate_item = self._audio_estimate_item(path, size_bytes, None)
                    self._table.setItem(table_row, COL_ESTIMATE, estimate_item)
        finally:
            self._table.setUpdatesEnabled(True)

        if restore_sorting:
            self._table.setSortingEnabled(True)

    def begin_probe_updates(self):
        if self._probe_updates_active:
            return

        if not self._use_virtual_table:
            self._rebuild_path_rows()
        # Keep sorting suspended for the entire probe phase to avoid full-table
        # resorting on every small update batch.
        self._probe_updates_active = True
        self._table.setSortingEnabled(False)

    def end_probe_updates(self):
        if not self._probe_updates_active:
            return

        self._probe_updates_active = False
        QTimer.singleShot(0, self._restore_sorting_after_probe)

    def _restore_sorting_after_probe(self):
        self._table.setSortingEnabled(True)
        self._invalidate_path_rows()
        self._pagination_last_state = None

    def update_probes(self, updates: list[tuple[str, dict | None]]):
        if not updates:
            return

        for path, probe_info in updates:
            self._probe_by_path[path] = probe_info

        if self._use_virtual_table:
            assert self._virtual_model is not None
            restore_sorting = False
            if not self._probe_updates_active:
                self._table.setSortingEnabled(False)
                restore_sorting = True

            row_updates: dict[str, dict] = {}
            for path, probe_info in self._prioritize_updates(updates):
                size_bytes = self._virtual_model.size_for_path(path)
                if size_bytes is None:
                    continue
                codec_text, rec_label, reason, status = self._base_codec_recommend_values(path, probe_info)
                estimate_text, estimate_sort, estimate_tip = self._audio_estimate_values(
                    path,
                    size_bytes,
                    probe_info,
                    recommended_label=rec_label,
                )
                estimate_change_pct = _estimate_change_pct(size_bytes, float(estimate_sort)) if estimate_sort >= 0 else None
                row_updates[path] = {
                    "codec": codec_text,
                    "recommend": rec_label,
                    "rec_status": status,
                    "rec_reason": reason,
                    "rec_color": recommendation_color(status),
                    "estimate_text": estimate_text,
                    "estimate_sort": estimate_sort,
                    "estimate_change_pct": estimate_change_pct,
                    "estimate_tip": estimate_tip,
                }

            self._virtual_model.update_rows(row_updates)

            if restore_sorting:
                self._table.setSortingEnabled(True)
            return

        restore_sorting = False
        if not self._probe_updates_active:
            self._table.setSortingEnabled(False)
            restore_sorting = True

        self._table.setUpdatesEnabled(False)
        try:
            for path, probe_info in self._prioritize_updates(updates):
                self._apply_probe_update(path, probe_info)
        finally:
            self._table.setUpdatesEnabled(True)

        if restore_sorting:
            self._table.setSortingEnabled(True)

    def _apply_probe_update(self, path: str, probe_info: dict | None):
        if probe_info is not None:
            self._probe_by_path[path] = probe_info
        elif path in self._probe_by_path:
            probe_info = self._probe_by_path[path]

        row = self._row_for_path(path)
        if row is None:
            return

        size_item = self._table.item(row, VCOL_SIZE if self._supports_conversion else COL_SIZE)
        if size_item is None:
            return

        size_bytes = int(size_item.data(Qt.ItemDataRole.UserRole) or 0)

        if self._is_video:
            codec_item, rec_item, estimate_item = self._video_details_items(path, size_bytes, probe_info)
            self._table.setItem(row, VCOL_CODEC, codec_item)
            self._table.setItem(row, VCOL_REC, rec_item)
            self._table.setItem(row, VCOL_ESTIMATE, estimate_item)
        elif self._supports_conversion:
            codec_item, rec_item = self._base_codec_recommend_items(path, probe_info)
            self._table.setItem(row, VCOL_CODEC, codec_item)
            self._table.setItem(row, VCOL_REC, rec_item)
            estimate_item = self._audio_estimate_item(path, size_bytes, probe_info)
            self._table.setItem(row, VCOL_ESTIMATE, estimate_item)
        else:
            codec_item, rec_item = self._base_codec_recommend_items(path, probe_info)
            self._table.setItem(row, COL_CODEC, codec_item)
            self._table.setItem(row, COL_REC, rec_item)
            estimate_item = self._audio_estimate_item(path, size_bytes, probe_info)
            self._table.setItem(row, COL_ESTIMATE, estimate_item)

    def clear(self):
        if self._use_virtual_table:
            assert self._virtual_model is not None
            self._virtual_model.clear_rows()
            self._probe_by_path.clear()
            self._pagination_page = 0
            self._pagination_last_state = None
            self._apply_pagination(save_settings=True)
            self._update_label()
            return

        self._suspend_check_updates = True
        self._probe_by_path.clear()
        self._path_rows.clear()
        self._path_rows_dirty = False
        self._table.setRowCount(0)
        self._suspend_check_updates = False
        self._pagination_page = 0
        self._pagination_last_state = None
        self._apply_pagination(save_settings=True)
        self._update_label()
        if self._supports_conversion:
            self._refresh_selection_controls()

    def file_count(self) -> int:
        return self._visible_file_count()

    def set_scan_locked(self, locked: bool):
        self._scan_locked = locked
        if self._supports_conversion:
            self._refresh_selection_controls()
        self._page_size_combo.setEnabled(not locked)
        if locked:
            self._table.clearSelection()
            self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        elif self._default_selection_mode is not None:
            self._table.setSelectionMode(self._default_selection_mode)
        self._refresh_pagination_controls()

    def _is_row_visible(self, row: int) -> bool:
        visible_bounds = self._visible_row_bounds()
        if visible_bounds is None:
            return False
        first_visible, last_visible = visible_bounds
        return first_visible <= row <= last_visible

    def _visible_row_bounds(self) -> tuple[int, int] | None:
        row_count = self._table.model().rowCount() if self._table.model() is not None else 0
        if row_count <= 0:
            return None

        viewport = self._table.viewport()
        height = viewport.height()
        if height <= 0:
            return None

        top_index = self._table.indexAt(QPoint(0, 0))
        bottom_index = self._table.indexAt(QPoint(0, max(0, height - 1)))

        if not top_index.isValid():
            top_index = self._table.indexAt(QPoint(0, min(height - 1, 1)))
        if not bottom_index.isValid():
            bottom_index = self._table.indexAt(QPoint(0, max(0, height - 1)))

        if not top_index.isValid() and not bottom_index.isValid():
            return None

        first_visible = top_index.row() if top_index.isValid() else bottom_index.row()
        last_visible = bottom_index.row() if bottom_index.isValid() else first_visible
        if first_visible > last_visible:
            first_visible, last_visible = last_visible, first_visible

        return first_visible, last_visible

    def _prioritize_updates(self, updates: list[tuple[str, dict | None]]) -> list[tuple[str, dict | None]]:
        visible_bounds = self._visible_row_bounds()
        visible: list[tuple[str, dict | None]] = []
        hidden: list[tuple[str, dict | None]] = []
        for path, probe_info in updates:
            row = self._row_for_path(path)
            if row is not None and visible_bounds is not None and visible_bounds[0] <= row <= visible_bounds[1]:
                visible.append((path, probe_info))
            else:
                hidden.append((path, probe_info))
        return visible + hidden

    def prioritize_stat_updates(self, updates: list[tuple]) -> list[tuple]:
        visible_bounds = self._visible_row_bounds()
        visible: list[tuple] = []
        hidden: list[tuple] = []
        for update_row in updates:
            if len(update_row) < 3:
                hidden.append(update_row)
                continue

            path = update_row[0]
            row_index = self._row_for_path(path)
            if row_index is not None and visible_bounds is not None and visible_bounds[0] <= row_index <= visible_bounds[1]:
                visible.append(update_row)
            else:
                hidden.append(update_row)
        return visible + hidden

    def _update_label(self):
        visible_count = self.file_count()
        total_count = self.total_file_count()
        if visible_count == 0:
            if total_count == 0:
                self._label.setText("No files found yet.")
            else:
                noun = "file" if total_count == 1 else "files"
                self._label.setText(f"0/{total_count} {noun}")
        else:
            noun = "file" if visible_count == 1 else "files"
            count_text = f"{visible_count} {noun}" if visible_count == total_count else f"{visible_count}/{total_count} {noun}"
            if self._supports_conversion:
                selected = self._selected_row_count()
                self._label.setText(f"{count_text} ({selected} selected)")
            else:
                self._label.setText(count_text)


class FailedPanel(QWidget):
    """Simple table for scan failures."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = QSettings()
        self._scan_locked = False
        self._default_selection_mode: QAbstractItemView.SelectionMode | None = None
        self._pagination_settings_prefix = "pagination/failed"
        self._pagination_page_size = self._load_page_size_setting()
        self._pagination_page = self._load_page_setting()
        self._pagination_restoring = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        top_bar = QHBoxLayout()
        self._label = QLabel("No failures.")
        top_bar.addWidget(self._label)
        top_bar.addStretch(1)

        self._page_size_label = QLabel("Items:")
        top_bar.addWidget(self._page_size_label)

        self._page_size_combo = QComboBox()
        for size in _PAGE_SIZE_OPTIONS:
            self._page_size_combo.addItem(str(size), size)
        size_index = self._page_size_combo.findData(self._pagination_page_size)
        if size_index < 0:
            size_index = self._page_size_combo.findData(_PAGE_SIZE_OPTIONS[1])
            self._pagination_page_size = int(_PAGE_SIZE_OPTIONS[1])
        self._page_size_combo.setCurrentIndex(size_index)
        self._page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        top_bar.addWidget(self._page_size_combo)

        self._prev_page_button = QPushButton("Prev")
        self._prev_page_button.clicked.connect(self._on_prev_page)
        top_bar.addWidget(self._prev_page_button)

        self._next_page_button = QPushButton("Next")
        self._next_page_button.clicked.connect(self._on_next_page)
        top_bar.addWidget(self._next_page_button)

        self._page_label = QLabel("Page 0 of 0")
        top_bar.addWidget(self._page_label)

        layout.addLayout(top_bar)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Name", "Reason", "Absolute Path"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._default_selection_mode = self._table.selectionMode()
        layout.addWidget(self._table)
        self._apply_pagination(save_settings=False)

    def _load_page_size_setting(self) -> int:
        raw = self._settings.value(f"{self._pagination_settings_prefix}/page_size", _PAGE_SIZE_OPTIONS[1])
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return int(_PAGE_SIZE_OPTIONS[1])
        if parsed not in _PAGE_SIZE_OPTIONS:
            return int(_PAGE_SIZE_OPTIONS[1])
        return parsed

    def _load_page_setting(self) -> int:
        raw = self._settings.value(f"{self._pagination_settings_prefix}/page", 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _save_pagination_settings(self):
        self._settings.setValue(f"{self._pagination_settings_prefix}/page_size", int(self._pagination_page_size))
        self._settings.setValue(f"{self._pagination_settings_prefix}/page", int(self._pagination_page))

    def _total_pages(self) -> int:
        count = self.file_count()
        if count <= 0:
            return 0
        return (count + self._pagination_page_size - 1) // self._pagination_page_size

    def _clamp_page(self):
        total_pages = self._total_pages()
        if total_pages <= 0:
            self._pagination_page = 0
            return
        self._pagination_page = max(0, min(self._pagination_page, total_pages - 1))

    def _apply_pagination(self, *, save_settings: bool = True):
        self._clamp_page()
        start = self._pagination_page * self._pagination_page_size
        end = start + self._pagination_page_size
        for row in range(self._table.rowCount()):
            self._table.setRowHidden(row, row < start or row >= end)
        self._refresh_pagination_controls()
        if save_settings and not self._pagination_restoring:
            self._save_pagination_settings()

    def _refresh_pagination_controls(self):
        total_pages = self._total_pages()
        if total_pages <= 0:
            self._page_label.setText("Page 0 of 0")
            self._prev_page_button.setEnabled(False)
            self._next_page_button.setEnabled(False)
            return

        self._page_label.setText(f"Page {self._pagination_page + 1} of {total_pages}")
        if self._scan_locked:
            self._prev_page_button.setEnabled(False)
            self._next_page_button.setEnabled(False)
            return
        self._prev_page_button.setEnabled(self._pagination_page > 0)
        self._next_page_button.setEnabled(self._pagination_page + 1 < total_pages)

    def _on_page_size_changed(self, _index: int):
        data = self._page_size_combo.currentData()
        try:
            page_size = int(data)
        except (TypeError, ValueError):
            return

        if page_size <= 0:
            return

        if page_size != self._pagination_page_size:
            self._pagination_page_size = page_size
            self._pagination_page = 0
            self._apply_pagination(save_settings=True)

    def _on_prev_page(self):
        if self._pagination_page <= 0:
            return
        self._pagination_page -= 1
        self._apply_pagination(save_settings=True)

    def _on_next_page(self):
        if self._pagination_page + 1 >= self._total_pages():
            return
        self._pagination_page += 1
        self._apply_pagination(save_settings=True)

    def add_failures(self, rows: list[tuple[str, str, str]]):
        if not rows:
            return

        self._table.setUpdatesEnabled(False)
        try:
            for name, reason, absolute_path in rows:
                row = self._table.rowCount()
                self._table.insertRow(row)
                self._table.setItem(row, 0, QTableWidgetItem(name))
                self._table.setItem(row, 1, QTableWidgetItem(reason))
                self._table.setItem(row, 2, QTableWidgetItem(absolute_path))
        finally:
            self._table.setUpdatesEnabled(True)
        self._table.viewport().update()
        self._apply_pagination(save_settings=False)
        self._update_label()

    def clear(self):
        self._table.setRowCount(0)
        self._pagination_page = 0
        self._apply_pagination(save_settings=True)
        self._update_label()

    def set_scan_locked(self, locked: bool):
        self._scan_locked = locked
        self._page_size_combo.setEnabled(not locked)
        if locked:
            self._table.clearSelection()
            self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        elif self._default_selection_mode is not None:
            self._table.setSelectionMode(self._default_selection_mode)
        self._refresh_pagination_controls()

    def file_count(self) -> int:
        return self._table.rowCount()

    def _update_label(self):
        count = self._table.rowCount()
        if count == 0:
            self._label.setText("No failures.")
            return
        noun = "item" if count == 1 else "items"
        self._label.setText(f"{count} failed {noun}")
