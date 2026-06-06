import os
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from reencode import codec_probe
from reencode import size_estimator


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


BASE_COLUMNS = ["Name", "Size", "Estimate", "Path", "Modified"]
VIDEO_COLUMNS = ["", "Name", "Size", "Codec", "Recommended", "Estimate", "Path", "Modified"]

COL_NAME, COL_SIZE, COL_ESTIMATE, COL_PATH, COL_MODIFIED = range(5)
VCOL_SELECT, VCOL_NAME, VCOL_SIZE, VCOL_CODEC, VCOL_REC, VCOL_ESTIMATE, VCOL_PATH, VCOL_MODIFIED = range(8)

# Colours for the Recommended cell
_COLOR_OPTIMAL  = QColor("#2e7d32")   # dark green
_COLOR_GOOD     = QColor("#1565c0")   # dark blue
_COLOR_REENCODE = QColor("#e65100")   # dark orange
_COLOR_PENDING  = QColor("#616161")   # neutral gray


def _recommended_ffmpeg_args(recommended_label: str) -> list[str]:
    label = recommended_label.lower()
    if "av1" in label:
        return ["-c:v", "libaom-av1", "-crf", "32", "-b:v", "0", "-cpu-used", "6"]
    if "vp9" in label:
        return ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0"]
    if "h.264" in label or "avc" in label:
        return ["-c:v", "libx264", "-crf", "23", "-preset", "medium"]
    return ["-c:v", "libx265", "-crf", "28", "-preset", "medium"]


def _recommended_output_path(path: str, recommended_label: str) -> str:
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


def _temporary_output_path(final_output_path: str) -> str:
    final_output = Path(final_output_path)
    suffix = final_output.suffix or ".mkv"
    token = uuid4().hex[:8]
    temp_name = f"{final_output.stem}.reencode-temp-{token}{suffix}"
    return str(final_output.with_name(temp_name))


