import os
from datetime import datetime

from PySide6.QtGui import QColor, QFont

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel,
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
VIDEO_COLUMNS = ["Name", "Size", "Codec", "Recommended", "Estimate", "Path", "Modified"]

COL_NAME, COL_SIZE, COL_ESTIMATE, COL_PATH, COL_MODIFIED = range(5)
VCOL_NAME, VCOL_SIZE, VCOL_CODEC, VCOL_REC, VCOL_ESTIMATE, VCOL_PATH, VCOL_MODIFIED = range(7)

# Colours for the Recommended cell
_COLOR_OPTIMAL  = QColor("#2e7d32")   # dark green
_COLOR_GOOD     = QColor("#1565c0")   # dark blue
_COLOR_REENCODE = QColor("#e65100")   # dark orange


class MediaPanel(QWidget):
    """A table that lists media files of one type."""

    def __init__(self, media_type: str, parent=None):
        super().__init__(parent)
        self._media_type = media_type
        self._is_video = media_type == "Videos"
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._label = QLabel("No files found yet.")
        layout.addWidget(self._label)

        if self._is_video:
            columns = VIDEO_COLUMNS
        else:
            columns = BASE_COLUMNS

        self._table = QTableWidget(0, len(columns))
        self._table.setHorizontalHeaderLabels(columns)

        if self._is_video:
            self._table.horizontalHeader().setSectionResizeMode(VCOL_NAME,     QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_SIZE,     QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_CODEC,    QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_REC,      QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_ESTIMATE, QHeaderView.ResizeMode.ResizeToContents)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_PATH,     QHeaderView.ResizeMode.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(VCOL_MODIFIED, QHeaderView.ResizeMode.ResizeToContents)
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
        layout.addWidget(self._table)

    def add_file(self, path: str):
        # Disable sorting while inserting to avoid row-index shifting
        self._table.setSortingEnabled(False)

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

            for item in (name_item, size_item, codec_item, rec_item, estimate_item, path_item, modified_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

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

        self._table.setSortingEnabled(True)
        self._update_label()

    def clear(self):
        self._table.setRowCount(0)
        self._update_label()

    def file_count(self) -> int:
        return self._table.rowCount()

    def _update_label(self):
        count = self._table.rowCount()
        if count == 0:
            self._label.setText("No files found yet.")
        else:
            noun = "file" if count == 1 else "files"
            self._label.setText(f"{count} {noun}")
