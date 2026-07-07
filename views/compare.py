"""Side-by-side compare view: two ViewerWidgets with mirrored zoom/pan,
for picking the better of two near-identical shots. Owns pane focus (the
accent border), the view sync, and the filename labels; the controller
routes culling keys to focused_entry() and drives image/texture delivery.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from views.viewer import ViewerWidget


class CompareView(QWidget):
    exit_requested = Signal()  # Esc in either pane

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list = []
        self._focus = 0
        self._syncing = False
        self.panes: list[ViewerWidget] = []
        self._frames: list[QFrame] = []
        self._labels: list[QLabel] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for i in range(2):
            pane = ViewerWidget()
            frame = QFrame()
            frame.setObjectName("comparePane")
            flay = QVBoxLayout(frame)
            flay.setContentsMargins(2, 2, 2, 2)
            flay.setSpacing(0)
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("padding: 2px; border: none;")
            flay.addWidget(pane, 1)
            flay.addWidget(label)
            row.addWidget(frame, 1)
            pane.focused.connect(lambda i=i: self.set_focus(i))
            pane.close_requested.connect(self.exit_requested)
            pane.view_changed.connect(lambda i=i: self._sync_view(i))
            self.panes.append(pane)
            self._frames.append(frame)
            self._labels.append(label)

    # ------------------------------------------------------------------ API

    @property
    def entries(self) -> list:
        return self._entries

    def show_pair(self, entries: list, stacks: list) -> None:
        self._entries = list(entries)
        for pane, label, e, stack in zip(self.panes, self._labels,
                                         entries, stacks):
            pane.show_image(e.id, e.width, e.height, stack)
            label.setText(e.name)
        self.set_focus(0)

    def clear(self) -> None:
        self._entries = []

    def focused_entry(self):
        return self._entries[self._focus] if self._entries else None

    def deliver_texture(self, fid: int, image, level: int) -> None:
        for pane in self.panes:
            pane.set_texture_image(fid, image, level)

    def set_focus(self, i: int) -> None:
        self._focus = i
        accent = self.palette().color(QPalette.ColorRole.Highlight).name()
        for j, frame in enumerate(self._frames):
            color = accent if j == i else "transparent"
            frame.setStyleSheet(
                f"QFrame#comparePane {{ border: 2px solid {color}; }}")
        self.panes[i].setFocus()

    # ------------------------------------------------------------- internals

    def _sync_view(self, source: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.panes[1 - source].apply_view_state(
                *self.panes[source].view_state())
        finally:
            self._syncing = False
