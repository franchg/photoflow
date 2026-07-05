"""Settings dialog: appearance, browsing, and catalog options.

Theme / hidden-files / catalog-path changes are collected and applied by the
app on OK. "Empty catalog" is destructive-adjacent (it wipes edit stacks,
ratings and thumbnail caches — never source files), so it acts immediately
behind its own big warning, via a callback the app provides.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout)

THEME_LABELS = ["System", "Light", "Dark"]
THEME_MODES = ["system", "light", "dark"]
SCALE_LABELS = ["90 %", "100 %", "110 %", "125 %", "150 %"]
SCALE_VALUES = [0.9, 1.0, 1.1, 1.25, 1.5]


class SettingsDialog(QDialog):
    def __init__(self, parent, *, theme: str, show_hidden: bool,
                 catalog_path: str, on_empty_catalog, ui_scale: float = 1.0,
                 on_clear_thumbs=None, on_remove_missing=None,
                 on_set_default=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self._on_empty = on_empty_catalog
        root = QVBoxLayout(self)

        appearance = QGroupBox("Appearance")
        form = QFormLayout(appearance)
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        theme_row = QHBoxLayout()
        theme_row.setSpacing(0)
        for i, label in enumerate(THEME_LABELS):
            b = QPushButton(label)
            b.setCheckable(True)
            self._theme_group.addButton(b, i)
            theme_row.addWidget(b)
        current = THEME_MODES.index(theme) if theme in THEME_MODES else 0
        self._theme_group.button(current).setChecked(True)
        form.addRow("Theme", theme_row)

        self._scale_group = QButtonGroup(self)
        self._scale_group.setExclusive(True)
        scale_row = QHBoxLayout()
        scale_row.setSpacing(0)
        for i, label in enumerate(SCALE_LABELS):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip("Scales the whole UI on top of the system DPI "
                         "scaling — applies on restart")
            self._scale_group.addButton(b, i)
            scale_row.addWidget(b)
        nearest = min(range(len(SCALE_VALUES)),
                      key=lambda i: abs(SCALE_VALUES[i] - ui_scale))
        self._scale_group.button(nearest).setChecked(True)
        form.addRow("UI scale", scale_row)
        root.addWidget(appearance)

        browsing = QGroupBox("Browsing")
        blay = QVBoxLayout(browsing)
        self._hidden = QCheckBox("Show hidden files and folders")
        self._hidden.setChecked(show_hidden)
        self._hidden.setToolTip(
            "Applies to the folder tree and to which JPEGs a folder scan picks up")
        blay.addWidget(self._hidden)
        root.addWidget(browsing)

        if on_set_default is not None:  # Linux only: xdg-mime association
            assoc = QGroupBox("File associations")
            alay = QVBoxLayout(assoc)
            default_btn = QPushButton("Make photoflow the default viewer "
                                      "for JPEG and PNG")
            default_btn.setToolTip(
                "Registers the launcher entry and sets photoflow as the "
                "xdg default handler — double-clicking an image opens it "
                "fullscreen in photoflow")
            default_btn.clicked.connect(on_set_default)
            alay.addWidget(default_btn, alignment=Qt.AlignmentFlag.AlignLeft)
            root.addWidget(assoc)

        catalog = QGroupBox("Catalog")
        clay = QVBoxLayout(catalog)
        path_row = QHBoxLayout()
        self._path = QLineEdit(catalog_path)
        self._path.setReadOnly(True)
        change = QPushButton("Change…")
        change.clicked.connect(self._pick_path)
        path_row.addWidget(self._path, 1)
        path_row.addWidget(change)
        clay.addLayout(path_row)
        note = QLabel("Changing the location opens (or creates) a catalog at "
                      "the new path. Existing data is not moved.")
        note.setWordWrap(True)
        # palette(mid) is near-invisible on light themes; PlaceholderText is
        # the real muted-text role but QSS palette() can't reference it.
        muted = note.palette()
        muted.setColor(QPalette.ColorRole.WindowText,
                       muted.color(QPalette.ColorRole.PlaceholderText))
        note.setPalette(muted)
        clay.addWidget(note)

        maint_row = QHBoxLayout()
        clear_thumbs = QPushButton("Clear thumbnail cache")
        clear_thumbs.setToolTip(
            "Deletes every cached thumbnail and compacts the catalog file. "
            "Edits, ratings and flags are kept; thumbnails regenerate as "
            "folders are browsed.")
        if on_clear_thumbs is not None:
            clear_thumbs.clicked.connect(on_clear_thumbs)
        missing_btn = QPushButton("Remove missing files…")
        missing_btn.setToolTip(
            "Scans the catalog and removes entries whose file was moved or "
            "deleted — including their edits, ratings and flags")
        if on_remove_missing is not None:
            missing_btn.clicked.connect(on_remove_missing)
        maint_row.addWidget(clear_thumbs)
        maint_row.addWidget(missing_btn)
        maint_row.addStretch(1)
        clay.addLayout(maint_row)

        empty = QPushButton("Empty catalog…")
        empty.setStyleSheet("QPushButton { color: #d33c30; font-weight: bold; }")
        empty.clicked.connect(self._confirm_empty)
        clay.addWidget(empty, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(catalog)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _pick_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Catalog database", self._path.text(),
            "SQLite database (*.db)",
            options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            if not os.path.splitext(path)[1]:
                path += ".db"
            self._path.setText(path)

    def _confirm_empty(self) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, "Empty catalog",
                          "<b>This permanently deletes ALL edit stacks, "
                          "ratings, flags and cached thumbnails — for every "
                          "photo the catalog has ever seen.</b>",
                          QMessageBox.StandardButton.Cancel, self)
        box.setInformativeText("Source JPEG files are not touched.\n"
                               "This cannot be undone.")
        wipe = box.addButton("Delete everything",
                             QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is wipe:
            self._on_empty()
            QMessageBox.information(self, "Empty catalog", "Catalog emptied.")

    def values(self) -> dict:
        return {
            "theme": THEME_MODES[max(0, self._theme_group.checkedId())],
            "ui_scale": SCALE_VALUES[max(0, self._scale_group.checkedId())],
            "show_hidden": self._hidden.isChecked(),
            "catalog_path": self._path.text().strip(),
        }
