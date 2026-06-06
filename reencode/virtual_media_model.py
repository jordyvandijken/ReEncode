from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont


class VirtualMediaTableModel(QAbstractTableModel):
    """Model-backed media rows used to pilot virtualized table rendering."""

    def __init__(self, columns: list[str], parent=None, exposure_chunk: int = 1000):
        super().__init__(parent)
        self._columns = columns
        self._rows: list[dict[str, Any]] = []
        self._exposed_count = 0
        self._exposure_chunk = max(1, int(exposure_chunk))
        self._pagination_page = 0
        self._pagination_page_size = 50
        self._path_rows: dict[str, int] = {}
        self._path_rows_dirty = False

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        total_rows = len(self._rows)
        if total_rows <= 0:
            return 0

        start = self._pagination_page * self._pagination_page_size
        end = min(total_rows, start + self._pagination_page_size)
        if start >= total_rows:
            return 0
        return max(0, end - start)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._columns):
            return self._columns[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        full_row = self._full_row_from_display_row(index.row())
        if full_row is None:
            return None

        row_data = self._rows[full_row]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return row_data["name"]
            if col == 1:
                return row_data["size_text"]
            if col == 2:
                return row_data["codec"]
            if col == 3:
                return row_data["recommend"]
            if col == 4:
                return row_data["estimate_text"]
            if col == 5:
                return row_data["path"]
            if col == 6:
                return row_data["modified"]
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 0:
                return row_data["path"]
            if col == 3:
                return row_data["rec_reason"]
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole and col in {1, 4}:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole and col == 3:
            return QBrush(row_data["rec_color"])

        if role == Qt.ItemDataRole.FontRole and col == 3:
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.UserRole and col == 1:
            return row_data["size_bytes"]

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder):
        reverse = order == Qt.SortOrder.DescendingOrder
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=lambda row: self._sort_key(row, column), reverse=reverse)
        self.layoutChanged.emit()
        self._path_rows_dirty = True

    def clear_rows(self):
        self.beginResetModel()
        self._rows.clear()
        self._exposed_count = 0
        self._pagination_page = 0
        self._path_rows.clear()
        self._path_rows_dirty = False
        self.endResetModel()

    def append_rows(self, rows: list[dict[str, Any]]):
        if not rows:
            return

        total_before = len(self._rows)
        exposed_before = self._exposed_count
        total_after = total_before + len(rows)

        target_exposed = exposed_before
        if exposed_before == total_before and exposed_before < self._exposure_chunk:
            target_exposed = min(total_after, self._exposure_chunk)

        if target_exposed > exposed_before:
            self.beginInsertRows(QModelIndex(), exposed_before, target_exposed - 1)
            self._rows.extend(rows)
            self._exposed_count = target_exposed
            self.endInsertRows()
        else:
            self._rows.extend(rows)

        self._path_rows_dirty = True

    def total_row_count(self) -> int:
        return len(self._rows)

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:
        if parent.isValid():
            return False
        return False

    def fetchMore(self, parent: QModelIndex = QModelIndex()):
        return

    def row_for_path(self, path: str) -> int | None:
        full_row = self._full_row_for_path(path)
        if full_row is None:
            return None
        return self._display_row_from_full_row(full_row)

    def size_for_path(self, path: str) -> int | None:
        row = self._full_row_for_path(path)
        if row is None:
            return None
        return int(self._rows[row]["size_bytes"])

    def set_pagination(self, page: int, page_size: int):
        page = max(0, int(page))
        page_size = max(1, int(page_size))

        if self._pagination_page == page and self._pagination_page_size == page_size:
            return

        self.beginResetModel()
        self._pagination_page = page
        self._pagination_page_size = page_size
        self.endResetModel()

    def update_row(self, path: str, updates: dict[str, Any]) -> bool:
        return self.update_rows({path: updates}) > 0

    def update_rows(self, row_updates: dict[str, dict[str, Any]]) -> int:
        if not row_updates:
            return 0

        if self._path_rows_dirty:
            self._rebuild_path_rows()

        changed_visible_rows: list[int] = []
        updated_count = 0
        for path, updates in row_updates.items():
            row = self._path_rows.get(path)
            if row is None:
                continue

            self._rows[row].update(updates)
            updated_count += 1
            visible_row = self._display_row_from_full_row(row)
            if visible_row is not None:
                changed_visible_rows.append(visible_row)

        self._emit_coalesced_data_changed(changed_visible_rows)
        return updated_count

    def _emit_coalesced_data_changed(self, rows: list[int]):
        if not rows:
            return

        rows = sorted(set(rows))
        range_start = rows[0]
        previous = rows[0]
        last_col = len(self._columns) - 1

        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue

            self.dataChanged.emit(self.index(range_start, 0), self.index(previous, last_col))
            range_start = row
            previous = row

        self.dataChanged.emit(self.index(range_start, 0), self.index(previous, last_col))

    def _rebuild_path_rows(self):
        self._path_rows = {row_data["path"]: row for row, row_data in enumerate(self._rows)}
        self._path_rows_dirty = False

    def _full_row_for_path(self, path: str) -> int | None:
        if self._path_rows_dirty:
            self._rebuild_path_rows()
        return self._path_rows.get(path)

    def _current_page_start(self) -> int:
        return self._pagination_page * self._pagination_page_size

    def _display_row_from_full_row(self, full_row: int) -> int | None:
        start = self._current_page_start()
        end = start + self._pagination_page_size
        if full_row < start or full_row >= end:
            return None
        return full_row - start

    def _full_row_from_display_row(self, display_row: int) -> int | None:
        full_row = self._current_page_start() + display_row
        if full_row < 0 or full_row >= len(self._rows):
            return None
        return full_row

    def _sort_key(self, row_data: dict[str, Any], column: int):
        if column == 1:
            return row_data["size_bytes"]
        if column == 4:
            return row_data["estimate_sort"]
        if column == 6:
            return row_data["modified"]

        if column == 0:
            return row_data["name"].casefold()
        if column == 2:
            return row_data["codec"].casefold()
        if column == 3:
            return row_data["recommend"].casefold()
        if column == 5:
            return row_data["path"].casefold()
        return ""


def recommendation_color(status: str) -> QColor:
    if status == "optimal":
        return QColor("#2e7d32")
    if status == "good":
        return QColor("#1565c0")
    if status == "pending":
        return QColor("#616161")
    return QColor("#e65100")
