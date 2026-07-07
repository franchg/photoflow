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

from verify_headless import (make_test_dng, make_test_jpeg,  # noqa: E402
                             make_test_png)

from PySide6.QtGui import QSurfaceFormat  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from views.viewer import default_gl_format  # noqa: E402

QSurfaceFormat.setDefaultFormat(default_gl_format())
app = QApplication(sys.argv)

import app as photoflow_app  # noqa: E402
from editstack import EditStack, Op  # noqa: E402
from PySide6.QtCore import QItemSelectionModel, QSettings  # noqa: E402
from PySide6.QtWidgets import QDialogButtonBox  # noqa: E402

# Fully isolated settings: redirect ALL user-scope QSettings of this process
# into a temp dir, so the tests can neither read nor pollute the real
# preferences (a leaked last_folder pointing at a deleted smoke dir once
# broke real startups with a popup).
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                  tempfile.mkdtemp(prefix="photoflow-smoke-settings-"))

# Belt over suspenders: also drop every persisted preference the tests
# assert on (last_folder's queued auto-scan would race the test's own scan).
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
make_test_dng(os.path.join(folder, "raw000.dng"),
              capture="2024:07:15 10:00:00")
N = 15  # 12 JPEGs + 2 PNGs + 1 RAW, all flowing through the same pipeline

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

# culling: rating/flagging a single photo auto-advances to the next one
win.grid.setCurrentIndex(win.proxy.index(0, 0))
app.processEvents()
e0 = win._current_entry()
win._rate(4)
app.processEvents()
assert win.model.entry_by_id(e0.id).rating == 4
e1 = win._current_entry()
assert e1.id != e0.id, "rating should advance to the next photo"
win._flag(1)
app.processEvents()
assert win.model.entry_by_id(e1.id).flag == 1
assert win.grid.selectionModel().currentIndex().row() == 2
ok("rate + pick with auto-advance")

# a multi-selection stays put (advancing would destroy it)
win.grid.setCurrentIndex(win.proxy.index(2, 0))
win.grid.selectionModel().select(
    win.proxy.index(3, 0), QItemSelectionModel.SelectionFlag.Select
    | QItemSelectionModel.SelectionFlag.Rows)
app.processEvents()
win._rate(2)
app.processEvents()
assert win.grid.selectionModel().currentIndex().row() == 2
assert all(win.model.entries()[r].rating == 2 for r in (2, 3))
win.grid.selectionModel().clear()
app.processEvents()
ok("multi-selection rating stays put")

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

# crop aspect presets: clicking a preset starts the crop locked to it;
# the box holds the ratio (in pixels) through a corner drag
win.panel._aspect_buttons["16:9"][0].click()
app.processEvents()
assert win.viewer.in_crop_mode


def _crop_px_ratio():
    dw, dh = win.viewer._display_size()
    _, _, cw, ch = win.viewer._crop_rect
    return (cw * dw) / (ch * dh)


dw0, dh0 = win.viewer._display_size()
target = 16 / 9 if dw0 >= dh0 else 9 / 16   # presets follow orientation
assert abs(_crop_px_ratio() - target) < 0.02, _crop_px_ratio()
r = win.viewer._crop_rect_logical()
start = QPointF(r.right(), r.bottom())
end = QPointF(r.right() - 70, r.bottom() - 20)
win.viewer.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, start,
                                Qt.MouseButton.LeftButton))
win.viewer.mouseMoveEvent(_mev(QEvent.Type.MouseMove, end,
                               Qt.MouseButton.LeftButton))
win.viewer.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease, end,
                                  Qt.MouseButton.NoButton))
assert abs(_crop_px_ratio() - target) < 0.02, _crop_px_ratio()
assert win.viewer._crop_rect[2] < 0.999  # the drag did shrink the box
win.panel._aspect_buttons["1:1"][0].click()  # re-snap while active
app.processEvents()
assert abs(_crop_px_ratio() - 1.0) < 0.02, _crop_px_ratio()
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
win.panel._aspect_buttons["Free"][0].click()  # restore default for later
app.processEvents()
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
assert not win.viewer.in_crop_mode
ok("crop aspect presets: lock on entry, survive drags, re-snap live")

