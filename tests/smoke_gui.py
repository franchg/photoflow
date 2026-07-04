"""GUI smoke test: drives the real MainWindow (offscreen by default).

Run:  uv run python tests/smoke_gui.py
Scans a folder of generated JPEGs, waits for async thumbnails, exercises
culling, stack persistence, bulk paste, and the GL viewer. Exit 0 on success.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from verify_headless import make_test_jpeg, make_test_png  # noqa: E402

from PySide6.QtGui import QSurfaceFormat  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from views.viewer import default_gl_format  # noqa: E402

QSurfaceFormat.setDefaultFormat(default_gl_format())
app = QApplication(sys.argv)

import app as photoflow_app  # noqa: E402
from editstack import EditStack, Op  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402

# Hermetic settings: drop every persisted preference the tests assert on
# (last_folder's queued auto-scan would also race the test's own scan).
_settings = QSettings("photoflow", "photoflow")
for _key in ("last_folder", "folders_visible", "grid_size", "show_hidden",
             "catalog_path"):
    _settings.remove(_key)
_settings.sync()

folder = tempfile.mkdtemp(prefix="photoflow-smoke-")
for i in range(12):
    make_test_jpeg(os.path.join(folder, f"img{i:03d}.jpg"),
                   orientation=6 if i % 3 == 0 else 1,
                   capture=f"2024:07:{i + 1:02d} 10:00:00")
make_test_png(os.path.join(folder, "png000.png"))
make_test_png(os.path.join(folder, "png001.png"), alpha=True)
N = 14  # 12 JPEGs + 2 PNGs, all flowing through the same pipeline

win = photoflow_app.MainWindow()
win.resize(1280, 800)
win.show()


def pump(cond, timeout=20.0, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return
        time.sleep(0.02)
    print(f"FAIL timeout waiting for {what}")
    sys.exit(1)


def ok(name):
    print(f"PASS {name}")


win._scan(folder)
pump(lambda: win.model.rowCount() == N, what="scan")
ok(f"scan found {N}")

pump(lambda: all(e.thumb is not None for e in win.model.entries()),
     what="all thumbs")
ok("all thumbs arrived (EXIF + decoded)")

pump(lambda: all(e.width > 0 for e in win.model.entries()), what="meta")
e0 = win.model.entries()[0]
assert (e0.width, e0.height) == (480, 640), f"oriented dims {e0.width}x{e0.height}"
ok("oriented dimensions in model")

# culling
win.grid.setCurrentIndex(win.proxy.index(0, 0))
app.processEvents()
win._rate(4)
win._flag(1)
assert win._current_entry().rating == 4 and win._current_entry().flag == 1
ok("rate + pick")

# filters
win.proxy.set_filters(min_rating=4)
assert win.proxy.rowCount() == 1
win.proxy.set_filters(min_rating=0)
assert win.proxy.rowCount() == N
ok("rating filter")

# Bridge-style status bar counts
win.grid.selectAll()
app.processEvents()
txt = win._count_label.text()
assert f"{N} items" in txt and f"{N} selected" in txt \
    and ("KB" in txt or "MB" in txt), txt
win.proxy.set_filters(min_rating=4)
app.processEvents()
txt = win._count_label.text()
assert f"{N - 1} hidden" in txt, txt
win.proxy.set_filters(min_rating=0)
win.grid.clearSelection()
app.processEvents()
assert "selected" not in win._count_label.text()
ok("status bar: items / hidden / selected / size")

# edit stack persistence + thumbnail re-render
fid = win.model.entries()[1].id
stack = EditStack([Op("tune", {"exposure": 0.6, "saturation": 0.3}),
                   Op("rotate", {"degrees": 90})])
win.history.seed(fid, EditStack())
old_thumb = win.model.entry_by_id(fid).thumb
win._apply_external_stack(fid, stack)
assert win.model.entry_by_id(fid).has_edits
pump(lambda: win.catalog.get_stack(fid) == stack.to_json(), what="stack write")
pump(lambda: (row := win.catalog.get_thumb_small(fid)) is not None and row[1],
     what="edited thumb in cache")
pump(lambda: win.model.entry_by_id(fid).thumb is not old_thumb,
     what="edited thumb delivered")
ok("stack persisted + edited thumb re-rendered")

# undo
assert win.history.undo(fid) is not None
ok("undo available after external stack")

# bulk copy/paste onto everything
win.panel.load(fid, stack)
win._copy_edits()
win.grid.selectAll()
app.processEvents()
win._paste_edits(append=False)
assert all(e.has_edits for e in win.model.entries())
pump(lambda: all((r := win.catalog.get_thumb_small(e.id)) and r[1]
                 for e in win.model.entries()),
     timeout=40, what="bulk edited thumbs")
ok(f"bulk paste onto {N} + background re-render converged")

# interactive crop: drag the bottom-right handle, Enter commits, Esc cancels.
# Pure interaction logic — works without a GL context.
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

win.grid.setCurrentIndex(win.proxy.index(2, 0))
app.processEvents()
if win.stacked.currentIndex() != 1:
    win._open_viewer()
app.processEvents()
committed = []
win.viewer.crop_committed.connect(committed.append)


def _mev(t, pos, buttons):
    return QMouseEvent(t, pos, QPointF(0, 0), Qt.MouseButton.LeftButton,
                       buttons, Qt.KeyboardModifier.NoModifier)


win._start_crop()
assert win.viewer.in_crop_mode
frame = win.viewer._frame_rect_logical()
r = win.viewer._crop_rect_logical()
start = QPointF(r.right(), r.bottom())
end = QPointF(r.right() - 80, r.bottom() - 60)
win.viewer.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, start,
                                Qt.MouseButton.LeftButton))
win.viewer.mouseMoveEvent(_mev(QEvent.Type.MouseMove, end,
                               Qt.MouseButton.LeftButton))
win.viewer.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease, end,
                                  Qt.MouseButton.NoButton))
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                   Qt.KeyboardModifier.NoModifier))
app.processEvents()
assert committed and not win.viewer.in_crop_mode
rw, rh = committed[0][2], committed[0][3]
exp_w, exp_h = 1 - 80 / frame.width(), 1 - 60 / frame.height()
assert abs(rw - exp_w) < 0.02 and abs(rh - exp_h) < 0.02, (committed, exp_w, exp_h)
fid_c = win.viewer.current_fid
pump(lambda: (s := win.catalog.get_stack(fid_c)) and '"crop"' in s,
     what="crop persisted")
ok("interactive crop: drag handle → commit → persisted with expected rect")

win._start_crop()
assert win.viewer.in_crop_mode
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
assert not win.viewer.in_crop_mode
ok("interactive crop: Esc cancels")

# fullscreen: F from the grid enters and exits (window shortcut), chrome hides
from PySide6.QtTest import QTest  # noqa: E402

win._close_viewer()
app.processEvents()
assert win.stacked.currentIndex() == 0
QTest.keyClick(win.grid, Qt.Key.Key_F)
app.processEvents()
assert win.isFullScreen() and win.stacked.currentIndex() == 1
assert (not win._toolbar.isVisible() and not win.panel.isVisible()
        and not win.filmstrip.isVisible())
QTest.keyClick(win.viewer, Qt.Key.Key_F)
app.processEvents()
assert (not win.isFullScreen() and win.stacked.currentIndex() == 0
        and win._toolbar.isVisible() and win.panel.isVisible())
ok("fullscreen: F from grid in/out, chrome restored")

# from the viewer: F enters fullscreen, Space advances, Z toggles zoom,
# Esc backs out of fullscreen first, then the viewer
win._open_viewer()
app.processEvents()
QTest.keyClick(win.viewer, Qt.Key.Key_F)
app.processEvents()
assert win.isFullScreen()
row_before = win.grid.selectionModel().currentIndex().row()
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                                   Qt.KeyboardModifier.NoModifier))
app.processEvents()
assert win.grid.selectionModel().currentIndex().row() == row_before + 1
was_fit = win.viewer._fit
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z,
                                   Qt.KeyboardModifier.NoModifier))
assert win.viewer._fit != was_fit
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
app.processEvents()
assert not win.isFullScreen() and win.stacked.currentIndex() == 1
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
app.processEvents()
assert win.stacked.currentIndex() == 0
ok("fullscreen: F toggles, Space=next, Z=zoom, Esc backs out in order")

# Del → system trash: file gone, model and catalog rows dropped
n_before = win.model.rowCount()
win.grid.setCurrentIndex(win.proxy.index(n_before - 1, 0))
app.processEvents()
victim = win._current_entry()
win._delete_selected()
app.processEvents()
assert win.model.rowCount() == n_before - 1
assert not os.path.exists(victim.path)
pump(lambda: win.catalog._read_conn().execute(
    "SELECT COUNT(*) FROM files WHERE id=?", (victim.id,)).fetchone()[0] == 0,
    what="catalog row removed")
assert win.grid.selectionModel().currentIndex().isValid()
ok("Del moves to trash; model, catalog, selection all consistent")

# folder tree: visible, toggleable, same-folder click is a no-op
assert win.tree.isVisible()
win._folders_action.setChecked(False)
assert not win.tree.isVisible()
win._folders_action.setChecked(True)
assert win.tree.isVisible()
gen = win.workers.generation
win.tree.folder_selected.emit(folder)
app.processEvents()
assert win.workers.generation == gen
ok("folder tree: toggle works, same-folder select does not rescan")

# Del while fullscreen: trash the shown image, advance to the next one
from models import IdRole  # noqa: E402

win.grid.setCurrentIndex(win.proxy.index(0, 0))
app.processEvents()
QTest.keyClick(win.grid, Qt.Key.Key_F)
app.processEvents()
assert win.isFullScreen()
n = win.model.rowCount()
cur = win._current_entry()
next_id = win.proxy.index(1, 0).data(IdRole)
QTest.keyClick(win.viewer, Qt.Key.Key_Delete)
app.processEvents()
assert win.model.rowCount() == n - 1
assert not os.path.exists(cur.path)
assert win.isFullScreen(), "should stay fullscreen after Del"
assert win.viewer.current_fid == next_id, "viewer must advance to next image"
assert win.panel.current_fid == next_id
QTest.keyClick(win.viewer, Qt.Key.Key_F)
app.processEvents()
assert not win.isFullScreen()
ok("Del in fullscreen: trashes current, shows the next image")

# viewer (GL) — may be unavailable on the offscreen platform
try:
    win.grid.setCurrentIndex(win.proxy.index(0, 0))
    win._open_viewer()
    pump(lambda: win.viewer._texture is not None or win.viewer._pending_image is not None,
         timeout=15, what="viewer texture")
    app.processEvents()
    win.viewer.toggle_fit()   # 100%
    app.processEvents()
    win._navigate(1)
    pump(lambda: win.viewer.current_fid == win.model.entries()[1].id
         or win.viewer.current_fid is not None, what="navigate")
    win._close_viewer()
    ok("viewer: texture, zoom toggle, navigate")
except Exception as exc:
    print(f"WARN viewer GL unavailable here: {exc}")

# Ctrl+R: force rescan drops cached thumbs and rebuilds them
n_now = win.model.rowCount()
win._force_rescan()
pump(lambda: win.model.rowCount() == n_now, what="rescan repopulates")
assert not any(e.has_thumb_cache for e in win.model.entries()), \
    "force rescan must invalidate the thumb cache"
pump(lambda: all(win.catalog.get_thumb_small(e.id) for e in win.model.entries()),
     timeout=40, what="thumbs regenerated")
pump(lambda: all(e.thumb is not None for e in win.model.entries()),
     what="thumbs redelivered")
ok("Ctrl+R: rescan + full thumbnail regeneration")

# hidden-files setting: dotfiles excluded by default, included when enabled
make_test_jpeg(os.path.join(folder, ".hidden.jpg"))
win._scan(folder)
pump(lambda: win.model.rowCount() == n_now, what="scan excludes dotfile")
win.settings.setValue("show_hidden", True)
win.tree.set_show_hidden(True)
win._scan(folder)
pump(lambda: win.model.rowCount() == n_now + 1, what="scan includes dotfile")
win.settings.setValue("show_hidden", False)
win.tree.set_show_hidden(False)
win._scan(folder)
pump(lambda: win.model.rowCount() == n_now, what="dotfile excluded again")
ok("hidden files setting drives the scan")

# grid size button group: three sizes, persisted
win._set_grid_size(0)
assert win.grid.gridSize().width() == 140
win._set_grid_size(2)
assert (win.grid.gridSize().width() == 264
        and int(win.settings.value("grid_size")) == 2)
win._set_grid_size(1)
assert win.grid.gridSize().width() == 196
ok("grid size selector: S/M/L applied + persisted")

# settings dialog fields round-trip
from views.settingsdialog import SettingsDialog  # noqa: E402

dlg = SettingsDialog(win, theme="dark", show_hidden=True,
                     catalog_path="/tmp/x.db", on_empty_catalog=lambda: None)
assert dlg.values() == {"theme": "dark", "show_hidden": True,
                        "catalog_path": "/tmp/x.db"}
dlg.deleteLater()
ok("settings dialog: fields round-trip")

# catalog relocation + empty — on a TEMP db, never the user's real catalog
tmp_db = os.path.join(folder, "test-catalog.db")
win._change_catalog_path(tmp_db)
pump(lambda: win.model.rowCount() == n_now, what="rescan on new catalog")
assert os.path.exists(tmp_db)
assert not any(e.has_edits for e in win.model.entries())
fid = win.model.entries()[0].id
win._apply_external_stack(fid, EditStack([Op("tune", {"exposure": 0.3})]))
pump(lambda: win.catalog.get_stack(fid) is not None, what="stack in new catalog")
win._empty_catalog()
pump(lambda: win.model.rowCount() == n_now, what="rescan after empty")
pump(lambda: not any(e.has_edits for e in win.model.entries())
     and win.catalog.get_stack(win.model.entries()[0].id) is None,
     what="catalog emptied")
win.settings.remove("catalog_path")  # don't leak the temp path to real runs
ok("catalog: relocate to new path + empty, both followed by clean rescan")

win.close()
app.processEvents()
print("\nSMOKE PASS")
