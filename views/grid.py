"""Thumbnail grid: QListView in IconMode (virtualized for free) plus a
delegate that paints the thumb with rating / flag / edited overlays.
The same view class doubles as the filmstrip under the viewer.

All chrome colors come from the active QPalette so the grid follows the
System / Light / Dark theme; only the semantic overlays (pick green,
reject red, rating amber) are fixed."""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPalette,
                           QPen)
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from models import (EditedRole, FLAG_PICK, FLAG_REJECT, FlagRole, NameRole,
                    RawRole,
                    RatingRole, ThumbRole)

CELL = QSize(196, 218)
FILMSTRIP_CELL = QSize(96, 96)

_PICK = QColor(80, 200, 100)
_REJECT = QColor(225, 70, 60)
_STAR = QColor(230, 180, 50)
_BADGE_BG = QColor(0, 0, 0, 160)
_BADGE_FG = QColor(255, 255, 255)


class ThumbDelegate(QStyledItemDelegate):
    def __init__(self, cell: QSize = CELL, show_label: bool = True, parent=None):
        super().__init__(parent)
        self._cell = cell
        self._show_label = show_label

    def sizeHint(self, option, index) -> QSize:
        return self._cell

    def paint(self, p: QPainter, option, index) -> None:
        r = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        pal = option.palette
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cell_path = QPainterPath()
        cell_path.addRoundedRect(QRectF(r), 8, 8)
        p.fillPath(cell_path, pal.color(QPalette.ColorRole.AlternateBase))
        label_h = 18 if self._show_label else 0
        img_rect = r.adjusted(4, 4, -4, -4 - label_h)

        thumb = index.data(ThumbRole)
        rejected = index.data(FlagRole) == FLAG_REJECT
        if thumb is not None and not thumb.isNull():
            scaled = thumb.size().scaled(img_rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(0, 0, scaled.width(), scaled.height())
            target.moveCenter(img_rect.center())
            if rejected:
                p.setOpacity(0.35)
            p.drawImage(target, thumb)
            p.setOpacity(1.0)
        else:
            p.setPen(QPen(pal.color(QPalette.ColorRole.PlaceholderText)))
            p.drawText(img_rect, Qt.AlignmentFlag.AlignCenter, "…")

        # flag edge (inset so it doesn't poke out of the rounded corners)
        flag = index.data(FlagRole)
        edge = QRectF(r.left() + 1, r.top() + 8, 3.5, r.height() - 16)
        if flag == FLAG_PICK:
            p.fillRect(edge, _PICK)
        elif flag == FLAG_REJECT:
            p.fillRect(edge, _REJECT)
            p.setPen(QPen(_REJECT, 2))
            f = p.font()
            f.setPixelSize(16)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRect(r.right() - 22, r.bottom() - 22 - label_h, 18, 18),
                       Qt.AlignmentFlag.AlignCenter, "✕")

        # rating stars
        rating = index.data(RatingRole) or 0
        if rating:
            p.setPen(QPen(_STAR))
            f = p.font()
            f.setPixelSize(13)
            f.setBold(False)
            p.setFont(f)
            p.drawText(QRect(r.left() + 8, r.bottom() - 20 - label_h, r.width() - 16, 16),
                       Qt.AlignmentFlag.AlignLeft, "★" * rating)

        # edited badge
        if index.data(EditedRole):
            badge = QRect(r.right() - 22, r.top() + 6, 16, 16)
            p.setBrush(_BADGE_BG)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(badge)
            p.setPen(QPen(_BADGE_FG))
            f = p.font()
            f.setPixelSize(11)
            f.setItalic(True)
            f.setBold(True)
            p.setFont(f)
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, "ƒ")

        # RAW chip (the file is a RAW, or a paired RAW rides behind it)
        if index.data(RawRole):
            chip = QRect(r.left() + 6, r.top() + 6, 32, 15)
            p.setBrush(_BADGE_BG)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(chip, 4, 4)
            p.setPen(QPen(_BADGE_FG))
            f = p.font()
            f.setPixelSize(9)
            f.setItalic(False)
            f.setBold(True)
            p.setFont(f)
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, "RAW")

        if self._show_label:
            label = pal.color(QPalette.ColorRole.Text)
            if not selected:
                label.setAlpha(150)
            p.setPen(QPen(label))
            f = QFont(option.font)
            f.setPixelSize(11)
            p.setFont(f)
            name = index.data(NameRole) or ""
            metrics = p.fontMetrics()
            elided = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle,
                                        r.width() - 12)
            p.drawText(QRect(r.left() + 6, r.bottom() - label_h, r.width() - 12, label_h - 2),
                       Qt.AlignmentFlag.AlignCenter, elided)

        if selected:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(pal.color(QPalette.ColorRole.Highlight), 2))
            p.drawRoundedRect(QRectF(r.adjusted(1, 1, -1, -1)), 7, 7)
        p.restore()


class GridView(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(64)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(4)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(40)
        self._delegate = ThumbDelegate(CELL, True, self)
        self.setItemDelegate(self._delegate)
        self.setFrameShape(QListView.Shape.NoFrame)

    def set_cell_size(self, size: QSize) -> None:
        self._delegate._cell = size
        self.setGridSize(size)
        self.doItemsLayout()


class FilmstripView(QListView):
    """Horizontal single-row strip under the viewer; shares the grid's
    model and selection model."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(2)
        self.setFixedHeight(FILMSTRIP_CELL.height() + 22)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setItemDelegate(ThumbDelegate(FILMSTRIP_CELL, False, self))
        self.setFrameShape(QListView.Shape.NoFrame)