# WB eyedropper: click → the stack's folded temperature/tint land exactly on
# the solve for the sampled pixel; Esc cancels the mode
from render import solve_white_balance  # noqa: E402

pump(lambda: win.viewer._sample_image is not None, what="viewer texture image")
win._start_wb_pick()
assert win.viewer.in_wb_pick_mode
wb_pos = win.viewer._frame_rect_logical().center()
expected = solve_white_balance(win.viewer._sample_source_rgb(wb_pos))
win.viewer.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, wb_pos,
                                Qt.MouseButton.LeftButton))
app.processEvents()
assert not win.viewer.in_wb_pick_mode
f_wb = win.panel.current_stack().folded_tune()
assert (abs(f_wb.temperature - expected[0]) < 0.01
        and abs(f_wb.tint - expected[1]) < 0.01), (f_wb, expected)
fid_wb = win.viewer.current_fid
if any(abs(v) >= 0.005 for v in expected):
    pump(lambda: (s := win.catalog.get_stack(fid_wb))
         and ('"temperature"' in s or '"tint"' in s), what="wb persisted")
win._start_wb_pick()
assert win.viewer.in_wb_pick_mode
win.viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
assert not win.viewer.in_wb_pick_mode
ok("WB eyedropper: click solves temperature/tint, persists, Esc cancels")

# vignette: V-style placement click sets the center and turns the effect
# on; sliders edit the op in place; zero strength removes it
win._start_vig_pick()
assert win.viewer.in_vig_pick_mode
vf = win.viewer._frame_rect_logical()
click = QPointF(vf.x() + 0.25 * vf.width(), vf.y() + 0.4 * vf.height())
win.viewer.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, click,
                                Qt.MouseButton.LeftButton))
app.processEvents()
assert not win.viewer.in_vig_pick_mode
vop = win.panel.current_stack().vignette()
assert vop is not None and abs(vop["cx"] - 0.25) < 0.02 \
    and abs(vop["cy"] - 0.4) < 0.02 and vop["strength"] == -0.5, vop
win.panel._vig_sliders["strength"].setValue(-80)
win.panel._vig_sliders["radius"].setValue(60)
app.processEvents()
vop = win.panel.current_stack().vignette()
assert vop["strength"] == -0.8 and vop["radius"] == 0.6, vop
fid_v = win.viewer.current_fid
pump(lambda: (s := win.catalog.get_stack(fid_v)) and '"vignette"' in s,
     what="vignette persisted")
win.panel._vig_sliders["strength"].setValue(0)
app.processEvents()
assert win.panel.current_stack().vignette() is None
win._apply_external_stack(fid_v, EditStack())
app.processEvents()
ok("vignette: click places center, sliders edit, zero removes")

# right-click hold: original (identity tune) while pressed, edits after
fid_cmp = win.viewer.current_fid
win._apply_external_stack(fid_cmp, EditStack([Op("tune", {"exposure": 0.5})]))
app.processEvents()
assert not win.viewer._effective_tune().identity
win.viewer.mousePressEvent(QMouseEvent(
    QEvent.Type.MouseButtonPress, wb_pos, QPointF(0, 0),
    Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
    Qt.KeyboardModifier.NoModifier))
assert win.viewer._show_original and win.viewer._effective_tune().identity
win.viewer.mouseReleaseEvent(QMouseEvent(
    QEvent.Type.MouseButtonRelease, wb_pos, QPointF(0, 0),
    Qt.MouseButton.RightButton, Qt.MouseButton.NoButton,
    Qt.KeyboardModifier.NoModifier))
assert (not win.viewer._show_original
        and not win.viewer._effective_tune().identity)
ok("right-click hold: original while pressed, edits restored on release")

