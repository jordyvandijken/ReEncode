import os
import subprocess
from datetime import datetime
from pathlib import Path

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


class _ConversionThread(QThread):
    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, jobs: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._jobs = jobs

    def run(self):
        for source_path, output_path in self._jobs:
            probe_info = codec_probe.probe_media_info(source_path) or {}
            codec_name = (probe_info.get("video_codec") or "").lower()
            _status, recommended_label, _reason = codec_probe.recommendation(codec_name)
            ffmpeg_args = _recommended_ffmpeg_args(recommended_label)

            command = [
                "ffmpeg",
                "-y",
                "-i",
                source_path,
                "-map",
                "0",
                *ffmpeg_args,
                "-c:a",
                "copy",
                "-c:s",
                "copy",
                output_path,
            ]

            try:
                result = subprocess.run(command, capture_output=True, text=True)
            except FileNotFoundError:
                self.finished.emit(False, "ffmpeg was not found in PATH.")
                return

            if result.returncode != 0:
                details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg failed for {os.path.basename(source_path)}"
                self.finished.emit(False, details)
                return

            self.progress.emit(output_path)

        self.finished.emit(True, f"Converted {len(self._jobs)} file(s).")


class MediaPanel(QWidget):
    """A table that lists media files of one type."""

    def __init__(self, media_type: str, parent=None):
        super().__init__(parent)
        self._media_type = media_type
        self._is_video = media_type == "Videos"
        self._conversion_thread: _ConversionThread | None = None
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

    def _selected_row_count(self) -> int:
        selected = 0
        for row in range(self._table.rowCount()):
            item = self._table.item(row, VCOL_SELECT)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected += 1
        return selected

    def _selected_video_jobs(self) -> list[tuple[str, str]]:
        jobs: list[tuple[str, str]] = []
        for row in range(self._table.rowCount()):
            select_item = self._table.item(row, VCOL_SELECT)
            if select_item is None or select_item.checkState() != Qt.CheckState.Checked:
                continue

            path_item = self._table.item(row, VCOL_PATH)
            rec_item = self._table.item(row, VCOL_REC)
            if path_item is None or rec_item is None:
                continue

            jobs.append((path_item.text(), _recommended_output_path(path_item.text(), rec_item.text())))

        return jobs

    def _convert_selected(self):
        if self._conversion_thread is not None:
            return

        jobs = self._selected_video_jobs()
        if not jobs:
            QMessageBox.information(self, "Convert selected", "Select at least one video first.")
            return

        self._suspend_check_updates = True
        try:
            self._convert_button.setEnabled(False)
            self._select_all.setEnabled(False)
        finally:
            self._suspend_check_updates = False

        self._conversion_thread = _ConversionThread(jobs, parent=self)
        self._conversion_thread.progress.connect(self._on_conversion_progress)
        self._conversion_thread.finished.connect(self._on_conversion_finished)
        self._label.setText(f"Converting {len(jobs)} file(s)...")
        self._conversion_thread.start()

    def _on_conversion_progress(self, output_path: str):
        self._label.setText(f"Created {os.path.basename(output_path)}")

    def _on_conversion_finished(self, success: bool, message: str):
        if self._conversion_thread is not None:
            self._conversion_thread.deleteLater()
            self._conversion_thread = None

        self._update_label()
        self._refresh_video_controls()

        if success:
            QMessageBox.information(self, "Convert selected", message)
        else:
            QMessageBox.warning(self, "Convert selected", message[:1000])

    def add_file(self, path: str):
        # Disable sorting while inserting to avoid row-index shifting
        self._table.setSortingEnabled(False)
        self._suspend_check_updates = True

        row = self._table.rowCount()
        self._table.insertRow(row)

        name = os.path.basename(path)

        try:
            stat = os.stat(path)
            size_bytes = stat.st_size
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            size_bytes = 0
            modified = "—"

        name_item = QTableWidgetItem(name)
        name_item.setToolTip(path)

        size_item = _NumericItem(_human_size(size_bytes), size_bytes)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        path_item = QTableWidgetItem(path)
        modified_item = QTableWidgetItem(modified)

        if self._is_video:
            probe_info = codec_probe.probe_media_info(path)
            raw_codec = (probe_info or {}).get("video_codec")
            if raw_codec:
                codec_text = codec_probe.codec_label(raw_codec)
                status, rec_label, reason = codec_probe.recommendation(raw_codec)
            else:
                codec_text = "Unknown (install ffmpeg)"
                status, rec_label, reason = "reencode", "H.265/HEVC", "Install ffmpeg/ffprobe to detect codec."

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
            color = {"optimal": _COLOR_OPTIMAL, "good": _COLOR_GOOD}.get(status, _COLOR_REENCODE)
            rec_item.setForeground(color)
            rec_item.setFont(_bold_font(rec_item.font()))

            select_item = QTableWidgetItem()
            select_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for item in (name_item, size_item, codec_item, rec_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self._table.setItem(row, VCOL_SELECT,   select_item)
            self._table.setItem(row, VCOL_NAME,     name_item)
            self._table.setItem(row, VCOL_SIZE,     size_item)
            self._table.setItem(row, VCOL_CODEC,    codec_item)
            self._table.setItem(row, VCOL_REC,      rec_item)
            self._table.setItem(row, VCOL_ESTIMATE, estimate_item)
            self._table.setItem(row, VCOL_PATH,     path_item)
            self._table.setItem(row, VCOL_MODIFIED, modified_item)
        else:
            probe_info = codec_probe.probe_media_info(path) if self._media_type == "Audio" else None
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

            for item in (name_item, size_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self._table.setItem(row, COL_NAME,     name_item)
            self._table.setItem(row, COL_SIZE,     size_item)
            self._table.setItem(row, COL_ESTIMATE, estimate_item)
            self._table.setItem(row, COL_PATH,     path_item)
            self._table.setItem(row, COL_MODIFIED, modified_item)

        self._suspend_check_updates = False
        self._table.setSortingEnabled(True)
        self._update_label()
        if self._is_video:
            self._refresh_video_controls()

    def clear(self):
        self._suspend_check_updates = True
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
            self._label.setText(f"{count} {noun}")
