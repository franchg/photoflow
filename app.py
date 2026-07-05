"""photoflow entry point: QApplication + MainWindow wiring.

The window owns the controller logic: it connects worker signals to the
model, routes culling keys, mediates between the stack panel / viewer /
catalog, and drives copy/paste + export. Heavy work never runs here.
"""
from __future__ import annotations

import copy
import math
import os
import subprocess
import sys
import time
from collections import OrderedDict

from PySide6.QtCore import QFile, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QKeySequence, QPalette, QShortcut,
                           QSurfaceFormat)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QProgressDialog, QPushButton, QSplitter,
                               QStackedWidget, QStatusBar, QToolBar,
                               QVBoxLayout, QWidget)

import styles
from catalog import Catalog
from decode import SCAN_EXTENSIONS
from editstack import EditClipboard, EditStack, StackError, StackHistory
from render import solve_white_balance
from export import ExportDialog, Exporter, ExportItem
from models import (FLAG_NONE, FLAG_PICK, FLAG_REJECT, FileListModel,
                    FilterProxy, IdRole, SORT_DATE, SORT_NAME)
from views.foldertree import FolderTree
from views.grid import FilmstripView, GridView
from views.settingsdialog import SettingsDialog
from views.stackpanel import StackPanel
from views.viewer import ViewerWidget, default_gl_format
from workers import VIEWER_FIT, WorkerHub, Workers

PAGE_GRID, PAGE_VIEWER = 0, 1
VIEWER_CACHE_SIZE = 10
PREFETCH_NEIGHBORS = 2

GRID_SIZES = (QSize(140, 162), QSize(196, 218), QSize(264, 286))
GRID_SIZE_ICONS = ("grid-small", "grid-medium", "grid-large")
GRID_SIZE_TIPS = ("Small thumbnails", "Medium thumbnails", "Large thumbnails")


def _fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


def _entry_stack(entry) -> EditStack:
    try:
        return EditStack.from_json(entry.stack_json)
    except StackError:
        return EditStack()


