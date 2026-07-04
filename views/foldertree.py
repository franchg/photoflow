"""Collapsible folder tree (left panel): directories only, lazy-loaded via
QFileSystemModel. Selecting a folder scans it into the grid."""
from __future__ import annotations

from PySide6.QtCore import QDir, Signal
from PySide6.QtWidgets import QFileSystemModel, QTreeView


class FolderTree(QTreeView):
    folder_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fs = QFileSystemModel(self)
        self._fs.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot
                           | QDir.Filter.Drives)
        self._fs.setRootPath("/")
        self.setModel(self._fs)
        self.setRootIndex(self._fs.index("/"))
        for col in range(1, self._fs.columnCount()):
            self.hideColumn(col)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setMinimumWidth(140)
        self.clicked.connect(self._emit_path)
        self.activated.connect(self._emit_path)

    def set_show_hidden(self, on: bool) -> None:
        flags = (QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot
                 | QDir.Filter.Drives)
        if on:
            flags |= QDir.Filter.Hidden
        self._fs.setFilter(flags)

    def _emit_path(self, index) -> None:
        if index.isValid():
            self.folder_selected.emit(self._fs.filePath(index))

    def select_path(self, path: str) -> None:
        """Programmatic sync (no signal): highlight and reveal a folder."""
        idx = self._fs.index(path)
        if idx.isValid():
            self.blockSignals(True)
            self.setCurrentIndex(idx)
            self.scrollTo(idx)  # expands ancestors
            self.blockSignals(False)
