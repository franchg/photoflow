"""Keyboard shortcuts reference (F1 / ? / the Help toolbar button).

SECTIONS is the single in-app source for the key list — keep it in sync
with the shortcuts wired in app.py/viewer.py and the README table.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout,
                               QLabel, QScrollArea, QVBoxLayout, QWidget)

SECTIONS = (
    ("Browse", (
        ("Ctrl+O", "Open folder…"),
        ("Ctrl+R / F5", "Re-scan folder, rebuild its thumbnails"),
        ("Enter / double-click", "Open the viewer; Esc goes back"),
        ("Space / → / ↓", "Next image"),
        ("← / ↑", "Previous image"),
        ("F / F11", "Fullscreen in/out (from the grid: current photo)"),
        ("Del", "Move selection to the system trash"),
    )),
    ("Cull", (
        ("0–5", "Rating (press again to clear)"),
        ("P / X / U", "Pick / reject / unflag"),
    )),
    ("Viewer", (
        ("Z / double-click", "Fit ↔ 100 %"),
        ("Wheel / drag", "Zoom / pan"),
        ("C", "Interactive crop — Enter applies, Esc cancels; aspect "
              "presets (3:2, 16:9, …) are in the edit panel"),
        ("W", "White-balance eyedropper: click a neutral gray — Esc cancels"),
        ("V", "Vignette: click to place the center — strength/size sliders "
              "in the panel"),
        ("Right-click (hold)", "Compare with the original — edits bypassed "
                               "while held"),
    )),
    ("Edit stacks", (
        ("Ctrl+Z / Ctrl+Shift+Z", "Undo / redo edits (per image)"),
        ("Ctrl+Shift+C", "Copy edit stack"),
        ("Ctrl+Shift+V", "Paste edits onto selection (replace)"),
        ("Ctrl+Alt+Shift+V", "Paste edits onto selection (append)"),
        ("Ctrl+L", "Apply last edit op to selection"),
    )),
    ("App", (
        ("Ctrl+E", "Export…"),
        ("Ctrl+,", "Settings…"),
        ("F1 / ?", "This help"),
    )),
)

_KEY_STYLE = ("QLabel { background: palette(alternate-base);"
              " border: 1px solid palette(mid); border-radius: 4px;"
              " padding: 1px 8px; font-family: monospace; }")


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setMinimumSize(560, 560)
        root = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        row = 0
        for title, entries in SECTIONS:
            header = QLabel(f"<b>{title}</b>")
            grid.addWidget(header, row, 0, 1, 2)
            row += 1
            for keys, action in entries:
                key_label = QLabel(keys)
                key_label.setStyleSheet(_KEY_STYLE)
                key_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(key_label, row, 0)
                desc = QLabel(action)
                desc.setWordWrap(True)
                grid.addWidget(desc, row, 1)
                row += 1
            grid.setRowMinimumHeight(row, 10)  # gap between sections
            row += 1
        grid.setRowStretch(row, 1)
        scroll.setWidget(body)
        root.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)