# rotation slider: free angle decomposes into 90° + fine; 0 clears the op
win.panel._rot_slider.setValue(1033)          # +103.3°
app.processEvents()
geo_rot = win.panel.current_stack().geometry()
assert geo_rot.cw_degrees == 90 and abs(geo_rot.fine - 13.3) < 1e-6, geo_rot
assert abs(win.panel.current_stack().total_rotation() - 103.3) < 1e-6
assert win.viewer._display_size()[0] < win.viewer._full_h  # auto-crop shrinks
win.panel._rot_slider.setValue(900)           # exactly 90 → lossless-able
app.processEvents()
geo_rot = win.panel.current_stack().geometry()
assert geo_rot.cw_degrees == 90 and geo_rot.fine == 0.0
win.panel._rot_slider.setValue(0)
app.processEvents()
assert not any(op.op == "rotate" for op in win.panel.current_stack().ops)
win._apply_external_stack(fid_cmp, EditStack())  # clean for later scenarios
app.processEvents()
ok("rotation slider: fine decompose, lossless snap at 90, zero clears")

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

dlg = SettingsDialog(win, theme="dark", show_hidden=True, ui_scale=1.25,
                     pair_raw=False,
                     catalog_path="/tmp/x.db", on_empty_catalog=lambda: None)
assert dlg.values() == {"theme": "dark", "ui_scale": 1.25, "show_hidden": True,
                        "pair_raw": False, "catalog_path": "/tmp/x.db"}
dlg.deleteLater()
ok("settings dialog: fields round-trip (incl. UI scale)")

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

# help dialog: F1 opens the non-modal shortcuts reference, sections populated
from views.helpdialog import SECTIONS  # noqa: E402

QTest.keyClick(win.grid, Qt.Key.Key_F1)
app.processEvents()
assert win._help is not None and win._help.isVisible()
n_keys = sum(len(entries) for _, entries in SECTIONS)
assert n_keys >= 20, n_keys
all_keys = [k for _, entries in SECTIONS for k, _ in entries]
assert len(all_keys) == len(set(all_keys)), "duplicate key rows"
win._help.close()
app.processEvents()
ok("help dialog: F1 opens, sections populated, keys unique")

# narrow window: toolbar overflow is reachable via the » extension button
from PySide6.QtWidgets import QToolButton  # noqa: E402

old_size = win.size()
win.resize(620, 700)
app.processEvents()
_ext = win._toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
assert _ext is not None and _ext.isVisible()
_h_before = win._toolbar.height()
QTest.mouseClick(_ext, Qt.MouseButton.LeftButton)
for _ in range(25):
    app.processEvents()
    time.sleep(0.02)
assert win._toolbar.height() > _h_before + 20, (win._toolbar.height(), _h_before)
win.resize(old_size)
app.processEvents()
ok("toolbar overflow: » visible at narrow widths, expands the hidden rows")

# sort combo: switching to capture date actually reorders (regression:
# sort(0, Asc) was a no-op when already name-sorted, so nothing moved)
fid0 = win.proxy.index(0, 0).data(photoflow_app.IdRole)
e0 = win.model.entry_by_id(fid0)
win.model.update_meta(e0.id, e0.width, e0.height, e0.orientation,
                      "2030-12-31 00:00:00")  # name-first, date-last
win._sort_combo.setCurrentIndex(1)  # Capture date
app.processEvents()
assert win.proxy.index(0, 0).data(photoflow_app.IdRole) != fid0, \
    "date sort did not reorder"
win._sort_combo.setCurrentIndex(0)  # back to Name
app.processEvents()
assert win.proxy.index(0, 0).data(photoflow_app.IdRole) == fid0
ok("sort combo: name ↔ capture date reorders both ways")

# startup with a vanished last_folder: silent (no modal would-block popup),
# nothing scanned, and the stale setting self-heals
gone = tempfile.mkdtemp(prefix="photoflow-smoke-gone-")
os.rmdir(gone)
_settings.setValue("last_folder", gone)
_settings.setValue("catalog_path", os.path.join(folder, "startup-test.db"))
_settings.sync()
win2 = photoflow_app.MainWindow()
win2.show()
for _ in range(30):
    app.processEvents()
    time.sleep(0.02)
assert win2.model.rowCount() == 0
assert not _settings.value("last_folder"), _settings.value("last_folder")
win2.close()
app.processEvents()
ok("startup with vanished last_folder: silent, setting cleared")

# Linux desktop integration: registration writes launcher entry + theme icons
from desktopintegration import register_linux_desktop  # noqa: E402

