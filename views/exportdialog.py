"""Export dialog: destination, composable file-name pattern with token
buttons and live preview, JPEG quality, optional resize. The engine and
the naming tokens live in export.py; this is only the UI."""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSlider, QSpinBox, QVBoxLayout)

from export import (DEFAULT_PATTERN, NAME_TOKENS, ExportItem, ExportOptions,
                    render_name)


class ExportDialog(QDialog):
    def __init__(self, count: int, parent=None,
                 sample: ExportItem | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Export {count} image{'s' if count != 1 else ''}")
        self._sample = sample
        form = QFormLayout(self)

        dest_row = QHBoxLayout()
        self._dest = QLineEdit(os.path.expanduser("~/Pictures/photoflow-export"))
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse)
        dest_row.addWidget(self._dest, 1)
        dest_row.addWidget(browse)
        form.addRow("Destination", dest_row)

        name_col = QVBoxLayout()
        self._pattern = QLineEdit(
            QSettings("photoflow", "photoflow").value("export_pattern",
                                                      DEFAULT_PATTERN))
        self._pattern.setToolTip(
            "Compose the file name from the tokens below plus any literal "
            "text. The extension follows the source format automatically.")
        self._pattern.textChanged.connect(self._update_preview)
        name_col.addWidget(self._pattern)
        tokens = QGridLayout()
        tokens.setSpacing(4)
        for i, (token, label, tip) in enumerate(NAME_TOKENS):
            b = QPushButton(label)
            b.setToolTip(f"{tip}  —  inserts {token}")
            b.clicked.connect(lambda _=False, t=token: self._pattern.insert(t))
            tokens.addWidget(b, i // 4, i % 4)
        name_col.addLayout(tokens)
        self._preview = QLabel()
        muted = self._preview.palette()
        muted.setColor(QPalette.ColorRole.WindowText,
                       muted.color(QPalette.ColorRole.PlaceholderText))
        self._preview.setPalette(muted)
        name_col.addWidget(self._preview)
        form.addRow("File name", name_col)
        self._update_preview()

        q_row = QHBoxLayout()
        self._quality = QSlider(Qt.Orientation.Horizontal)
        self._quality.setRange(60, 100)
        self._quality.setValue(90)
        self._q_label = QLabel("90")
        self._quality.valueChanged.connect(lambda v: self._q_label.setText(str(v)))
        q_row.addWidget(self._quality, 1)
        q_row.addWidget(self._q_label)
        form.addRow("Quality", q_row)

        r_row = QHBoxLayout()
        self._resize_on = QCheckBox("Long edge")
        self._resize_px = QSpinBox()
        self._resize_px.setRange(64, 30000)
        self._resize_px.setValue(2048)
        self._resize_px.setEnabled(False)
        self._resize_on.toggled.connect(self._resize_px.setEnabled)
        r_row.addWidget(self._resize_on)
        r_row.addWidget(self._resize_px, 1)
        form.addRow("Resize", r_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export destination",
                                             self._dest.text())
        if d:
            self._dest.setText(d)

    def _update_preview(self) -> None:
        if self._sample is None:
            self._preview.setText("")
            return
        ext = os.path.splitext(self._sample.path)[1].lower() or ".jpg"
        name = render_name(self._pattern.text(), self._sample) + ext
        self._preview.setText(f"Preview: {name}")

    def options(self) -> ExportOptions:
        pattern = self._pattern.text().strip() or DEFAULT_PATTERN
        QSettings("photoflow", "photoflow").setValue("export_pattern", pattern)
        return ExportOptions(
            dest_dir=self._dest.text().strip(),
            quality=self._quality.value(),
            resize_long=self._resize_px.value() if self._resize_on.isChecked() else None,
            name_pattern=pattern)