class _ConversionThread(QThread):
    progress = Signal(str, int, int, object)
    finished = Signal(bool, str)

    def __init__(self, jobs: list[tuple[str, str]], replace_originals: bool, parent=None):
        super().__init__(parent)
        self._jobs = jobs
        self._replace_originals = replace_originals

    def run(self):
        total_jobs = len(self._jobs)

        for job_index, (source_path, final_output_path) in enumerate(self._jobs, start=1):
            temp_output_path = _temporary_output_path(final_output_path)
            probe_info = codec_probe.probe_media_info(source_path) or {}
            codec_name = (probe_info.get("video_codec") or "").lower()
            _status, recommended_label, _reason = codec_probe.recommendation(codec_name)
            ffmpeg_args = _recommended_ffmpeg_args(recommended_label)
            duration_seconds = probe_info.get("duration")
            if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
                duration_seconds = None

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
                "-map",
                "0",
                *ffmpeg_args,
                "-c:a",
                "copy",
                "-c:s",
                "copy",
                temp_output_path,
            ]

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
        self._conversion_thread: _ConversionThread | None = None
        self._path_items: dict[str, list[QTableWidgetItem]] = {}
        self._suspend_check_updates = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        top_bar = QHBoxLayout()
        self._label = QLabel("No files found yet.")
        top_bar.addWidget(self._label)

        if self._is_video:
            top_bar.addStretch(1)

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

        if self._is_video:
            columns = VIDEO_COLUMNS
        else:
            columns = BASE_COLUMNS

        self._table = QTableWidget(0, len(columns))
        self._table.setHorizontalHeaderLabels(columns)

        if self._is_video:
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
            self._table.horizontalHeader().setSectionResizeMode(COL_ESTIMATE, QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(COL_PATH,     QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(COL_MODIFIED, QHeaderView.ResizeMode.ResizeToContents)

        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        # Keep path data in the model for internal lookups, but hide it from the UI.
        self._table.setColumnHidden(VCOL_PATH if self._is_video else COL_PATH, True)
        if self._is_video:
            self._table.itemChanged.connect(self._on_table_item_changed)
        layout.addWidget(self._table)

        if self._is_video:
            self._refresh_video_controls()

    def _on_select_all_changed(self, state: int):
        if not self._is_video or self._suspend_check_updates:
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

        self._refresh_video_controls()

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if not self._is_video or self._suspend_check_updates:
            return

        if self._table.indexFromItem(item).column() != VCOL_SELECT:
            return

        self._refresh_video_controls()

    def _refresh_video_controls(self):
        if not self._is_video:
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

        self._convert_button.setEnabled(selected > 0 and self._conversion_thread is None)
        self._do_not_replace.setEnabled(total > 0 and self._conversion_thread is None)
        self._update_label()

    def _selected_row_count(self) -> int:
        selected = 0
        for row in range(self._table.rowCount()):
            item = self._table.item(row, VCOL_SELECT)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected += 1
        return selected

    def _selected_video_jobs(self, replace_originals: bool) -> list[tuple[str, str]]:
        jobs: list[tuple[str, str]] = []
        for row in range(self._table.rowCount()):
            select_item = self._table.item(row, VCOL_SELECT)
            if select_item is None or select_item.checkState() != Qt.CheckState.Checked:
                continue

            path_item = self._table.item(row, VCOL_PATH)
            rec_item = self._table.item(row, VCOL_REC)
            if path_item is None or rec_item is None:
                continue

            source_path = path_item.text()
            if replace_originals:
                final_output_path = source_path
            else:
                final_output_path = _recommended_output_path(source_path, rec_item.text())

            jobs.append((source_path, final_output_path))

        return jobs

    def _row_for_path(self, path: str) -> int | None:
        cached_items = self._path_items.get(path)
        while cached_items:
            row = cached_items[0].row()
            if row >= 0:
                return row
            cached_items.pop(0)

        for row in range(self._table.rowCount()):
            path_item = self._table.item(row, VCOL_PATH if self._is_video else COL_PATH)
            if path_item is not None and path_item.text() == path:
                return row
        return None

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
        if self._media_type.lower() == "audio" and probe_info is None:
            estimate_item = _NumericItem("Pending probe", -1)
            estimate_item.setToolTip("Estimate appears after probe completes.")
            estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return estimate_item

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

        estimate_item.setFlags(estimate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return estimate_item

    def _convert_selected(self):
        if self._conversion_thread is not None:
            return

        replace_originals = not self._do_not_replace.isChecked()
        jobs = self._selected_video_jobs(replace_originals)
        if not jobs:
            QMessageBox.information(self, "Convert selected", "Select at least one video first.")
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
        modified = "—"
        if modified_timestamp:
            try:
                modified = datetime.fromtimestamp(int(modified_timestamp)).strftime("%Y-%m-%d %H:%M")
            except (OverflowError, ValueError):
                modified = "—"

        name_item = QTableWidgetItem(name)
        name_item.setToolTip(path)

        size_item = _NumericItem(_human_size(size_bytes), size_bytes)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size_item.setData(Qt.ItemDataRole.UserRole, size_bytes)

        path_item = QTableWidgetItem(path)
        self._path_items.setdefault(path, []).append(path_item)
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
        else:
            estimate_item = self._audio_estimate_item(path, size_bytes, None)

            for item in (name_item, size_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self._table.setItem(row, COL_NAME, name_item)
            self._table.setItem(row, COL_SIZE, size_item)
            self._table.setItem(row, COL_ESTIMATE, estimate_item)
            self._table.setItem(row, COL_PATH, path_item)
            self._table.setItem(row, COL_MODIFIED, modified_item)

    def add_file(self, path: str, size_bytes: int = 0, modified_timestamp: str = ""):
        # Disable sorting while inserting to avoid row-index shifting
        self._table.setSortingEnabled(False)
        self._suspend_check_updates = True

        self._insert_file_row(path, size_bytes, modified_timestamp)

        self._suspend_check_updates = False
        self._table.setSortingEnabled(True)
        self._update_label()
        if self._is_video:
            self._refresh_video_controls()

    def add_files(self, rows: list[tuple[str, int, str]]):
        if not rows:
            return

        self._table.setSortingEnabled(False)
        self._suspend_check_updates = True
        for path, size_bytes, modified_timestamp in rows:
            self._insert_file_row(path, size_bytes, modified_timestamp)

        self._suspend_check_updates = False
        self._table.setSortingEnabled(True)
        self._update_label()
        if self._is_video:
            self._refresh_video_controls()

    def update_probe(self, path: str, probe_info: dict | None):
        row = self._row_for_path(path)
        if row is None:
            return

        size_item = self._table.item(row, VCOL_SIZE if self._is_video else COL_SIZE)
        if size_item is None:
            return

        size_bytes = int(size_item.data(Qt.ItemDataRole.UserRole) or 0)

        if self._is_video:
            codec_item, rec_item, estimate_item = self._video_details_items(path, size_bytes, probe_info)
            self._table.setItem(row, VCOL_CODEC, codec_item)
            self._table.setItem(row, VCOL_REC, rec_item)
            self._table.setItem(row, VCOL_ESTIMATE, estimate_item)
        else:
            estimate_item = self._audio_estimate_item(path, size_bytes, probe_info)
            self._table.setItem(row, COL_ESTIMATE, estimate_item)

    def clear(self):
        self._suspend_check_updates = True
        self._path_items.clear()
        self._table.setRowCount(0)
        self._suspend_check_updates = False
        self._update_label()
        if self._is_video:
            self._refresh_video_controls()

    def file_count(self) -> int:
        return self._table.rowCount()

    def _update_label(self):
        count = self._table.rowCount()
        if count == 0:
            self._label.setText("No files found yet.")
        else:
            noun = "file" if count == 1 else "files"
            if self._is_video:
                selected = self._selected_row_count()
                self._label.setText(f"{count} {noun} ({selected} selected)")
            else:
                self._label.setText(f"{count} {noun}")
