import os
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, QPoint, QSettings, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from reencode import codec_probe
from reencode import size_estimator
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


def _columns_for_media_type(media_type: str, supports_conversion: bool) -> list[str]:
    if media_type == "Images":
        return IMAGE_SELECTABLE_COLUMNS if supports_conversion else IMAGE_BASE_COLUMNS
    return SELECTABLE_COLUMNS if supports_conversion else BASE_COLUMNS


def _recommended_ffmpeg_args(recommended_label: str) -> list[str]:
    label = recommended_label.lower()
    if "av1" in label:
        return ["-c:v", "libaom-av1", "-crf", "32", "-b:v", "0", "-cpu-used", "6"]
    if "vp9" in label:
        return ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0"]
    if "h.264" in label or "avc" in label:
        return ["-c:v", "libx264", "-crf", "23", "-preset", "medium"]
    return ["-c:v", "libx265", "-crf", "28", "-preset", "medium"]


def _recommended_audio_ffmpeg_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", "192k"]


def _recommended_image_ffmpeg_args() -> list[str]:
    return ["-c:v", "libwebp", "-quality", "80"]


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


def _recommended_audio_output_path(path: str) -> str:
    source = Path(path)
    candidate = source.with_name(f"{source.stem}.reencoded.m4a")

    if not candidate.exists():
        return str(candidate)

    index = 2
    while True:
        next_candidate = source.with_name(f"{source.stem}.reencoded-{index}.m4a")
        if not next_candidate.exists():
            return str(next_candidate)
        index += 1


def _recommended_image_output_path(path: str) -> str:
    source = Path(path)
    candidate = source.with_name(f"{source.stem}.reencoded.webp")

    if not candidate.exists():
        return str(candidate)

    index = 2
    while True:
        next_candidate = source.with_name(f"{source.stem}.reencoded-{index}.webp")
        if not next_candidate.exists():
            return str(next_candidate)
        index += 1


def _recommended_output_path(media_type: str, path: str, recommended_label: str) -> str:
    if media_type == "Videos":
        return _recommended_video_output_path(path, recommended_label)
    if media_type == "Audio":
        return _recommended_audio_output_path(path)
    return _recommended_image_output_path(path)


def _temporary_output_path(final_output_path: str) -> str:
    final_output = Path(final_output_path)
    suffix = final_output.suffix or ".mkv"
    token = uuid4().hex[:8]
    temp_name = f"{final_output.stem}.reencode-temp-{token}{suffix}"
    return str(final_output.with_name(temp_name))