reg_dir = tempfile.mkdtemp(prefix="photoflow-smoke-desktop-")
register_linux_desktop(reg_dir)
_desktop_file = os.path.join(reg_dir, "applications", "photoflow.desktop")
assert os.path.isfile(_desktop_file)
assert os.path.isfile(os.path.join(
    reg_dir, "icons", "hicolor", "scalable", "apps", "photoflow.svg"))
assert os.path.isfile(os.path.join(
    reg_dir, "icons", "hicolor", "256x256", "apps", "photoflow.png"))
with open(_desktop_file) as f:
    _entry = f.read()
assert "Icon=photoflow" in _entry and "Exec=" in _entry, _entry
register_linux_desktop(reg_dir)  # idempotent re-run
ok("desktop registration: launcher entry + theme icons written")

# CLI open: a folder path lands in the grid, an image path goes fullscreen
# on exactly that image (the Exec=%F double-click path)
win.open_path(folder)
pump(lambda: win.model.rowCount() > 1 and win._current_folder == folder,
     what="open_path folder scan")
assert not win._fullscreen and win.stacked.currentIndex() == 0
img_target = win.model.entries()[1].path
win.open_path(img_target)
pump(lambda: win._fullscreen and (e := win._current_entry()) is not None
     and e.path == img_target, what="open_path image fullscreen")
assert win.isFullScreen() and win.stacked.currentIndex() == 1
win._exit_fullscreen()
app.processEvents()
win.open_path(os.path.join(folder, "does-not-exist.jpg"))  # silently ignored
app.processEvents()
assert not win._fullscreen
ok("open_path: folder → grid, image → fullscreen, bogus path ignored")

# catalog maintenance: clearing the thumbnail cache keeps edits; the
# missing-file sweep drops moved/deleted entries (progress path, no dialogs)
fid_keep = win.model.entries()[0].id
win._apply_external_stack(fid_keep, EditStack([Op("tune", {"exposure": 0.25})]))
pump(lambda: (t := win.catalog.get_thumb_small(fid_keep)) is not None and t[1],
     what="edited thumb cached")
win._clear_thumb_cache(interactive=False)
assert win.catalog.get_thumb_small(fid_keep) is None
assert win.catalog.get_stack(fid_keep) is not None  # edits survive
ok("clear thumbnail cache: thumbs gone, edits kept")

victim2 = win.model.entries()[2]
os.remove(victim2.path)  # vanish behind the app's back
n_before2 = win.model.rowCount()
removed = win._remove_missing_entries(interactive=False)
assert removed == 1, removed
pump(lambda: win.model.rowCount() == n_before2 - 1,
     what="rescan after missing-file sweep")
pump(lambda: win.catalog._read_conn().execute(
    "SELECT COUNT(*) FROM files WHERE id=?", (victim2.id,)).fetchone()[0] == 0,
    what="missing row removed")
assert win.catalog.get_stack(fid_keep) is not None  # others untouched
assert win._remove_missing_entries(interactive=False) == 0  # now all exist
ok("remove missing files: sweep removed 1, edits of existing files kept")

# export scope: Ctrl+E must offer exactly the selection (a single selection
# used to silently fall back to the whole folder), and the dialog's confirm
# button carries the count. Stub the dialog so nothing modal opens.
export_counts = []
_real_dialog = photoflow_app.ExportDialog


class _DialogSpy:
    DialogCode = _real_dialog.DialogCode

    def __init__(self, count, parent=None, sample=None):
        export_counts.append(count)

    def exec(self):
        return self.DialogCode.Rejected


photoflow_app.ExportDialog = _DialogSpy
try:
    win.grid.selectionModel().clear()
    win.grid.setCurrentIndex(win.proxy.index(0, 0))
    app.processEvents()
    win._export()                      # no explicit selection → current photo
    for r in range(3):
        win.grid.selectionModel().select(
            win.proxy.index(r, 0), QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows)
    app.processEvents()
    win._export()                      # multi-selection → exactly those
    assert export_counts == [1, 3], export_counts
finally:
    photoflow_app.ExportDialog = _real_dialog
from views.exportdialog import ExportDialog  # noqa: E402
_dlg = ExportDialog(3)
_btn = _dlg.findChild(QDialogButtonBox).button(
    QDialogButtonBox.StandardButton.Ok)
