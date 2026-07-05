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
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from editstack import VIGNETTE_DEFAULTS, EditStack, Op

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

# crop aspect presets: label -> value for ViewerWidget.set_crop_aspect
# (None = free, "original" = the visible frame's ratio, floats follow the
# photo orientation: 3:2 acts as 2:3 on portrait frames)
CROP_ASPECTS = (("Free", None), ("Orig", "original"), ("1:1", 1.0),
                ("3:2", 1.5), ("4:3", 4.0 / 3.0), ("16:9", 16.0 / 9.0))


class StackPanel(QWidget):
    stack_edited = Signal(object, bool)   # (EditStack, final)
    copy_requested = Signal()
    paste_replace_requested = Signal()
    paste_append_requested = Signal()
    apply_last_requested = Signal()
    crop_requested = Signal()             # start interactive crop in the viewer
    crop_aspect_selected = Signal(object)  # aspect value from CROP_ASPECTS
    wb_pick_requested = Signal()          # start the WB eyedropper in the viewer
    vig_pick_requested = Signal()         # start vignette placement in the viewer

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
        self._btn_vig = QPushButton("Vig")
        self._btn_vig.setToolTip(
            "Vignette: click the image to place the center (V) — strength "
            "and size below; Esc cancels")
        self._btn_vig.clicked.connect(self.vig_pick_requested)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setToolTip("Remove every op from the stack")
        self._btn_clear.clicked.connect(self._clear)
        for b in (self._btn_rotate, self._btn_crop, self._btn_wb,
                  self._btn_vig, self._btn_clear):
            ops_row.addWidget(b)
        root.addLayout(ops_row)

        # crop aspect presets: checking one while not cropping also starts
        # the crop (the app wires crop_aspect_selected accordingly)
        aspect_row = QHBoxLayout()
        aspect_row.setSpacing(2)
        self._aspect_group = QButtonGroup(self)
        self._aspect_group.setExclusive(True)
        self._aspect_buttons = {}
        for label, value in CROP_ASPECTS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet("padding: 3px 2px;")  # six across a narrow panel
            b.setMinimumWidth(30)
            b.setToolTip("Crop box ratio — presets follow the photo "
                         "orientation" if value not in (None, "original")
                         else {None: "Freeform crop box",
                               "original": "Keep the photo's own ratio"}[value])
            self._aspect_group.addButton(b)
            self._aspect_buttons[label] = (b, value)
            b.clicked.connect(lambda _=False, v=value:
                              self.crop_aspect_selected.emit(v))
            aspect_row.addWidget(b)
        self._aspect_buttons["Free"][0].setChecked(True)
        root.addLayout(aspect_row)

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

        # vignette sliders; the center comes from the Vig placement mode
        self._vig_sliders = {}
        self._vig_values = {}
        for key, label, lo, hi in (("strength", "Vignette", -100, 100),
                                   ("radius", "Vig size", 10, 200)):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(78)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue(0 if key == "strength" else 100)
            value = QLabel("0" if key == "strength" else "100")
            value.setMinimumWidth(38)
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            slider.valueChanged.connect(
                lambda v, k=key: self._vignette_changed(k, v))
            slider.sliderReleased.connect(self._slider_released)
            row.addWidget(name)
            row.addWidget(slider, 1)
            row.addWidget(value)
            root.addLayout(row)
            self._vig_sliders[key] = slider
            self._vig_values[key] = value

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

    def crop_aspect(self):
        """The currently selected crop-aspect value (see CROP_ASPECTS)."""
        for label, (btn, value) in self._aspect_buttons.items():
            if btn.isChecked():
                return value
        return None

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
        vig = self._stack.last_vignette()
        s = round((vig.params.get("strength", 0.0) if vig else 0.0) * 100)
        r = round((vig.params.get("radius", 1.0) if vig else 1.0) * 100)
        for key, val, neutral in (("strength", s, 0), ("radius", r, 100)):
            self._vig_sliders[key].blockSignals(True)
            self._vig_sliders[key].setValue(val)
            self._vig_sliders[key].blockSignals(False)
            self._vig_values[key].setText(f"{val:+d}" if key == "strength"
                                          and val else str(val))

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

    def _ensure_vignette(self) -> Op:
        vig = self._stack.last_vignette()
        if vig is None:
            params = dict(VIGNETTE_DEFAULTS)
            params["radius"] = self._vig_sliders["radius"].value() / 100.0
            vig = Op("vignette", params)
            self._stack.ops.append(vig)
            self._rebuild_list()
        return vig

    def _vignette_changed(self, key: str, raw: int) -> None:
        if self._loading:
            return
        if key == "radius" and self._stack.last_vignette() is None:
            self._vig_values[key].setText(str(raw))  # size pre-set, no op yet
            return
        vig = self._ensure_vignette()
        vig.params[key] = raw / 100.0
        if key == "strength" and raw == 0 and vig is self._stack.ops[-1]:
            self._stack.ops.pop()          # zero strength = no vignette
            self._rebuild_list()
        else:
            row = self._stack.ops.index(vig) if vig in self._stack.ops else -1
            if 0 <= row < self._list.count():
                self._list.blockSignals(True)
                self._list.item(row).setText(vig.summary())
                self._list.blockSignals(False)
        self._vig_values[key].setText(f"{raw:+d}" if key == "strength"
                                      and raw else str(raw))
        self._emit(final=not self._vig_sliders[key].isSliderDown())

    def set_vignette_center(self, cx: float, cy: float) -> None:
        """Commit path for the viewer's vignette placement click. Placing a
        center with zero strength turns the vignette on at a visible -50."""
        if self._fid is None:
            return
        vig = self._ensure_vignette()
        vig.params["cx"] = round(cx, 4)
        vig.params["cy"] = round(cy, 4)
        if not vig.params.get("strength", 0.0):
            vig.params["strength"] = -0.5
        self._rebuild_list()
        self._load_sliders()
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
