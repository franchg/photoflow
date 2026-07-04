"""Qt models for the grid: one FileEntry per photo, plus filter/sort proxy.

The model is data-only; thumbnails arrive asynchronously from workers via
update_thumb(). data() lazily requests missing thumbs through a callback the
controller installs, which makes loading visible-first for free (QListView
only asks for what it paints).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QSortFilterProxyModel,
                            Qt, Signal)
from PySide6.QtGui import QImage

IdRole = Qt.ItemDataRole.UserRole + 1
PathRole = Qt.ItemDataRole.UserRole + 2
RatingRole = Qt.ItemDataRole.UserRole + 3
FlagRole = Qt.ItemDataRole.UserRole + 4
EditedRole = Qt.ItemDataRole.UserRole + 5
CaptureDtRole = Qt.ItemDataRole.UserRole + 6
ThumbRole = Qt.ItemDataRole.UserRole + 7
NameRole = Qt.ItemDataRole.UserRole + 8

FLAG_NONE, FLAG_PICK, FLAG_REJECT = 0, 1, -1


@dataclass
class FileEntry:
    id: int
    path: str
    name: str
    mtime: float
    size: int
    width: int = 0
    height: int = 0
    orientation: int = 1
    capture_dt: str | None = None
    rating: int = 0
    flag: int = 0
    stack_json: str | None = None
    has_edits: bool = False
    has_thumb_cache: bool = False      # catalog has a valid small blob
    thumb: QImage = field(default=None, repr=False)
    thumb_requested: bool = False


class FileListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[FileEntry] = []
        self._row_by_id: dict[int, int] = {}
        self.thumb_requester = None  # callable(FileEntry), set by controller

    # -- population ------------------------------------------------------

    def set_entries(self, entries: list[FileEntry]) -> None:
        self.beginResetModel()
        self._entries = entries
        self._row_by_id = {e.id: i for i, e in enumerate(entries)}
        self.endResetModel()

    def clear(self) -> None:
        self.set_entries([])

    def entry(self, row: int) -> FileEntry | None:
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def entry_by_id(self, fid: int) -> FileEntry | None:
        row = self._row_by_id.get(fid)
        return None if row is None else self._entries[row]

    def entries(self) -> list[FileEntry]:
        return self._entries

    def remove_entries(self, fids: list[int]) -> None:
        rows = sorted((self._row_by_id[f] for f in fids if f in self._row_by_id),
                      reverse=True)
        for row in rows:
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._entries[row]
            self.endRemoveRows()
        self._row_by_id = {e.id: i for i, e in enumerate(self._entries)}

    # -- QAbstractListModel -------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole or role == NameRole:
            return e.name
        if role == ThumbRole:
            if e.thumb is None and not e.thumb_requested and self.thumb_requester:
                e.thumb_requested = True
                self.thumb_requester(e)
            return e.thumb
        if role == IdRole:
            return e.id
        if role == PathRole:
            return e.path
        if role == RatingRole:
            return e.rating
        if role == FlagRole:
            return e.flag
        if role == EditedRole:
            return e.has_edits
        if role == CaptureDtRole:
            # Fall back to mtime so date sort is total before EXIF arrives.
            return e.capture_dt or f"~{e.mtime:017.6f}"
        return None

    # -- async updates from workers / controller ----------------------------

    def _emit_changed(self, row: int, roles: list[int]) -> None:
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, roles)

    def update_thumb(self, fid: int, thumb: QImage) -> None:
        row = self._row_by_id.get(fid)
        if row is None:
            return
        e = self._entries[row]
        e.thumb = thumb
        e.thumb_requested = True
        self._emit_changed(row, [ThumbRole])

    def invalidate_thumb(self, fid: int) -> None:
        """Force a re-request on next paint (after an edit-stack change)."""
        row = self._row_by_id.get(fid)
        if row is not None:
            self._entries[row].thumb_requested = False
            self._emit_changed(row, [ThumbRole])

    def update_meta(self, fid: int, width: int, height: int,
                    orientation: int, capture_dt: str | None) -> None:
        row = self._row_by_id.get(fid)
        if row is None:
            return
        e = self._entries[row]
        e.width, e.height, e.orientation = width, height, orientation
        if capture_dt:
            e.capture_dt = capture_dt
        self._emit_changed(row, [CaptureDtRole])

    def set_rating(self, fid: int, rating: int) -> None:
        row = self._row_by_id.get(fid)
        if row is not None:
            self._entries[row].rating = rating
            self._emit_changed(row, [RatingRole])

    def set_flag(self, fid: int, flag: int) -> None:
        row = self._row_by_id.get(fid)
        if row is not None:
            self._entries[row].flag = flag
            self._emit_changed(row, [FlagRole])

    def set_stack(self, fid: int, stack_json: str | None, has_edits: bool) -> None:
        row = self._row_by_id.get(fid)
        if row is not None:
            e = self._entries[row]
            e.stack_json, e.has_edits = stack_json, has_edits
            self._emit_changed(row, [EditedRole])


SORT_NAME, SORT_DATE = 0, 1
FLAG_FILTER_ALL, FLAG_FILTER_PICKS, FLAG_FILTER_REJECTS, FLAG_FILTER_UNFLAGGED = range(4)


class FilterProxy(QSortFilterProxyModel):
    """Rating / flag / edited filtering + name or capture-date sort."""

    filtersChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.min_rating = 0
        self.flag_filter = FLAG_FILTER_ALL
        self.edited_only = False
        self.sort_key = SORT_NAME
        self.setDynamicSortFilter(True)

    def set_filters(self, min_rating=None, flag_filter=None, edited_only=None):
        if min_rating is not None:
            self.min_rating = min_rating
        if flag_filter is not None:
            self.flag_filter = flag_filter
        if edited_only is not None:
            self.edited_only = edited_only
        self.invalidateRowsFilter()
        self.filtersChanged.emit()

    def set_sort_key(self, key: int) -> None:
        self.sort_key = key
        self.sort(0, Qt.SortOrder.AscendingOrder)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        m = self.sourceModel()
        idx = m.index(row, 0, parent)
        if self.min_rating and m.data(idx, RatingRole) < self.min_rating:
            return False
        flag = m.data(idx, FlagRole)
        if self.flag_filter == FLAG_FILTER_PICKS and flag != FLAG_PICK:
            return False
        if self.flag_filter == FLAG_FILTER_REJECTS and flag != FLAG_REJECT:
            return False
        if self.flag_filter == FLAG_FILTER_UNFLAGGED and flag != FLAG_NONE:
            return False
        if self.edited_only and not m.data(idx, EditedRole):
            return False
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        m = self.sourceModel()
        if self.sort_key == SORT_DATE:
            lv, rv = m.data(left, CaptureDtRole), m.data(right, CaptureDtRole)
            if lv != rv:
                return lv < rv
        return m.data(left, NameRole) < m.data(right, NameRole)