assert _btn.text() == "Export 3 photos", _btn.text()
_dlg.deleteLater()
ok("export offers exactly the selection; dialog button shows the count")

# compare view: two synced panes, focused-pane culling, B toggles
from PySide6.QtCore import QPointF  # noqa: E402

win.grid.selectionModel().clear()
win.grid.setCurrentIndex(win.proxy.index(0, 0))
app.processEvents()
win._toggle_compare()  # one photo selected: refused with a hint
assert win.stacked.currentIndex() == photoflow_app.PAGE_GRID
win.grid.selectionModel().select(
    win.proxy.index(1, 0), QItemSelectionModel.SelectionFlag.Select
    | QItemSelectionModel.SelectionFlag.Rows)
app.processEvents()
win._toggle_compare()
assert win.stacked.currentIndex() == photoflow_app.PAGE_COMPARE
assert len(win._compare.entries) == 2
ce0, ce1 = win._compare.entries
win._compare.set_focus(1)
win._rate(5)
app.processEvents()
assert win.model.entry_by_id(ce1.id).rating == 5
assert win.model.entry_by_id(ce0.id).rating != 5
assert win.stacked.currentIndex() == photoflow_app.PAGE_COMPARE  # no advance
win._compare.panes[0]._set_scale(2.0, QPointF(0, 0))
fit1, scale1, _pan1 = win._compare.panes[1].view_state()
assert not fit1 and abs(scale1 - 2.0) < 1e-9, (fit1, scale1)
win._exit_compare()
assert win.stacked.currentIndex() == photoflow_app.PAGE_GRID
ok("compare view: focus culling + synced zoom, B/Esc in and out")

# RAW+JPEG pairing: one photo in the grid, mirrored culling, paired trash
pair_dir = tempfile.mkdtemp(prefix="photoflow-smoke-pair-")
make_test_jpeg(os.path.join(pair_dir, "shot001.jpg"))
make_test_dng(os.path.join(pair_dir, "shot001.dng"))
make_test_jpeg(os.path.join(pair_dir, "loner.jpg"))

win.settings.setValue("pair_raw", False)
win._scan(pair_dir)
pump(lambda: win.model.rowCount() == 3, what="unpaired scan")
win.settings.setValue("pair_raw", True)
win._scan(pair_dir)
pump(lambda: win.model.rowCount() == 2, what="paired scan")
paired = next(e for e in win.model.entries() if e.name == "shot001.jpg")
dng_path = os.path.join(pair_dir, "shot001.dng")
assert paired.raw_twin_id is not None and paired.raw_twin_path == dng_path
from models import RawRole  # noqa: E402
row = next(r for r in range(win.proxy.rowCount())
           if win.proxy.index(r, 0).data() == "shot001.jpg")
assert win.proxy.index(row, 0).data(RawRole) is True
win.grid.setCurrentIndex(win.proxy.index(row, 0))
app.processEvents()
win._rate(3)
pump(lambda: win.catalog._read_conn().execute(
    "SELECT rating FROM files WHERE id=?",
    (paired.raw_twin_id,)).fetchone()[0] == 3, what="mirrored rating")
win.grid.setCurrentIndex(win.proxy.index(row, 0))
app.processEvents()
win._delete_selected()
pump(lambda: not os.path.exists(paired.path)
     and not os.path.exists(dng_path), what="paired trash")
pump(lambda: win.catalog._read_conn().execute(
    "SELECT COUNT(*) FROM files WHERE id IN (?, ?)",
    (paired.id, paired.raw_twin_id)).fetchone()[0] == 0,
    what="paired catalog rows removed")
assert win.model.rowCount() == 1
ok("raw+jpeg pairing: one photo, RAW chip, mirrored rating, paired trash")

# single instance: a second launch forwards its path to the running window
sock_name = "photoflow-smoke-instance"
assert not photoflow_app.forward_to_running(folder, sock_name)
server = photoflow_app.serve_single_instance(win, sock_name)
assert server is not None
assert photoflow_app.forward_to_running(folder, sock_name)
pump(lambda: win._current_folder == folder, what="forwarded open")
server.close()
ok("single instance: second launch forwarded to the running window")

win.close()
app.processEvents()
print("\nSMOKE PASS")