class MainWindow(QMainWindow):
    def __init__(self, initial_path: str | None = None):
        super().__init__()
        self.setWindowTitle("photoflow")
        self.resize(1440, 900)

        self.settings = QSettings("photoflow", "photoflow")
        self.catalog = Catalog(self.settings.value("catalog_path") or None)
        self.hub = WorkerHub(self)
        self.workers = Workers(self.catalog, self.hub)
        self.clipboard = EditClipboard()
        self.history = StackHistory()
        self.exporter = Exporter(self)
        self._viewer_cache: OrderedDict[int, tuple] = OrderedDict()  # fid -> (QImage, level)
        self._progress: QProgressDialog | None = None
        self._fullscreen = False
        self._fs_prev_page = PAGE_GRID
        self._fs_was_maximized = False
        self._current_folder: str | None = None
        self._pending_open_file: str | None = None  # CLI image → fullscreen

        # -- models ---------------------------------------------------------
        self.model = FileListModel(self)
        self.model.thumb_requester = self.workers.request_thumb
        self.proxy = FilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.sort(0, Qt.SortOrder.AscendingOrder)

        # -- central: grid page + viewer page --------------------------------
        self.grid = GridView()
        self.grid.setModel(self.proxy)
        self.grid.doubleClicked.connect(lambda _: self._open_viewer())

        self.viewer = ViewerWidget()
        self.filmstrip = FilmstripView()
        self.filmstrip.setModel(self.proxy)
        self.filmstrip.setSelectionModel(self.grid.selectionModel())

        viewer_page = QWidget()
        vlay = QVBoxLayout(viewer_page)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(self.viewer, 1)
        vlay.addWidget(self.filmstrip)

        self.stacked = QStackedWidget()
        self.stacked.addWidget(self.grid)
        self.stacked.addWidget(viewer_page)

        self.panel = StackPanel()
        self.tree = FolderTree()
        self.tree.set_show_hidden(self._show_hidden_setting())
        self.tree.folder_selected.connect(self._on_tree_folder)
        split = QSplitter()
        split.setHandleWidth(7)  # 1px visible line (QSS margins), 7px grab area
        split.addWidget(self.tree)
        split.addWidget(self.stacked)
        split.addWidget(self.panel)
        split.setStretchFactor(1, 1)
        split.setSizes([210, 940, 290])
        self.setCentralWidget(split)

        self._build_toolbar()
        self.setStatusBar(QStatusBar())
        self._count_label = QLabel()
        self.statusBar().addPermanentWidget(self._count_label)

        # -- wiring ------------------------------------------------------------
        self.hub.scan_done.connect(self._on_scan_done)
        self.hub.scan_failed.connect(self._on_scan_failed)
        self.hub.thumb_ready.connect(self._on_thumb_ready)
        self.hub.meta_ready.connect(self._on_meta_ready)
        self.hub.viewer_ready.connect(self._on_viewer_image)

        sel = self.grid.selectionModel()
        sel.currentChanged.connect(self._on_current_changed)
        sel.selectionChanged.connect(lambda *_: self._update_count())
        self.proxy.filtersChanged.connect(self._update_count)
        self.proxy.rowsInserted.connect(lambda *_: self._update_count())
        self.proxy.rowsRemoved.connect(lambda *_: self._update_count())

        self.viewer.nav_requested.connect(self._navigate)
        self.viewer.close_requested.connect(self._close_viewer)
        self.viewer.needs_full_res.connect(self._request_full_res)
        self.viewer.crop_committed.connect(self._commit_crop)
        self.viewer.wb_picked.connect(self._commit_wb_pick)
        self.viewer.wb_pick_canceled.connect(
            lambda: self.statusBar().clearMessage())
        self.viewer.crop_canceled.connect(
            lambda: self.statusBar().clearMessage())

        self.panel.stack_edited.connect(self._on_stack_edited)
        self.panel.crop_requested.connect(self._start_crop)
        self.panel.wb_pick_requested.connect(self._start_wb_pick)
        self.panel.copy_requested.connect(self._copy_edits)
        self.panel.paste_replace_requested.connect(lambda: self._paste_edits(False))
        self.panel.paste_append_requested.connect(lambda: self._paste_edits(True))
        self.panel.apply_last_requested.connect(self._apply_last_edit)

        self.exporter.progress.connect(self._on_export_progress)
        self.exporter.finished.connect(self._on_export_finished)

        self._build_shortcuts()
        self._refresh_icons()

        if initial_path:
            QTimer.singleShot(0, lambda: self.open_path(initial_path))
        else:
            folder = self.settings.value("last_folder")
            if folder and os.path.isdir(folder):
                QTimer.singleShot(0, lambda: self._scan(folder))
            elif folder:
                # the folder vanished since last run — a startup popup (or
                # worse, one per launch since _scan re-persists) helps nobody
                self.settings.remove("last_folder")

    def _refresh_icons(self) -> None:
        """(Re)tint all icons to the active theme's text color."""
        color = self.palette().color(QPalette.ColorRole.WindowText)
        for action, name in self._icon_actions:
            action.setIcon(styles.themed_icon(name, color))
        for i, name in enumerate(GRID_SIZE_ICONS):
            self._size_group.button(i).setIcon(styles.themed_icon(name, color))
        self.panel.set_icons(lambda name: styles.themed_icon(name, color))

    def _set_grid_size(self, index: int) -> None:
        self.settings.setValue("grid_size", index)
        self.grid.set_cell_size(GRID_SIZES[index])
        idx = self.grid.selectionModel().currentIndex()
        if idx.isValid():
            self.grid.scrollTo(idx)

    # ------------------------------------------------------------ UI setup

    def _build_toolbar(self) -> None:
        tb = self._toolbar = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        self._folders_action = QAction("Folders", self)
        self._folders_action.setCheckable(True)
        show_tree = self.settings.value("folders_visible", True, type=bool)
        self._folders_action.setChecked(show_tree)
        self.tree.setVisible(show_tree)
        self._folders_action.toggled.connect(self._toggle_folder_tree)
        tb.addAction(self._folders_action)

        act_open = QAction("Open folder…", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self._pick_folder)
        tb.addAction(act_open)
        tb.addSeparator()

        self._size_group = QButtonGroup(self)
        self._size_group.setExclusive(True)
        saved_size = int(self.settings.value("grid_size", 1))
        saved_size = saved_size if 0 <= saved_size < len(GRID_SIZES) else 1
        for i, tip in enumerate(GRID_SIZE_TIPS):
            b = QPushButton()
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setFixedSize(34, 30)
            self._size_group.addButton(b, i)
            tb.addWidget(b)
        self._size_group.button(saved_size).setChecked(True)
        self._size_group.idClicked.connect(self._set_grid_size)
        self.grid.set_cell_size(GRID_SIZES[saved_size])
        tb.addSeparator()

        tb.addWidget(QLabel(" Sort "))
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Name", "Capture date"])
        self._sort_combo.currentIndexChanged.connect(
            lambda i: self.proxy.set_sort_key(SORT_DATE if i else SORT_NAME))
        tb.addWidget(self._sort_combo)

        tb.addWidget(QLabel("  Rating "))
        self._rating_combo = QComboBox()
        self._rating_combo.addItems(["All", "★+", "★★+", "★★★+", "★★★★+", "★★★★★"])
        self._rating_combo.currentIndexChanged.connect(
            lambda i: self.proxy.set_filters(min_rating=i))
        tb.addWidget(self._rating_combo)

        tb.addWidget(QLabel("  Flag "))
        self._flag_combo = QComboBox()
        self._flag_combo.addItems(["All", "Picks", "Rejects", "Unflagged"])
        self._flag_combo.currentIndexChanged.connect(
            lambda i: self.proxy.set_filters(flag_filter=i))
        tb.addWidget(self._flag_combo)

        self._edited_check = QCheckBox(" Edited only")
        self._edited_check.toggled.connect(
            lambda on: self.proxy.set_filters(edited_only=on))
        tb.addWidget(self._edited_check)

        tb.addSeparator()

        act_export = QAction("Export…", self)
        act_export.setShortcut("Ctrl+E")
        act_export.triggered.connect(self._export)
        tb.addAction(act_export)

        act_settings = QAction("Settings…", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self._open_settings)
        tb.addAction(act_settings)

        self._icon_actions = [(act_open, "folder"),
                              (self._folders_action, "sidebar"),
                              (act_export, "export"),
                              (act_settings, "settings")]

    def _build_shortcuts(self) -> None:
        for n in range(6):
            QShortcut(QKeySequence(str(n)), self,
                      activated=lambda n=n: self._rate(n))
        QShortcut(QKeySequence("P"), self, activated=lambda: self._flag(FLAG_PICK))
        QShortcut(QKeySequence("X"), self, activated=lambda: self._flag(FLAG_REJECT))
        QShortcut(QKeySequence("U"), self, activated=lambda: self._flag(FLAG_NONE))
        QShortcut(QKeySequence(Qt.Key.Key_Return), self.grid,
                  context=Qt.ShortcutContext.WidgetShortcut,
                  activated=self._open_viewer)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self.grid,
                  context=Qt.ShortcutContext.WidgetShortcut,
                  activated=self._open_viewer)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self._copy_edits)
        QShortcut(QKeySequence("Ctrl+Shift+V"), self,
                  activated=lambda: self._paste_edits(False))
        QShortcut(QKeySequence("Ctrl+Alt+Shift+V"), self,
                  activated=lambda: self._paste_edits(True))
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._apply_last_edit)
        QShortcut(QKeySequence("C"), self, activated=self._start_crop)
        QShortcut(QKeySequence("W"), self, activated=self._start_wb_pick)
        QShortcut(QKeySequence("F"), self, activated=self._toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_F11), self,
                  activated=self._toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._force_rescan)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=self._force_rescan)
        # Del = move to trash, scoped to the photo views so it can never fire
        # while the edit panel (op list, sliders) has focus.
        for w in (self.grid, self.filmstrip, self.viewer):
            QShortcut(QKeySequence(Qt.Key.Key_Delete), w,
                      context=Qt.ShortcutContext.WidgetShortcut,
                      activated=self._delete_selected)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self._undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self._redo)

    # ------------------------------------------------------------------ settings

    def _show_hidden_setting(self) -> bool:
        return self.settings.value("show_hidden", False, type=bool)

    def _clear_thumb_cache(self, interactive: bool = True) -> None:
        """Settings: drop every cached thumbnail (edits/ratings/flags stay)."""
        before = os.path.getsize(self.catalog.db_path)
        fut = self.catalog.clear_thumbs()
        busy = QProgressDialog("Clearing thumbnail cache…", "", 0, 0, self)
        busy.setWindowTitle("Thumbnail cache")
        busy.setCancelButton(None)  # a queued VACUUM can't be interrupted
        busy.setWindowModality(Qt.WindowModality.ApplicationModal)
        busy.setMinimumDuration(0)
        while not fut.done():
            QApplication.processEvents()
            time.sleep(0.02)
        busy.close()
        freed = max(0, before - os.path.getsize(self.catalog.db_path)) / 1e6
        msg = (f"Thumbnail cache cleared — {freed:.1f} MB reclaimed. "
               "Thumbnails regenerate as folders are browsed.")
        if interactive:
            QMessageBox.information(self, "Thumbnail cache", msg)
        else:
            self.statusBar().showMessage(msg, 6000)

    def _remove_missing_entries(self, interactive: bool = True) -> int:
        """Settings: purge catalog rows whose file no longer exists on disk
        (moved or deleted) — their edits, ratings and flags go with them."""
        rows = self.catalog.all_files()
        progress = QProgressDialog(
            f"Checking {len(rows)} cataloged files…", "Cancel",
            0, len(rows), self)
        progress.setWindowTitle("Remove missing files")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        missing: list[int] = []
        for i, (fid, path) in enumerate(rows):
            if i % 64 == 0:
                progress.setValue(i)
                QApplication.processEvents()
                if progress.wasCanceled():
                    progress.close()
                    return 0  # nothing was removed
            if not os.path.exists(path):
                missing.append(fid)
        progress.setValue(len(rows))
        progress.close()
        if not missing:
            if interactive:
                QMessageBox.information(
                    self, "Remove missing files",
                    f"All {len(rows)} cataloged files exist on disk.")
            return 0
        if interactive:
            resp = QMessageBox.question(
                self, "Remove missing files",
                f"{len(missing)} of {len(rows)} cataloged files no longer "
                "exist on disk.\nRemove their catalog entries? Any edits, "
                "ratings and flags they had are deleted with them.")
            if resp != QMessageBox.StandardButton.Yes:
                return 0
        self.catalog.remove_files(missing).result()
        if self._current_folder:
            self._scan(self._current_folder)
        self.statusBar().showMessage(
            f"Removed {len(missing)} missing files from the catalog", 6000)
        return len(missing)

    def _make_default_viewer(self) -> None:
        """Register the desktop entry (dev runs too) and make it the system
        default handler for JPEG and PNG via xdg-mime."""
        try:
            _register_linux_desktop(force=True)
            result = subprocess.run(
                ["xdg-mime", "default", "photoflow.desktop",
                 "image/jpeg", "image/png"],
                capture_output=True, text=True)
        except OSError as e:
            QMessageBox.warning(self, "Default viewer", str(e))
            return
        if result.returncode == 0:
            self.statusBar().showMessage(
                "photoflow is now the default viewer for JPEG and PNG", 6000)
        else:
            QMessageBox.warning(self, "Default viewer",
                                result.stderr.strip() or "xdg-mime failed")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(
            self,
            theme=str(self.settings.value("theme", "system")),
            show_hidden=self._show_hidden_setting(),
            catalog_path=self.catalog.db_path,
            on_empty_catalog=self._empty_catalog,
            on_clear_thumbs=self._clear_thumb_cache,
            on_remove_missing=self._remove_missing_entries,
            on_set_default=(self._make_default_viewer
                            if sys.platform == "linux" else None))
        if dlg.exec() != SettingsDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if v["theme"] != self.settings.value("theme", "system"):
            self.settings.setValue("theme", v["theme"])
            apply_theme(QApplication.instance(), v["theme"])
            self._refresh_icons()
        if v["show_hidden"] != self._show_hidden_setting():
            self.settings.setValue("show_hidden", v["show_hidden"])
            self.tree.set_show_hidden(v["show_hidden"])
            if self._current_folder:
                self._scan(self._current_folder)
        if v["catalog_path"] and v["catalog_path"] != self.catalog.db_path:
            self._change_catalog_path(v["catalog_path"])

    def _change_catalog_path(self, path: str) -> None:
        self.workers.bump_generation()  # invalidate in-flight jobs
        old = self.catalog
        try:
            self.catalog = Catalog(path)
        except Exception as e:
            self.catalog = old
            QMessageBox.warning(self, "Catalog",
                                f"Could not open catalog at {path}:\n{e}")
            return
        self.workers.catalog = self.catalog
        old.close()
        self.settings.setValue("catalog_path", path)
        self.history = StackHistory()
        if self._current_folder:
            self._scan(self._current_folder)
        else:
            self.model.clear()
        self.statusBar().showMessage(f"Catalog: {path}", 5000)

    def _empty_catalog(self) -> None:
        self.catalog.clear_all()
        self.history = StackHistory()
        # The writer queue is FIFO, so the rescan's ingest lands after the wipe.
        if self._current_folder:
            self._scan(self._current_folder)
        else:
            self.model.clear()

    # -------------------------------------------------------------- folder scan

    def _pick_folder(self) -> None:
        start = self.settings.value("last_folder") or ""
        folder = QFileDialog.getExistingDirectory(self, "Open folder", start)
        if folder:
            self._scan(folder)

    def _toggle_folder_tree(self, on: bool) -> None:
        self.tree.setVisible(on and not self._fullscreen)
        self.settings.setValue("folders_visible", on)

    def _on_tree_folder(self, folder: str) -> None:
        if folder != self._current_folder:
            self._scan(folder)

    def open_path(self, path: str) -> None:
        """CLI / file-manager entry (Exec=%F): a folder opens in the grid,
        an image opens its parent folder and goes fullscreen on the image."""
        path = os.path.abspath(path)
        if os.path.isdir(path):
            self._scan(path)
        elif (os.path.isfile(path)
              and os.path.splitext(path)[1].lower() in SCAN_EXTENSIONS):
            self._pending_open_file = path
            self._scan(os.path.dirname(path))

    def _scan(self, folder: str) -> None:
        self._current_folder = folder
        self.settings.setValue("last_folder", folder)
        self.setWindowTitle(f"photoflow — {folder}")
        self.statusBar().showMessage(f"Scanning {folder}…")
        self.tree.select_path(folder)
        self._viewer_cache.clear()
        self.model.clear()
        self.workers.scan_folder(folder, self._show_hidden_setting())

    def _force_rescan(self) -> None:
        """Ctrl+R: re-scan the current folder and rebuild all its thumbnails."""
        if not self._current_folder:
            return
        ids = [e.id for e in self.model.entries()]
        if ids:
            self.catalog.drop_thumbs(ids)  # queued before the rescan's ingest
        self.statusBar().showMessage("Re-scanning, rebuilding thumbnails…", 4000)
        self._scan(self._current_folder)

    def _on_scan_done(self, gen: int, entries: list) -> None:
        if gen != self.workers.generation:
            return
        self.model.set_entries(entries)
        self.statusBar().showMessage(f"{len(entries)} photos", 4000)
        self._update_count()
        if self.proxy.rowCount():
            self.grid.setCurrentIndex(self.proxy.index(0, 0))
        self.grid.setFocus()
        pending, self._pending_open_file = self._pending_open_file, None
        if pending:
            for row in range(self.proxy.rowCount()):
                idx = self.proxy.index(row, 0)
                e = self.model.entries()[self.proxy.mapToSource(idx).row()]
                if e.path == pending:
                    self.grid.setCurrentIndex(idx)
                    self._enter_fullscreen()
                    break

    def _on_scan_failed(self, gen: int, message: str) -> None:
        if gen != self.workers.generation:
            return
        # Non-modal: a failed scan must never block the window from showing.
        self.statusBar().showMessage(f"Scan failed: {message}", 8000)
        if self._current_folder and not os.path.isdir(self._current_folder):
            if self.settings.value("last_folder") == self._current_folder:
                self.settings.remove("last_folder")
            self._current_folder = None
            self.setWindowTitle("photoflow")

    def _update_count(self) -> None:
        """Bridge-style: 'N items, M hidden, X selected - ZZ.Z MB'."""
        total = self.model.rowCount()
        shown = self.proxy.rowCount()
        hidden = total - shown
        selected = [self.model.entry_by_id(i.data(IdRole))
                    for i in self.grid.selectionModel().selectedRows()]
        selected = [e for e in selected if e]

        parts = [f"{total} item{'s' if total != 1 else ''}"]
        if hidden:
            parts.append(f"{hidden} hidden")
        if selected:
            parts.append(f"{len(selected)} selected")
            size = sum(e.size for e in selected)
        elif shown == total:
            size = sum(e.size for e in self.model.entries())
        else:
            size = sum(e.size for e in
                       (self.model.entry_by_id(self.proxy.index(r, 0).data(IdRole))
                        for r in range(shown)) if e)
        text = ", ".join(parts)
        if size:
            text += f" - {_fmt_size(size)}"
        self._count_label.setText(text)

    # ------------------------------------------------------- worker deliveries

    def _on_thumb_ready(self, gen: int, fid: int, image, edited: bool) -> None:
        if gen == self.workers.generation:
            self.model.update_thumb(fid, image)

    def _on_meta_ready(self, gen: int, fid: int, w: int, h: int,
                       orientation: int, capture_dt) -> None:
        if gen == self.workers.generation:
            self.model.update_meta(fid, w, h, orientation, capture_dt)

    def _on_viewer_image(self, gen: int, fid: int, image, level: int) -> None:
        if gen != self.workers.generation:
            return
        cached = self._viewer_cache.get(fid)
        if level >= VIEWER_FIT and (
                cached is None or level > cached[1]
                or (level == cached[1] and image.width() > cached[0].width())):
            self._viewer_cache[fid] = (image, level)
            self._viewer_cache.move_to_end(fid)
            while len(self._viewer_cache) > VIEWER_CACHE_SIZE:
                self._viewer_cache.popitem(last=False)
        self.viewer.set_texture_image(fid, image, level)

    # --------------------------------------------------------------- selection

    def _current_entry(self):
        idx = self.grid.selectionModel().currentIndex()
        if not idx.isValid():
            return None
        return self.model.entry_by_id(idx.data(IdRole))

    def _selected_entries(self) -> list:
        rows = self.grid.selectionModel().selectedRows()
        entries = [self.model.entry_by_id(i.data(IdRole)) for i in rows]
        entries = [e for e in entries if e]
        if not entries:
            e = self._current_entry()
            entries = [e] if e else []
        return entries

    def _on_current_changed(self, current, _previous) -> None:
        entry = self._current_entry()
        if entry is None:
            self.panel.load(None, None)
            return
        stack = _entry_stack(entry)
        self.history.seed(entry.id, stack)
        self.panel.load(entry.id, stack)
        if self.stacked.currentIndex() == PAGE_VIEWER:
            self._show_in_viewer(entry)

    # ------------------------------------------------------------------ viewer

    def _open_viewer(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        self.stacked.setCurrentIndex(PAGE_VIEWER)
        self.viewer.setFocus()
        self._show_in_viewer(entry)

    def _close_viewer(self) -> None:
        if self._fullscreen:  # Esc backs out of fullscreen first
            self._exit_fullscreen()
            return
        self.stacked.setCurrentIndex(PAGE_GRID)
        self.grid.setFocus()
        idx = self.grid.selectionModel().currentIndex()
        if idx.isValid():
            self.grid.scrollTo(idx)

    # -------------------------------------------------------------- fullscreen

    def _toggle_fullscreen(self) -> None:
        if self._fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        self._fullscreen = True
        self._fs_prev_page = self.stacked.currentIndex()
        self._fs_was_maximized = self.isMaximized()
        if self.stacked.currentIndex() != PAGE_VIEWER:
            self.stacked.setCurrentIndex(PAGE_VIEWER)
            self._show_in_viewer(entry)
        self._toolbar.setVisible(False)
        self.statusBar().setVisible(False)
        self.panel.setVisible(False)
        self.filmstrip.setVisible(False)
        self.tree.setVisible(False)
        self.showFullScreen()
        self.viewer.setFocus()
        # The viewport grew: re-request a fit decode once the resize landed.
        QTimer.singleShot(150, self._refresh_viewer_res)

    def _exit_fullscreen(self) -> None:
        if not self._fullscreen:
            return
        self._fullscreen = False
        self._toolbar.setVisible(True)
        self.statusBar().setVisible(True)
        self.panel.setVisible(True)
        self.filmstrip.setVisible(True)
        self.tree.setVisible(self._folders_action.isChecked())
        if self._fs_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        if self._fs_prev_page == PAGE_GRID:
            self._close_viewer()
        else:
            self.viewer.setFocus()

    def _refresh_viewer_res(self) -> None:
        entry = self._current_entry()
        if (entry is not None and self.stacked.currentIndex() == PAGE_VIEWER
                and self.viewer.current_fid == entry.id):
            self.workers.request_viewer_image(entry.id, entry.path,
                                              self._fit_target(entry))

    def _fit_target(self, entry) -> int:
        dpr = self.viewer.devicePixelRatioF()
        vw, vh = self.viewer.width() * dpr, self.viewer.height() * dpr
        if not entry.width or not entry.height:
            return int(max(vw, vh)) or 2048
        fit = min(vw / entry.width, vh / entry.height)
        return min(int(math.ceil(fit * max(entry.width, entry.height))),
                   max(entry.width, entry.height))

    def _show_in_viewer(self, entry) -> None:
        self.viewer.show_image(entry.id, entry.width, entry.height,
                               _entry_stack(entry))
        cached = self._viewer_cache.get(entry.id)
        if cached is not None:
            self.viewer.set_texture_image(entry.id, cached[0], cached[1])
        else:
            self.workers.request_viewer_placeholder(entry.id)
            self.workers.request_viewer_image(entry.id, entry.path,
                                              self._fit_target(entry))
        # Prefetch neighbors in filmstrip order.
        idx = self.grid.selectionModel().currentIndex()
        for off in range(1, PREFETCH_NEIGHBORS + 1):
            for row in (idx.row() + off, idx.row() - off):
                nidx = self.proxy.index(row, 0)
                if not nidx.isValid():
                    continue
                nentry = self.model.entry_by_id(nidx.data(IdRole))
                if nentry and nentry.id not in self._viewer_cache:
                    self.workers.request_viewer_image(
                        nentry.id, nentry.path, self._fit_target(nentry))

    def _navigate(self, delta: int) -> None:
        idx = self.grid.selectionModel().currentIndex()
        row = (idx.row() if idx.isValid() else 0) + delta
        if 0 <= row < self.proxy.rowCount():
            self.grid.setCurrentIndex(self.proxy.index(row, 0))

    def _request_full_res(self, fid: int) -> None:
        entry = self.model.entry_by_id(fid)
        if entry:
            self.workers.request_viewer_image(fid, entry.path, None)

    # ------------------------------------------------------------------ culling

    def _delete_selected(self) -> None:
        if self.viewer.in_crop_mode or self.viewer.in_wb_pick_mode:
            return
        entries = self._selected_entries()
        if not entries:
            return
        if len(entries) > 1:
            resp = QMessageBox.question(
                self, "Move to trash",
                f"Move {len(entries)} images to the trash?")
            if resp != QMessageBox.StandardButton.Yes:
                return
        prev_row = self.grid.selectionModel().currentIndex().row()
        trashed, failed = [], []
        for e in entries:
            (trashed if QFile.moveToTrash(e.path) else failed).append(e)
        if trashed:
            ids = [e.id for e in trashed]
            self.catalog.remove_files(ids)
            self.model.remove_entries(ids)
            for fid in ids:
                self._viewer_cache.pop(fid, None)
        count = self.proxy.rowCount()
        if count == 0:
            if self._fullscreen:
                self._exit_fullscreen()
            if self.stacked.currentIndex() == PAGE_VIEWER:
                self._close_viewer()
            self.panel.load(None, None)
        else:
            row = min(max(prev_row, 0), count - 1)
            idx = self.proxy.index(row, 0)
            self.grid.setCurrentIndex(idx)
            # Removing the current row often leaves the current *row number*
            # unchanged, so currentChanged never fires — refresh the panel
            # (and the viewer, e.g. fullscreen Del-culling) explicitly.
            entry = self._current_entry()
            if entry is not None and self.panel.current_fid != entry.id:
                self._on_current_changed(idx, None)
        msg = f"Moved {len(trashed)} image(s) to trash"
        if failed:
            msg += f" — {len(failed)} failed (no trash on this filesystem?)"
        self.statusBar().showMessage(msg, 5000)
        self._update_count()

    def _rate(self, rating: int) -> None:
        for e in self._selected_entries():
            e_rating = rating if e.rating != rating else 0
            self.catalog.set_rating(e.id, e_rating)
            self.model.set_rating(e.id, e_rating)

    def _flag(self, flag: int) -> None:
        for e in self._selected_entries():
            self.catalog.set_flag(e.id, flag)
            self.model.set_flag(e.id, flag)

    # ------------------------------------------------------------- interactive crop

    def _start_crop(self) -> None:
        if self._current_entry() is None or self.viewer.in_crop_mode:
            return
        if self.stacked.currentIndex() != PAGE_VIEWER:
            self._open_viewer()
        self.viewer.begin_crop()
        self.statusBar().showMessage(
            "Crop: drag the handles or the box — Enter applies, Esc cancels")

    def _commit_crop(self, rect: list) -> None:
        self.statusBar().clearMessage()
        fid = self.viewer.current_fid
        if fid is not None and fid == self.panel.current_fid:
            self.panel.append_crop(rect)

    # ------------------------------------------------------------ WB eyedropper

    def _start_wb_pick(self) -> None:
        if (self._current_entry() is None or self.viewer.in_crop_mode
                or self.viewer.in_wb_pick_mode):
            return
        if self.stacked.currentIndex() != PAGE_VIEWER:
            self._open_viewer()
        self.viewer.begin_wb_pick()
        self.statusBar().showMessage(
            "White balance: click a spot that should be neutral gray — "
            "Esc cancels")

    def _commit_wb_pick(self, r: float, g: float, b: float) -> None:
        self.statusBar().clearMessage()
        fid = self.viewer.current_fid
        if fid is not None and fid == self.panel.current_fid:
            self.panel.apply_wb(*solve_white_balance((r, g, b)))

    # ------------------------------------------------------------------- editing

    def _on_stack_edited(self, stack: EditStack, final: bool) -> None:
        fid = self.panel.current_fid
        if fid is None:
            return
        if self.viewer.current_fid == fid:
            self.viewer.set_stack(stack)
        if final:
            self._persist_stack(fid, stack)

    def _persist_stack(self, fid: int, stack: EditStack,
                       bulk: bool = False) -> None:
        js = stack.to_json() if stack.ops else None
        self.model.set_stack(fid, js, stack.has_edits())
        self.catalog.set_stack(fid, js)
        self.history.record(fid, stack)
        entry = self.model.entry_by_id(fid)
        if entry:
            self.workers.rerender_thumb(entry, bulk=bulk)

    def _apply_external_stack(self, fid: int, stack: EditStack,
                              bulk: bool = False) -> None:
        """Paste/undo path: also refresh panel and viewer if fid is current."""
        self._persist_stack(fid, stack, bulk=bulk)
        if self.panel.current_fid == fid:
            self.panel.load(fid, stack)
        if self.viewer.current_fid == fid:
            self.viewer.set_stack(stack)

    def _undo(self) -> None:
        fid = self.panel.current_fid
        if fid is not None:
            stack = self.history.undo(fid)
            if stack is not None:
                self._restore_stack(fid, stack)

    def _redo(self) -> None:
        fid = self.panel.current_fid
        if fid is not None:
            stack = self.history.redo(fid)
            if stack is not None:
                self._restore_stack(fid, stack)

    def _restore_stack(self, fid: int, stack: EditStack) -> None:
        js = stack.to_json() if stack.ops else None
        self.model.set_stack(fid, js, stack.has_edits())
        self.catalog.set_stack(fid, js)
        entry = self.model.entry_by_id(fid)
        if entry:
            self.workers.rerender_thumb(entry)
        self.panel.load(fid, stack)
        if self.viewer.current_fid == fid:
            self.viewer.set_stack(stack)

    # ----------------------------------------------------------- edits clipboard

    def _copy_edits(self) -> None:
        if self.panel.current_fid is None:
            return
        n = self.clipboard.copy(self.panel.current_stack())
        self.statusBar().showMessage(f"Copied {n} edit op(s)", 3000)

    def _paste_edits(self, append: bool) -> None:
        if self.clipboard.is_empty():
            self.statusBar().showMessage("Edit clipboard is empty", 3000)
            return
        targets = self._selected_entries()
        bulk = len(targets) > 4
        for e in targets:
            base = _entry_stack(e)
            self.history.seed(e.id, base)
            new = (self.clipboard.paste_append(base) if append
                   else self.clipboard.paste_replace(base))
            self._apply_external_stack(e.id, new, bulk=bulk)
        mode = "appended to" if append else "replaced on"
        self.statusBar().showMessage(
            f"Edits {mode} {len(targets)} image(s)", 4000)

    def _apply_last_edit(self) -> None:
        source = self.panel.current_stack()
        if not source.ops:
            return
        last_op = source.ops[-1]
        src_fid = self.panel.current_fid
        targets = [e for e in self._selected_entries() if e.id != src_fid]
        bulk = len(targets) > 4
        for e in targets:
            base = _entry_stack(e)
            self.history.seed(e.id, base)
            base.ops.append(copy.deepcopy(last_op))
            self._apply_external_stack(e.id, base, bulk=bulk)
        if targets:
            self.statusBar().showMessage(
                f"Applied “{last_op.summary()}” to {len(targets)} image(s)", 4000)

    # -------------------------------------------------------------------- export

    def _export(self) -> None:
        entries = self._selected_entries()
        if len(entries) <= 1:
            entries = [self.model.entry_by_id(self.proxy.index(r, 0).data(IdRole))
                       for r in range(self.proxy.rowCount())]
            entries = [e for e in entries if e]
        if not entries:
            return
        items = [ExportItem(e.id, e.path, e.stack_json, e.capture_dt, e.mtime)
                 for e in entries]
        dlg = ExportDialog(len(items), self, sample=items[0])
        if dlg.exec() != ExportDialog.DialogCode.Accepted:
            return
        opts = dlg.options()
        if not opts.dest_dir:
            return
        self._progress = QProgressDialog(
            f"Exporting {len(items)} images…", "Cancel", 0, len(items), self)
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.canceled.connect(self.exporter.cancel)
        self.exporter.run(items, opts)

    def _on_export_progress(self, done: int, total: int, path: str,
                            error) -> None:
        if self._progress is not None:
            self._progress.setValue(done)
        if error:
            self.statusBar().showMessage(f"Export failed: {path}: {error}", 6000)

    def _on_export_finished(self, ok: int, failed: int) -> None:
        if self._progress is not None:
            self._progress.reset()
            self._progress = None
        msg = f"Exported {ok} image(s)"
        if failed:
            msg += f", {failed} failed"
        self.statusBar().showMessage(msg, 6000)

    # ---------------------------------------------------------------------------

    def closeEvent(self, ev) -> None:
        self.workers.shutdown()
        self.exporter.cancel()
        self.catalog.close()
        super().closeEvent(ev)


THEMES = ("system", "light", "dark")
_native_style: str | None = None
_native_palette: QPalette | None = None


def capture_native_theme(app: QApplication) -> None:
    """Remember the platform's style and palette before we touch anything,
    so 'System' can always be restored at runtime."""
    global _native_style, _native_palette
    _native_style = app.style().objectName()
    _native_palette = QPalette(app.palette())


def apply_theme(app: QApplication, mode: str) -> None:
    if mode in ("light", "dark"):
        tokens = styles.DARK if mode == "dark" else styles.LIGHT
        app.setStyle("Fusion")
        app.setPalette(styles.make_palette(tokens))
        app.setStyleSheet(styles.build_qss(tokens))
    else:  # system: whatever the platform theme (GTK/KDE/…) provides
        app.setStyleSheet("")
        app.setStyle(_native_style or "Fusion")
        app.setPalette(_native_palette or QPalette())


def _register_linux_desktop(data_home: str | None = None, *,
                            force: bool = False) -> None:
    """Install the launcher entry + theme icons for the packaged Linux binary.

    On Wayland the dock/taskbar icon comes from a .desktop file matched to
    the window's app id — a window icon alone shows as a generic gear. Runs
    on every frozen start so Exec= follows the binary if it moves. force=True
    (the default-viewer button) registers dev runs too; data_home overrides
    the destination (tests).
    """
    if sys.platform != "linux":
        return
    if data_home is None:
        if not (force or getattr(sys, "frozen", False)):
            return
        data_home = os.environ.get("XDG_DATA_HOME",
                                   os.path.expanduser("~/.local/share"))
    apps_dir = os.path.join(data_home, "applications")
    scalable = os.path.join(data_home, "icons", "hicolor", "scalable", "apps")
    sized = os.path.join(data_home, "icons", "hicolor", "256x256", "apps")
    for d in (apps_dir, scalable, sized):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(scalable, "photoflow.svg"), "w") as f:
        f.write(styles.app_icon_svg())
    png = os.path.join(sized, "photoflow.png")
    if not os.path.exists(png):
        styles.write_app_icon(png)
    if getattr(sys, "frozen", False):
        exec_cmd = f'"{os.path.realpath(sys.executable)}"'
    else:  # dev run registered via the default-viewer button
        exec_cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
    entry = "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        "Name=photoflow",
        "Comment=Fast JPEG/PNG browser, culling and non-destructive editor",
        f"Exec={exec_cmd} %F",
        "Icon=photoflow",
        "Terminal=false",
        "Categories=Graphics;Photography;Viewer;",
        "MimeType=image/jpeg;image/png;",
        "StartupWMClass=photoflow",
    )) + "\n"
    path = os.path.join(apps_dir, "photoflow.desktop")
    try:
        with open(path) as f:
            if f.read() == entry:
                return
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(entry)


def main() -> int:
    QSurfaceFormat.setDefaultFormat(default_gl_format())
    app = QApplication(sys.argv)
    app.setApplicationName("photoflow")
    app.setDesktopFileName("photoflow")  # Wayland app id ↔ photoflow.desktop
    app.setWindowIcon(styles.app_icon())
    try:
        _register_linux_desktop()
    except OSError:
        pass  # desktop integration must never block startup
    capture_native_theme(app)
    theme = QSettings("photoflow", "photoflow").value("theme", "system")
    apply_theme(app, theme if theme in THEMES else "system")
    # photoflow [folder|image] — also the Exec=%F double-click path
    target = next((a for a in app.arguments()[1:] if not a.startswith("-")),
                  None)
    win = MainWindow(initial_path=target)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
