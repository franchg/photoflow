"""Edit stack panel: the ordered op list (enable/disable/remove/reorder),
tune sliders with live GPU preview, rotate/crop entry points, and the
copy/paste buttons.

The panel owns a *working copy* of the current image's stack. Slider drags
emit stack_edited(stack, final=False) for shader-only preview; release (or
any structural change) emits final=True, which the app persists and pushes
to history + thumbnail re-render.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from editstack import EditStack, Op

# Snapseed's Tune Image set, in its order. The stack key for Brightness stays
# "exposure" (schema compat); "hue" remains valid in stacks but has no slider.
PANEL_KEYS = ("exposure", "contrast", "saturation", "ambiance",
              "highlights", "shadows", "temperature", "tint")
SLIDER_LABELS = {
    "exposure": "Brightness",
    "contrast": "Contrast",
    "saturation": "Saturation",
    "ambiance": "Ambiance",
    "highlights": "Highlights",
    "shadows": "Shadows",
    "temperature": "Temperature",
    "tint": "Tint",
}


class StackPanel(QWidget):
    stack_edited = Signal(object, bool)   # (EditStack, final)
    copy_requested = Signal()
    paste_replace_requested = Signal()
    paste_append_requested = Signal()
    apply_last_requested = Signal()
    crop_requested = Signal()             # start interactive crop in the viewer
    wb_pick_requested = Signal()          # start the WB eyedropper in the viewer

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stack = EditStack()
        self._fid: int | None = None
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        root.addWidget(QLabel("<b>Edit stack</b>"))

        ops_row = QHBoxLayout()
        self._btn_rotate = QPushButton("90°")
        self._btn_rotate.setToolTip("Rotate 90° clockwise")
        self._btn_rotate.clicked.connect(self._rotate_cw)
        self._btn_crop = QPushButton("Crop")
        self._btn_crop.setToolTip("Drag a crop on the image (C) — Enter applies, Esc cancels")
        self._btn_crop.clicked.connect(self.crop_requested)
        self._btn_wb = QPushButton("WB")
        self._btn_wb.setToolTip(
            "White balance: click a spot that should be neutral gray (W) — "
            "Esc cancels")
        self._btn_wb.clicked.connect(self.wb_pick_requested)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setToolTip("Remove every op from the stack")
        self._btn_clear.clicked.connect(self._clear)
        for b in (self._btn_rotate, self._btn_crop, self._btn_wb,
                  self._btn_clear):
            ops_row.addWidget(b)
        root.addLayout(ops_row)

        # rotation slider: total stack rotation in 0.1° steps; multiples of
        # 90 keep the lossless-export path, anything else resamples
        rot_row = QHBoxLayout()
        rot_name = QLabel("Rotate")
        rot_name.setMinimumWidth(78)
        self._rot_slider = QSlider(Qt.Orientation.Horizontal)
        self._rot_slider.setRange(-1800, 1800)
        self._rot_slider.setValue(0)
        self._rot_slider.setToolTip(
            "Rotation in degrees — snaps math to lossless at 0/±90/±180")
        self._rot_value = QLabel("0°")
        self._rot_value.setMinimumWidth(44)
        self._rot_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._rot_slider.valueChanged.connect(self._rotation_changed)
        self._rot_slider.sliderReleased.connect(self._slider_released)
        rot_row.addWidget(rot_name)
        rot_row.addWidget(self._rot_slider, 1)
        rot_row.addWidget(self._rot_value)
        root.addLayout(rot_row)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setMaximumHeight(160)
        self._list.itemChanged.connect(self._item_toggled)
        root.addWidget(self._list)

        move_row = QHBoxLayout()
        self._btn_up = QPushButton()
        self._btn_up.setToolTip("Move op up")
        self._btn_up.clicked.connect(lambda: self._move(-1))
        self._btn_down = QPushButton()
        self._btn_down.setToolTip("Move op down")
        self._btn_down.clicked.connect(lambda: self._move(1))
        self._btn_remove = QPushButton("Remove")
        self._btn_remove.setToolTip("Remove the selected op")
        self._btn_remove.clicked.connect(self._remove)
        for b in (self._btn_up, self._btn_down, self._btn_remove):
            move_row.addWidget(b)
        root.addLayout(move_row)

        root.addWidget(QLabel("<b>Tune</b>"))
        self._sliders: dict[str, QSlider] = {}
        self._value_labels: dict[str, QLabel] = {}
        for key in PANEL_KEYS:
            row = QHBoxLayout()
            name = QLabel(SLIDER_LABELS[key])
            name.setMinimumWidth(78)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(-100, 100)
            slider.setValue(0)
            value = QLabel("0")
            value.setMinimumWidth(38)
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            slider.valueChanged.connect(lambda v, k=key: self._slider_changed(k, v))
            slider.sliderReleased.connect(self._slider_released)
            row.addWidget(name)
            row.addWidget(slider, 1)
            row.addWidget(value)
            root.addLayout(row)
            self._sliders[key] = slider
            self._value_labels[key] = value

        self._btn_reset = QPushButton("Reset tune")
        self._btn_reset.clicked.connect(self._reset_tune)
        root.addWidget(self._btn_reset)

        root.addSpacing(8)
        root.addWidget(QLabel("<b>Edits clipboard</b>"))
        clip_row = QHBoxLayout()
        self._btn_copy = QPushButton("Copy")
        self._btn_copy.setToolTip("Copy this stack (Ctrl+Shift+C)")
        self._btn_copy.clicked.connect(self.copy_requested)
        self._btn_paste = QPushButton("Paste")
        self._btn_paste.setToolTip("Replace stacks of selection (Ctrl+Shift+V)")
        self._btn_paste.clicked.connect(self.paste_replace_requested)
        self._btn_paste_add = QPushButton("Paste +")
        self._btn_paste_add.setToolTip(
            "Append onto stacks of selection (Ctrl+Alt+Shift+V)")
        self._btn_paste_add.clicked.connect(self.paste_append_requested)
        for b in (self._btn_copy, self._btn_paste, self._btn_paste_add):
            clip_row.addWidget(b)
        root.addLayout(clip_row)
        self._btn_last = QPushButton("Apply last edit to selection")
        self._btn_last.setToolTip(
            "Copy the newest op of this image onto the selection (Ctrl+L)")
        self._btn_last.clicked.connect(self.apply_last_requested)
        root.addWidget(self._btn_last)

        root.addStretch(1)
        self.setEnabled(False)

    def set_icons(self, icon) -> None:
        """icon: callable(name) -> QIcon, tinted to the active theme."""
        for btn, name in ((self._btn_rotate, "rotate"), (self._btn_crop, "crop"),
                          (self._btn_wb, "pipette"),
                          (self._btn_clear, "trash"), (self._btn_up, "chevron-up"),
                          (self._btn_down, "chevron-down"), (self._btn_remove, "x"),
                          (self._btn_reset, "rotate-ccw"), (self._btn_copy, "copy"),
                          (self._btn_paste, "paste"),
                          (self._btn_paste_add, "paste-plus"),
                          (self._btn_last, "zap")):
            btn.setIcon(icon(name))

    # -- loading -------------------------------------------------------------

    def load(self, fid: int | None, stack: EditStack | None) -> None:
        self._loading = True
        try:
            self._fid = fid
            self._stack = (stack or EditStack()).clone()
            self.setEnabled(fid is not None)
            self._rebuild_list()
            self._load_sliders()
        finally:
            self._loading = False

    def current_stack(self) -> EditStack:
        return self._stack

    @property
    def current_fid(self) -> int | None:
        return self._fid

    def _rebuild_list(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for i, op in enumerate(self._stack.ops):
            item = QListWidgetItem(op.summary())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if op.enabled
                               else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _load_sliders(self) -> None:
        tune = self._stack.last_tune()
        for key in PANEL_KEYS:
            v = (tune.params.get(key, 0.0) if tune else 0.0)
            self._sliders[key].blockSignals(True)
            self._sliders[key].setValue(round(v * 100))
            self._sliders[key].blockSignals(False)
            n = round(v * 100)
            self._value_labels[key].setText(f"{n:+d}" if n else "0")
        deg = self._stack.total_rotation()
        self._rot_slider.blockSignals(True)
        self._rot_slider.setValue(round(deg * 10))
        self._rot_slider.blockSignals(False)
        self._rot_value.setText(f"{deg:+g}°" if deg else "0°")

    # -- edits ------------------------------------------------------------------

    def _emit(self, final: bool) -> None:
        if not self._loading and self._fid is not None:
            self.stack_edited.emit(self._stack, final)

    def _ensure_tune(self) -> Op:
        tune = self._stack.last_tune()
        if tune is None:
            tune = Op("tune", {})
            self._stack.ops.append(tune)
            self._rebuild_list()
        return tune

    def _slider_changed(self, key: str, raw: int) -> None:
        if self._loading:
            return
        tune = self._ensure_tune()
        v = raw / 100.0
        if v:
            tune.params[key] = v
        else:
            tune.params.pop(key, None)
        self._value_labels[key].setText(f"{raw:+d}" if raw else "0")
        row = self._stack.ops.index(tune)
        if row < self._list.count():
            self._list.blockSignals(True)
            self._list.item(row).setText(tune.summary())
            self._list.blockSignals(False)
        # Live preview; final commit happens on release (or on click-set,
        # where isSliderDown() is False and no release event will come).
        self._emit(final=not self._sliders[key].isSliderDown())

    def _slider_released(self) -> None:
        self._emit(final=True)

    def _rotation_changed(self, raw: int) -> None:
        if self._loading:
            return
        deg = raw / 10.0
        self._stack.set_rotation(deg)
        self._rot_value.setText(f"{deg:+g}°" if deg else "0°")
        self._rebuild_list()
        self._emit(final=not self._rot_slider.isSliderDown())

    def _reset_tune(self) -> None:
        changed = False
        for op in self._stack.ops:
            if op.op == "tune" and op.params:
                op.params.clear()
                changed = True
        self._stack.ops = [op for op in self._stack.ops
                           if not (op.op == "tune" and not op.params)]
        if changed:
            self._rebuild_list()
            self._load_sliders()
            self._emit(final=True)

    def _rotate_cw(self) -> None:
        self._stack.add_rotation(90)
        self._rebuild_list()
        self._load_sliders()
        self._emit(final=True)

    def append_crop(self, rect: list[float]) -> None:
        """Commit path for the viewer's interactive crop."""
        if self._fid is None:
            return
        self._stack.append(Op("crop", {"rect": list(rect)}))
        self._rebuild_list()
        self._emit(final=True)

    def apply_wb(self, temperature: float, tint: float) -> None:
        """Commit path for the viewer's WB eyedropper. The solve is for the
        whole render, so the edited op is set to whatever makes the *folded*
        temperature/tint land on the solved values."""
        if self._fid is None:
            return
        tune = self._ensure_tune()
        folded = self._stack.folded_tune()
        for key, target in (("temperature", temperature), ("tint", tint)):
            others = getattr(folded, key) - tune.params.get(key, 0.0)
            v = max(-1.0, min(1.0, round(target - others, 4)))
            if v:
                tune.params[key] = v
            else:
                tune.params.pop(key, None)
        self._rebuild_list()
        self._load_sliders()
        self._emit(final=True)

    def _clear(self) -> None:
        if self._stack.ops:
            self._stack.ops.clear()
            self._rebuild_list()
            self._load_sliders()
            self._emit(final=True)

    def _selected_row(self) -> int:
        items = self._list.selectedItems()
        return self._list.row(items[0]) if items else -1

    def _remove(self) -> None:
        row = self._selected_row()
        if 0 <= row < len(self._stack.ops):
            del self._stack.ops[row]
            self._rebuild_list()
            self._load_sliders()
            self._emit(final=True)

    def _move(self, delta: int) -> None:
        row = self._selected_row()
        new = row + delta
        if 0 <= row < len(self._stack.ops) and 0 <= new < len(self._stack.ops):
            ops = self._stack.ops
            ops[row], ops[new] = ops[new], ops[row]
            self._rebuild_list()
            self._list.setCurrentRow(new)
            self._emit(final=True)

    def _item_toggled(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        row = self._list.row(item)
        if 0 <= row < len(self._stack.ops):
            self._stack.ops[row].enabled = (
                item.checkState() == Qt.CheckState.Checked)
            self._load_sliders()
            self._emit(final=True)