class _ConversionThread(QThread):
    progress = Signal(str, int, int, object)
    finished = Signal(bool, str)

    def __init__(self, jobs: list[tuple[str, str, str, str]], replace_originals: bool, parent=None):
        super().__init__(parent)
        self._jobs = jobs
        self._replace_originals = replace_originals

    def _job_conversion_inputs(self, media_type: str, source_path: str, recommended_label: str) -> tuple[list[str], float | None]:
        probe_info = codec_probe.probe_media_info(source_path) or {}

        if media_type == "Videos":
            codec_name = (probe_info.get("video_codec") or "").lower()
            if not recommended_label:
                _status, recommended_label, _reason = codec_probe.recommendation(codec_name)
            ffmpeg_args = _recommended_ffmpeg_args(recommended_label)
            duration_seconds = probe_info.get("duration")
        elif media_type == "Audio":
            ffmpeg_args = _recommended_audio_ffmpeg_args()
            duration_seconds = probe_info.get("duration")
        else:
            ffmpeg_args = _recommended_image_ffmpeg_args()
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
                process = subprocess.Popen(
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
        self._setup_ui()

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

        if self._supports_conversion:
            self._select_all = QCheckBox("Select all")
            self._select_all.setTristate(True)
            self._select_all.stateChanged.connect(self._on_select_all_changed)
            top_bar.addWidget(self._select_all)

            self._do_not_replace = QCheckBox("Do not replace original")
            self._do_not_replace.setChecked(False)
            self._do_not_replace.setToolTip("When checked, conversion creates a separate .reencoded output file.")
            top_bar.addWidget(self._do_not_replace)

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
            self._table.horizontalHeader().setSectionResizeMode(VCOL_SELECT,    QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_NAME,      QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_SIZE,      QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_CODEC,     QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_REC,       QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_ESTIMATE,  QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_PATH,      QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_MODIFIED,  QHeaderView.ResizeMode.ResizeToContents)
        else:
            self._table.horizontalHeader().setSectionResizeMode(COL_NAME,     QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(COL_SIZE,     QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(COL_CODEC,    QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(COL_REC,      QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(COL_ESTIMATE, QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(COL_PATH,     QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_MODIFIED, QHeaderView.ResizeMode.ResizeToContents)

        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._default_selection_mode = self._table.selectionMode()
        if not self._use_virtual_table:
            self._table.horizontalHeader().sortIndicatorChanged.connect(self._invalidate_path_rows)
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
        if self._media_type == "Images":
            self._do_not_replace.setChecked(True)
            self._do_not_replace.setEnabled(False)
        else:
            self._do_not_replace.setEnabled(total > 0 and enabled)
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

        if self._use_virtual_table:
            assert self._virtual_model is not None
            self._virtual_model.set_pagination(self._pagination_page, self._pagination_page_size)
        else:
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
            status, rec_label, reason = codec_probe.recommendation(raw_codec)
        else:
            codec_text = "Probing..."
            status, rec_label, reason = "pending", "Pending probe", "Codec details will appear after the async probe phase."

        if probe_info is None:
            estimate_item = _NumericItem("Pending probe", -1)
            estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            estimate_item.setToolTip("Estimate appears after probe completes.")
            codec_item = QTableWidgetItem(codec_text)

            rec_item = QTableWidgetItem(rec_label)
            rec_item.setToolTip(reason)
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

        estimate_bytes, savings_ratio = size_estimator.estimate_output(
            size_bytes,
            self._media_type,
            path,
            probe_info,
        )
        if estimate_bytes is None:
            estimate_item = _NumericItem("—", -1)
        else:
            estimate_text = size_estimator.format_estimate(_human_size(estimate_bytes), savings_ratio)
            estimate_item = _NumericItem(estimate_text, estimate_bytes)
            estimate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        codec_item = QTableWidgetItem(codec_text)

        rec_item = QTableWidgetItem(rec_label)
        rec_item.setToolTip(reason)
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
        estimate_text, estimate_sort = self._audio_estimate_values(path, size_bytes, probe_info)
        estimate_item = _NumericItem(estimate_text, estimate_sort)
        if estimate_text == "Pending probe":
            estimate_item.setToolTip("Estimate appears after probe completes.")
        elif estimate_sort >= 0:
            estimate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return estimate_item

    def _audio_estimate_values(self, path: str, size_bytes: int, probe_info: dict | None) -> tuple[str, float]:
        if self._media_type.lower() == "audio" and probe_info is None:
            return "Pending probe", -1

        estimate_bytes, savings_ratio = size_estimator.estimate_output(
            size_bytes,
            self._media_type,
            path,
            probe_info,
        )
        if estimate_bytes is None:
            return "—", -1
        else:
            estimate_text = size_estimator.format_estimate(_human_size(estimate_bytes), savings_ratio)
            return estimate_text, estimate_bytes

    def _base_codec_recommend_items(self, path: str, probe_info: dict | None):
        codec_text, rec_label, reason, status = self._base_codec_recommend_values(path, probe_info)

        codec_item = QTableWidgetItem(codec_text)
        rec_item = QTableWidgetItem(rec_label)
        rec_item.setToolTip(reason)

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
                status = "optimal" if file_ext == ".webp" else "good"
                return file_ext, ".webp", "Images convert to .webp by default.", status
            return "Unknown", ".webp", "File extension could not be determined.", "pending"

        if self._media_type == "Audio":
            raw_codec = (probe_info or {}).get("audio_codec")
            if raw_codec:
                codec_text = codec_probe.codec_label(raw_codec)
                status, rec_label, reason = codec_probe.recommendation(raw_codec)
            elif probe_info is None:
                codec_text = "Probing..."
                status, rec_label, reason = (
                    "pending",
                    "Pending probe",
                    "Codec details will appear after the async probe phase.",
                )
            else:
                codec_text = "Unknown"
                status, rec_label, reason = ("pending", "Pending probe", "No audio codec details were reported.")
        else:
            codec_text = "-"
            status, rec_label, reason = ("pending", "-", "Recommendations are only available for audio/video codecs.")
        return codec_text, rec_label, reason, status

    def _virtual_row(self, path: str, size_bytes: int, modified_timestamp: str, probe_info: dict | None) -> dict:
        codec_text, rec_label, reason, status = self._base_codec_recommend_values(path, probe_info)
        estimate_text, estimate_sort = self._audio_estimate_values(path, size_bytes, probe_info)
        return {
            "name": os.path.basename(path),
            "size_bytes": int(size_bytes),
            "size_text": _human_size(size_bytes),
            "codec": codec_text,
            "recommend": rec_label,
            "rec_reason": reason,
            "rec_color": recommendation_color(status),
            "estimate_text": estimate_text,
            "estimate_sort": estimate_sort,
            "path": path,
            "modified": self._format_modified(modified_timestamp),
        }

    def _convert_selected(self):
        if self._conversion_thread is not None:
            return

        replace_originals = not self._do_not_replace.isChecked()
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
            self._do_not_replace.setEnabled(False)
        finally:
            self._suspend_check_updates = False

        self._conversion_thread = _ConversionThread(jobs, replace_originals=replace_originals, parent=self)
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
        self._refresh_video_controls()
        self.conversion_status_changed.emit(message, False)

        if success:
            QMessageBox.information(self, "Convert selected", message)
        else:
            QMessageBox.warning(self, "Convert selected", message[:1000])

    def _insert_file_row(self, path: str, size_bytes: int = 0, modified_timestamp: str = ""):
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
        self._invalidate_path_rows()
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
        self._invalidate_path_rows()
        self._apply_pagination(save_settings=False)
        self._update_label()
        if self._supports_conversion:
            self._refresh_selection_controls()

    def update_probe(self, path: str, probe_info: dict | None):
        self.update_probes([(path, probe_info)])

    def update_file_stats(self, rows: list[tuple[str, int, str]]):
        if not rows:
            return

        if self._use_virtual_table:
            assert self._virtual_model is not None
            restore_sorting = False
            if not self._probe_updates_active:
                self._table.setSortingEnabled(False)
                restore_sorting = True

            row_updates: dict[str, dict] = {}
            for path, size_bytes, modified_timestamp in rows:
                estimate_text, estimate_sort = self._audio_estimate_values(path, size_bytes, None)
                row_updates[path] = {
                    "size_bytes": int(size_bytes),
                    "size_text": _human_size(size_bytes),
                    "modified": self._format_modified(modified_timestamp),
                    "estimate_text": estimate_text,
                    "estimate_sort": estimate_sort,
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
            for path, size_bytes, modified_timestamp in rows:
                row = self._row_for_path(path)
                if row is None:
                    continue

                size_item = _NumericItem(_human_size(size_bytes), size_bytes)
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                size_item.setData(Qt.ItemDataRole.UserRole, size_bytes)
                size_item.setFlags(size_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                modified_item = QTableWidgetItem(self._format_modified(modified_timestamp))
                modified_item.setFlags(modified_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                if self._supports_conversion:
                    self._table.setItem(row, VCOL_SIZE, size_item)
                    self._table.setItem(row, VCOL_MODIFIED, modified_item)
                else:
                    self._table.setItem(row, COL_SIZE, size_item)
                    self._table.setItem(row, COL_MODIFIED, modified_item)
                    estimate_item = self._audio_estimate_item(path, size_bytes, None)
                    self._table.setItem(row, COL_ESTIMATE, estimate_item)
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

    def update_probes(self, updates: list[tuple[str, dict | None]]):
        if not updates:
            return

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
                estimate_text, estimate_sort = self._audio_estimate_values(path, size_bytes, probe_info)
                row_updates[path] = {
                    "codec": codec_text,
                    "recommend": rec_label,
                    "rec_reason": reason,
                    "rec_color": recommendation_color(status),
                    "estimate_text": estimate_text,
                    "estimate_sort": estimate_sort,
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
            self._pagination_page = 0
            self._apply_pagination(save_settings=True)
            self._update_label()
            return

        self._suspend_check_updates = True
        self._path_rows.clear()
        self._path_rows_dirty = False
        self._table.setRowCount(0)
        self._suspend_check_updates = False
        self._pagination_page = 0
        self._apply_pagination(save_settings=True)
        self._update_label()
        if self._supports_conversion:
            self._refresh_selection_controls()

    def file_count(self) -> int:
        if self._use_virtual_table:
            assert self._virtual_model is not None
            return self._virtual_model.total_row_count()
        return self._table.rowCount()

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

    def prioritize_stat_updates(self, updates: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
        visible_bounds = self._visible_row_bounds()
        visible: list[tuple[str, int, str]] = []
        hidden: list[tuple[str, int, str]] = []
        for path, size_bytes, modified_timestamp in updates:
            row = self._row_for_path(path)
            if row is not None and visible_bounds is not None and visible_bounds[0] <= row <= visible_bounds[1]:
                visible.append((path, size_bytes, modified_timestamp))
            else:
                hidden.append((path, size_bytes, modified_timestamp))
        return visible + hidden

    def _update_label(self):
        count = self.file_count()
        if count == 0:
            self._label.setText("No files found yet.")
        else:
            noun = "file" if count == 1 else "files"
            if self._supports_conversion:
                selected = self._selected_row_count()
                self._label.setText(f"{count} {noun} ({selected} selected)")
            else:
                self._label.setText(f"{count} {noun}")


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
