"""Single-image viewer: QOpenGLWidget + one textured quad.

The texture holds the *unedited*, orientation-normalized decode at (at least)
screen resolution. The edit stack is applied per-frame in the fragment shader
(tune) and in the UV/vertex transforms (rotate/crop), so slider drags re-render
at 60 fps without touching pixels. Geometry math matches render.apply_geometry.

Coordinates: y-down ortho, quad positions in [0,1]²; uv==a_pos maps texel row 0
(QImage top row) to screen top — no image mirroring needed anywhere.
"""
from __future__ import annotations

import math
import os

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QMatrix3x3, QMatrix4x4, QPainter,
                           QPainterPath, QPalette, QPen, QSurfaceFormat,
                           QVector2D)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
                              QOpenGLTexture, QOpenGLVertexArrayObject)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import decode
from editstack import EditStack, Geometry
from render import TuneUniforms, inscribed_scale, local_mean_luma

_GL_FLOAT = 0x1406
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_TRIANGLE_STRIP = 0x0005

_VERT_SRC = """
#version 330 core
layout(location = 0) in vec2 a_pos;
uniform mat4 u_mvp;
uniform mat3 u_uv;
out vec2 v_uv;
out vec2 v_frame;
void main() {
    v_uv = (u_uv * vec3(a_pos, 1.0)).xy;
    v_frame = a_pos;
    gl_Position = u_mvp * vec4(a_pos, 0.0, 1.0);
}
"""

_FRAG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "shaders", "adjust.frag")

# Inverse rotation matrices: display-frame (u', v') -> source uv, y-down, CW.
_ROT_INV = {
    0: np.eye(3, dtype=np.float64),
    90: np.array([[0, 1, 0], [-1, 0, 1], [0, 0, 1]], dtype=np.float64),
    180: np.array([[-1, 0, 1], [0, -1, 1], [0, 0, 1]], dtype=np.float64),
    270: np.array([[0, -1, 1], [1, 0, 0], [0, 0, 1]], dtype=np.float64),
}

MIN_SCALE, MAX_SCALE = 0.02, 8.0
MIN_CROP = 0.02          # smallest crop edge, normalized
_HANDLE_TOL = 9.0        # hit radius around handles, logical px

# handle name -> anchor position on the crop rect (x, y in 0..1)
_HANDLES = {"tl": (0, 0), "tr": (1, 0), "bl": (0, 1), "br": (1, 1),
            "t": (0.5, 0), "b": (0.5, 1), "l": (0, 0.5), "r": (1, 0.5)}
_LEFTISH, _RIGHTISH = {"tl", "bl", "l"}, {"tr", "br", "r"}
_TOPPISH, _BOTTOMISH = {"tl", "tr", "t"}, {"bl", "br", "b"}
_CROP_CURSORS = {
    "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
    "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "move": Qt.CursorShape.SizeAllCursor,
}


def mat3_uniform(m: np.ndarray) -> QMatrix3x3:
    """Row-major numpy 3×3 → QMatrix3x3 (Qt handles the GL layout)."""
    return QMatrix3x3([float(v) for v in np.asarray(m).flatten()])


def uv_matrix_for(geo: Geometry, full_w: int, full_h: int) -> np.ndarray:
    """Visible-frame [0,1]² → source-texture UV: user crop, then the fine
    rotation's inscribed-rect frame, then the 90° rotation. Mirrors
    render.apply_geometry (which resamples the same mapping)."""
    cx, cy, cw, ch = geo.rect
    crop = np.array([[cw, 0, cx], [0, ch, cy], [0, 0, 1]], dtype=np.float64)
    m = crop
    if geo.fine != 0.0:
        w2, h2 = (full_w, full_h)
        if geo.cw_degrees % 180:
            w2, h2 = h2, w2
        k = inscribed_scale(w2, h2, geo.fine)
        phi = math.radians(geo.fine)
        c, s = math.cos(phi), math.sin(phi)
        pre = np.array([[k * w2, 0, -0.5 * k * w2],
                        [0, k * h2, -0.5 * k * h2],
                        [0, 0, 1]])
        rot = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])  # rotate by -fine
        post = np.array([[1.0 / w2, 0, 0.5],
                         [0, 1.0 / h2, 0.5],
                         [0, 0, 1]])
        m = post @ rot @ pre @ m
    return _ROT_INV[geo.cw_degrees % 360] @ m


class CurveTexture:
    """GL-side cache of TuneUniforms.curve: a CURVE_N×1 RGB32F texture.
    Re-uploads only when a different curve array is bound (the identity
    curve is a module-wide singleton, so browsing without edits never
    re-uploads). Must be created/destroyed with a current GL context."""

    def __init__(self):
        self._tex: QOpenGLTexture | None = None
        # the uploaded array itself, not its id(): holding the reference
        # keeps it alive, so identity can't be recycled onto a new array
        self._curve: np.ndarray | None = None

    def bind(self, curve: np.ndarray, unit: int) -> None:
        if self._tex is None:
            tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            tex.setFormat(QOpenGLTexture.TextureFormat.RGB32F)
            tex.setSize(curve.shape[0], 1)
            tex.setMipLevels(1)
            tex.allocateStorage(QOpenGLTexture.PixelFormat.RGB,
                                QOpenGLTexture.PixelType.Float32)
            tex.setMinMagFilters(QOpenGLTexture.Filter.Nearest,
                                 QOpenGLTexture.Filter.Nearest)
            tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._tex = tex
            self._curve = None
        if self._curve is not curve:
            self._tex.setData(QOpenGLTexture.PixelFormat.RGB,
                              QOpenGLTexture.PixelType.Float32,
                              np.ascontiguousarray(curve).tobytes())
            self._curve = curve
        self._tex.bind(unit)

    def release(self, unit: int) -> None:
        if self._tex is not None:
            self._tex.release(unit)

    def destroy(self) -> None:
        if self._tex is not None:
            self._tex.destroy()
            self._tex = None
            self._curve = None


class LmapTexture:
    """GL-side cache of the ambiance local-mean map (render.local_mean_luma):
    a small R32F texture, re-uploaded when a different array is bound and
    re-created when the map size changes (it tracks the photo's aspect)."""

    def __init__(self):
        self._tex: QOpenGLTexture | None = None
        self._map: np.ndarray | None = None  # by reference, like CurveTexture

    def bind(self, lmap: np.ndarray, unit: int) -> None:
        h, w = lmap.shape
        if self._tex is not None and (self._tex.width() != w
                                      or self._tex.height() != h):
            self._tex.destroy()
            self._tex = None
        if self._tex is None:
            # RGB32F with the channel replicated: PySide6's setData silently
            # drops single-channel Red/Float32 uploads (the RGB path is the
            # one CurveTexture already proves out)
            tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            tex.setFormat(QOpenGLTexture.TextureFormat.RGB32F)
            tex.setSize(w, h)
            tex.setMipLevels(1)
            tex.allocateStorage(QOpenGLTexture.PixelFormat.RGB,
                                QOpenGLTexture.PixelType.Float32)
            tex.setMinMagFilters(QOpenGLTexture.Filter.Nearest,
                                 QOpenGLTexture.Filter.Nearest)
            tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._tex = tex
            self._map = None
        if self._map is not lmap:
            rgb = np.repeat(np.ascontiguousarray(lmap)[..., None], 3, axis=2)
            self._tex.setData(QOpenGLTexture.PixelFormat.RGB,
                              QOpenGLTexture.PixelType.Float32,
                              rgb.tobytes())
            self._map = lmap
        self._tex.bind(unit)

    def release(self, unit: int) -> None:
        if self._tex is not None:
            self._tex.release(unit)

    def destroy(self) -> None:
        if self._tex is not None:
            self._tex.destroy()
            self._tex = None
            self._map = None


_NO_LMAP = np.zeros((1, 1), dtype=np.float32)  # placeholder while no image


def _display_size_for(geo, full_w: float, full_h: float) -> tuple[float, float]:
    """Displayed region size in full-res source pixels (rotate,
    fine-rotation auto-crop, then crop)."""
    w, h = full_w, full_h
    if geo.cw_degrees % 180:
        w, h = h, w
    if geo.fine != 0.0:
        k = inscribed_scale(w, h, geo.fine)
        w, h = w * k, h * k
    _, _, cw, ch = geo.rect
    return max(w * cw, 1.0), max(h * ch, 1.0)


def set_tune_uniforms(prog: QOpenGLShaderProgram, tune,
                      curve_tex: CurveTexture, lmap_tex: LmapTexture,
                      lmap: np.ndarray, vig_tex: CurveTexture,
                      frame_size: tuple[float, float]) -> None:
    """Shared by the viewer and the shader parity test. Scalars go through
    setUniformValue1f/1i — PySide6's (str, float) overload is broken."""
    prog.setUniformValue1i("u_tex", 0)
    prog.setUniformValue1i("u_curve", 1)
    curve_tex.bind(tune.curve, 1)
    prog.setUniformValue1i("u_lmap", 2)
    lmap_tex.bind(lmap, 2)
    prog.setUniformValue("u_lmap_size",
                         QVector2D(lmap.shape[1], lmap.shape[0]))
    prog.setUniformValue1f("u_amb", float(tune.ambiance))
    prog.setUniformValue1f("u_hl", float(tune.highlights))
    prog.setUniformValue1f("u_sh", float(tune.shadows))
    prog.setUniformValue1f("u_sat", float(tune.saturation))
    prog.setUniformValue1i("u_vig_curve", 3)
    vig_tex.bind(tune.vig_curve, 3)
    prog.setUniformValue1f("u_vig", float(tune.vig_strength))
    prog.setUniformValue("u_vig_center", QVector2D(*tune.vig_center))
    prog.setUniformValue1f("u_vig_radius", float(tune.vig_radius))
    prog.setUniformValue("u_vig_frame", QVector2D(*frame_size))


def default_gl_format() -> QSurfaceFormat:
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapInterval(1)
    return fmt


class ViewerWidget(QOpenGLWidget):
    needs_full_res = Signal(int)   # user zoomed past the current texture's res
    nav_requested = Signal(int)    # ±1 from arrow keys / space
    close_requested = Signal()     # Esc
    crop_committed = Signal(object)  # [x, y, w, h] in the current visible frame
    crop_canceled = Signal()
    wb_picked = Signal(float, float, float)  # source-space sRGB under the click
    wb_pick_canceled = Signal()
    vig_center_picked = Signal(float, float)  # visible-frame coords of click
    vig_pick_canceled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)  # hover cursors over crop handles

        self._program: QOpenGLShaderProgram | None = None
        self._vao = QOpenGLVertexArrayObject()
        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._texture: QOpenGLTexture | None = None
        self._curve_tex = CurveTexture()
        self._vig_tex = CurveTexture()
        self._lmap_tex = LmapTexture()
        self._lmap = _NO_LMAP            # ambiance local-mean map (per photo)
        self._lmap_fid: int | None = None
        self._pending_image = None       # QImage waiting for upload in paintGL
        self._tex_level = -1             # workers.VIEWER_* level of the texture
        # Outgoing frame kept on screen while the next photo decodes:
        # (texture, tune, geo, full_w, full_h, lmap). Without it every
        # switch flashes the bare background until the decode lands.
        self._hold = None

        self._fid: int | None = None
        self._full_w = 0                 # oriented full-res dims of the source
        self._full_h = 0
        self._tune = TuneUniforms(EditStack().folded_tune())
        self._geo = Geometry()

        self._fit = True
        self._scale = 1.0                # device px per full-res source px
        self._pan = QPointF(0, 0)        # top-left of image rect, device px
        self._drag_start: QPointF | None = None
        self._pan_start = QPointF(0, 0)
        self._full_requested = False

        # interactive crop mode
        self._crop_mode = False
        self._crop_rect = [0.0, 0.0, 1.0, 1.0]  # normalized in visible frame
        self._crop_drag = None           # (handle, start QPointF, start rect)
        self._crop_aspect = None         # None=free, "original", or w/h float

        # white-balance eyedropper mode
        self._wb_pick = False
        # vignette center placement mode
        self._vig_pick = False
        self._sample_image = None        # CPU copy of the texture, for picking

        # right-click hold: compare with the original (tune bypassed;
        # crop/rotation stay so the framing doesn't jump mid-hold)
        self._show_original = False
        self._identity_tune = TuneUniforms(EditStack().folded_tune())

    # ------------------------------------------------------------------ API

    def show_image(self, fid: int, full_w: int, full_h: int,
                   stack: EditStack | None) -> None:
        """Switch to a new photo. Texture arrives later via set_texture_image."""
        if self._texture is not None:
            self._drop_hold()
            self._hold = (self._texture, self._tune, self._geo,
                          self._full_w, self._full_h, self._lmap)
            self._texture = None
        self._fid = fid
        self._full_w, self._full_h = full_w, full_h
        self._pending_image = None
        self._full_requested = False
        self._tex_level = -1
        if self._crop_mode:              # navigating away drops the crop
            self._crop_mode = False
            self._crop_drag = None
            self.unsetCursor()
        if self._wb_pick:                # ... and the pending WB pick
            self._wb_pick = False
            self.unsetCursor()
        if self._vig_pick:               # ... and the vignette placement
            self._vig_pick = False
            self.unsetCursor()
        self._show_original = False
        self._sample_image = None
        self._lmap = _NO_LMAP
        self._lmap_fid = None
        self.set_stack(stack, _repaint=False)
        self._fit = True
        self.update()

    def set_texture_image(self, fid: int, image, level: int) -> None:
        if fid != self._fid:
            return
        if level < self._tex_level:
            return  # never replace a texture with a lesser one
        self._pending_image = image
        self._sample_image = image       # QImage is shared, this is a cheap ref
        self._tex_level = level
        if fid != self._lmap_fid:
            # ambiance local-mean map: content-dependent only, so the first
            # (screen-res) arrival is enough — skip the full-res re-upload
            self._lmap = local_mean_luma(decode.qimage_to_rgb(image))
            self._lmap_fid = fid
        if not self._full_w:
            self._full_w, self._full_h = image.width(), image.height()
        self.update()

    def set_stack(self, stack: EditStack | None, _repaint: bool = True) -> None:
        stack = stack or EditStack()
        self._tune = TuneUniforms(stack.folded_tune(), stack.vignette())
        old_geo, self._geo = self._geo, stack.geometry()
        if _repaint:
            if old_geo != self._geo and self._fit:
                pass  # fit recomputed every frame anyway
            self.update()

    @property
    def current_fid(self) -> int | None:
        return self._fid

    def toggle_fit(self) -> None:
        if self._fit:
            self._set_scale(1.0, self._widget_center())
        else:
            self._fit = True
            self.update()

    # -------------------------------------------------------- interactive crop

    @property
    def in_crop_mode(self) -> bool:
        return self._crop_mode

    def begin_crop(self) -> None:
        """Enter crop mode: fit the current frame and let the user drag a
        rect. The rect is normalized to the *visible* frame, i.e. exactly the
        coordinate space a new `crop` op composes in."""
        if self._crop_mode or self._fid is None:
            return
        self._crop_mode = True
        self._crop_rect = [0.0, 0.0, 1.0, 1.0]
        self._crop_drag = None
        self._fit = True
        self._snap_crop_aspect()
        self.update()

    def set_crop_aspect(self, aspect) -> None:
        """Lock the crop box ratio: None (free), "original", or w/h (in
        pixels; presets follow the photo orientation — 3:2 acts as 2:3 on a
        portrait frame). Snaps the current box when crop mode is active."""
        self._crop_aspect = aspect
        if self._crop_mode:
            self._snap_crop_aspect()
            self.update()

    def _aspect_norm(self) -> float | None:
        """Locked ratio converted to normalized-rect units (rw/rh), or None."""
        if self._crop_aspect is None:
            return None
        dw, dh = self._display_size()
        if self._crop_aspect == "original":
            return 1.0
        a = float(self._crop_aspect)
        if dw < dh:
            a = 1.0 / a          # presets follow the frame orientation
        return a * dh / dw

    def _snap_crop_aspect(self) -> None:
        """Re-shape the current crop box to the locked ratio, keeping its
        center and staying inside the frame."""
        r = self._aspect_norm()
        if r is None:
            return
        x, y, w, h = self._crop_rect
        cx, cy = x + w / 2, y + h / 2
        if w / h > r:
            w = h * r            # target is narrower: keep height
        else:
            h = w / r            # target is wider: keep width
        w, h = max(w, MIN_CROP), max(h, MIN_CROP)
        x = min(max(cx - w / 2, 0.0), 1.0 - w)
        y = min(max(cy - h / 2, 0.0), 1.0 - h)
        self._crop_rect = [x, y, w, h]

    # ------------------------------------------------------ WB eyedropper

    @property
    def in_wb_pick_mode(self) -> bool:
        return self._wb_pick

    def begin_wb_pick(self) -> None:
        """Enter eyedropper mode: the next click on the image emits the
        source-space color under the cursor; Esc cancels."""
        if self._wb_pick or self._vig_pick or self._crop_mode or self._fid is None:
            return
        self._wb_pick = True
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _end_wb_pick(self, rgb: tuple[float, float, float] | None) -> None:
        self._wb_pick = False
        self.unsetCursor()
        if rgb is not None:
            self.wb_picked.emit(*rgb)
        else:
            self.wb_pick_canceled.emit()

    # ------------------------------------------------ vignette placement

    @property
    def in_vig_pick_mode(self) -> bool:
        return self._vig_pick

    def begin_vig_pick(self) -> None:
        """Enter vignette placement: the next click on the image emits its
        visible-frame coords as the new center; Esc cancels. A ring shows
        the current center/radius while the mode is active."""
        if self._vig_pick or self._wb_pick or self._crop_mode or self._fid is None:
            return
        self._vig_pick = True
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def _end_vig_pick(self, pos: QPointF | None) -> None:
        self._vig_pick = False
        self.unsetCursor()
        self.update()
        if pos is not None:
            f = self._frame_rect_logical()
            cx = (pos.x() - f.x()) / max(f.width(), 1.0)
            cy = (pos.y() - f.y()) / max(f.height(), 1.0)
            self.vig_center_picked.emit(min(max(cx, 0.0), 1.0),
                                        min(max(cy, 0.0), 1.0))
        else:
            self.vig_pick_canceled.emit()

    def _paint_vig_overlay(self) -> None:
        """Ring at the current vignette center/radius (falloff midpoint)."""
        f = self._frame_rect_logical()
        cx = f.x() + self._tune.vig_center[0] * f.width()
        cy = f.y() + self._tune.vig_center[1] * f.height()
        half_diag = 0.5 * math.hypot(f.width(), f.height())
        # ring where the falloff weight reaches half: t²(3-2t)·amp = 0.5
        rad = self._tune.vig_radius * half_diag * 0.65
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 140), 3))
        p.drawEllipse(QPointF(cx, cy), rad, rad)
        p.setPen(QPen(QColor(255, 255, 255, 220), 1.5))
        p.drawEllipse(QPointF(cx, cy), rad, rad)
        p.drawEllipse(QPointF(cx, cy), 3, 3)
        p.end()

    def _sample_source_rgb(self, pos: QPointF) -> tuple[float, float, float] | None:
        """Mean sRGB (0..1) of a small source-image patch under a widget
        point, or None outside the frame / before the texture arrived."""
        img = self._sample_image
        if img is None or img.isNull():
            return None
        f = self._frame_rect_logical()
        if f.width() <= 0 or f.height() <= 0 or not f.contains(pos):
            return None
        # visible-frame coords → source UV, exactly as the shader's u_uv
        frame = np.array([(pos.x() - f.x()) / f.width(),
                          (pos.y() - f.y()) / f.height(), 1.0])
        u, v, _ = self._uv_matrix() @ frame
        px = min(max(int(u * img.width()), 0), img.width() - 1)
        py = min(max(int(v * img.height()), 0), img.height() - 1)
        rad = 2  # 5×5 patch: noise-robust, still "the pixel you clicked"
        acc = np.zeros(3)
        n = 0
        for yy in range(max(0, py - rad), min(img.height(), py + rad + 1)):
            for xx in range(max(0, px - rad), min(img.width(), px + rad + 1)):
                c = img.pixelColor(xx, yy)
                acc += (c.redF(), c.greenF(), c.blueF())
                n += 1
        return tuple(acc / n)

    def _finish_crop(self, commit: bool) -> None:
        rect = list(self._crop_rect)
        self._crop_mode = False
        self._crop_drag = None
        self.unsetCursor()
        self.update()
        full = (rect[2] >= 0.999 and rect[3] >= 0.999)
        if commit and not full:
            self.crop_committed.emit([round(v, 4) for v in rect])
        else:
            self.crop_canceled.emit()

    def _frame_rect_logical(self) -> QRectF:
        """Displayed frame in logical widget px."""
        x, y, w, h = self._clamped_rect()
        dpr = self.devicePixelRatioF()
        return QRectF(x / dpr, y / dpr, w / dpr, h / dpr)

    def _crop_rect_logical(self) -> QRectF:
        f = self._frame_rect_logical()
        cx, cy, cw, ch = self._crop_rect
        return QRectF(f.x() + cx * f.width(), f.y() + cy * f.height(),
                      cw * f.width(), ch * f.height())

    def _crop_hit(self, pos: QPointF) -> str | None:
        r = self._crop_rect_logical()
        for name, (hx, hy) in _HANDLES.items():
            px = r.x() + hx * r.width()
            py = r.y() + hy * r.height()
            if abs(pos.x() - px) <= _HANDLE_TOL and abs(pos.y() - py) <= _HANDLE_TOL:
                return name
        return "move" if r.contains(pos) else None

    def _apply_crop_drag(self, handle: str, dx: float, dy: float) -> None:
        """dx/dy are deltas normalized to the frame; start rect in the drag."""
        x, y, w, h = self._crop_drag[2]
        r = None if handle == "move" else self._aspect_norm()
        if handle == "move":
            x = min(max(x + dx, 0.0), 1.0 - w)
            y = min(max(y + dy, 0.0), 1.0 - h)
        else:
            x0, y0, w0, h0 = x, y, w, h
            if handle in _LEFTISH:
                nx = min(max(x + dx, 0.0), x + w - MIN_CROP)
                w, x = x + w - nx, nx
            if handle in _RIGHTISH:
                w = min(max(w + dx, MIN_CROP), 1.0 - x)
            if handle in _TOPPISH:
                ny = min(max(y + dy, 0.0), y + h - MIN_CROP)
                h, y = y + h - ny, ny
            if handle in _BOTTOMISH:
                h = min(max(h + dy, MIN_CROP), 1.0 - y)
            if r is not None:
                x, y, w, h = self._lock_drag_aspect(handle, x0, y0, w0, h0,
                                                    w, h, r)
        self._crop_rect = [x, y, w, h]
        self.update()

    def _lock_drag_aspect(self, handle, x0, y0, w0, h0, nw, nh, r):
        """Re-fit a freely-resized box (nw, nh) to ratio r around the drag
        anchor: corners pin the opposite corner, edges pin the opposite edge
        and stay centered on the perpendicular axis."""
        ax = x0 + w0 if handle in _LEFTISH else x0     # anchored x edge
        ay = y0 + h0 if handle in _TOPPISH else y0     # anchored y edge
        if handle in ("tl", "tr", "bl", "br"):
            if nw * nw / r < nh * nh * r:              # dominant drag axis
                nw = nh * r
            avail_w = ax if handle in _LEFTISH else 1.0 - ax
            avail_h = ay if handle in _TOPPISH else 1.0 - ay
            nw = min(nw, avail_w, avail_h * r)
            nw = max(nw, MIN_CROP)
            nh = nw / r
        elif handle in ("l", "r"):                     # width drives height
            cy = y0 + h0 / 2
            nh = min(nw / r, 2 * cy, 2 * (1.0 - cy))
            nh = max(nh, MIN_CROP)
            nw = nh * r
            ay = cy - nh / 2
        else:                                          # t/b: height drives
            cx = x0 + w0 / 2
            nw = min(nh * r, 2 * cx, 2 * (1.0 - cx))
            nw = max(nw, MIN_CROP)
            nh = nw / r
            ax = cx - nw / 2
        x = ax - nw if handle in _LEFTISH else ax
        y = ay - nh if handle in _TOPPISH else ay
        return (min(max(x, 0.0), 1.0 - nw), min(max(y, 0.0), 1.0 - nh),
                nw, nh)

    def _paint_crop_overlay(self) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = self._frame_rect_logical()
        r = self._crop_rect_logical()

        outside = QPainterPath()
        outside.addRect(frame)
        inner = QPainterPath()
        inner.addRect(r)
        p.fillPath(outside.subtracted(inner), QColor(0, 0, 0, 120))

        thirds = QPen(QColor(255, 255, 255, 70), 1)
        p.setPen(thirds)
        for i in (1, 2):
            x = r.x() + r.width() * i / 3
            y = r.y() + r.height() * i / 3
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        p.setPen(QPen(QColor(255, 255, 255, 235), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)

        p.setPen(QPen(QColor(40, 40, 40, 200), 1))
        p.setBrush(QColor(255, 255, 255, 235))
        for hx, hy in _HANDLES.values():
            px = r.x() + hx * r.width()
            py = r.y() + hy * r.height()
            p.drawRect(QRectF(px - 4, py - 4, 8, 8))
        p.end()

    # ---------------------------------------------------------------- GL

    def initializeGL(self) -> None:
        self.context().aboutToBeDestroyed.connect(self._release_gl)
        prog = QOpenGLShaderProgram()
        prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERT_SRC)
        with open(_FRAG_PATH) as f:
            prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, f.read())
        if not prog.link():
            raise RuntimeError(f"shader link failed: {prog.log()}")
        self._program = prog

        self._vao.create()
        self._vao.bind()
        self._vbo.create()
        self._vbo.bind()
        quad = np.array([0, 0, 1, 0, 0, 1, 1, 1], dtype=np.float32)
        self._vbo.allocate(quad.tobytes(), quad.nbytes)
        prog.bind()
        prog.enableAttributeArray(0)
        prog.setAttributeBuffer(0, _GL_FLOAT, 0, 2)
        prog.release()
        self._vao.release()

    def _drop_hold(self, gl_current: bool = False) -> None:
        """Destroy the held outgoing frame's texture (if any). Pass
        gl_current=True when the GL context is already current (paintGL) —
        doneCurrent() there would unbind it mid-paint."""
        if self._hold is None:
            return
        if gl_current:
            self._hold[0].destroy()
        else:
            self.makeCurrent()
            self._hold[0].destroy()
            self.doneCurrent()
        self._hold = None

    def _release_gl(self) -> None:
        self.makeCurrent()
        self._drop_hold(gl_current=True)
        if self._texture is not None:
            self._texture.destroy()
            self._texture = None
        self._curve_tex.destroy()
        self._vig_tex.destroy()
        self._lmap_tex.destroy()
        self._vbo.destroy()
        self._vao.destroy()
        self._program = None
        self.doneCurrent()

    def _upload_pending(self) -> None:
        img = self._pending_image
        self._pending_image = None
        self._drop_hold(gl_current=True)  # the new photo takes over
        if self._texture is not None:
            self._texture.destroy()
        tex = QOpenGLTexture(img, QOpenGLTexture.MipMapGeneration.GenerateMipMaps)
        tex.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
        tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self._texture = tex

    # -- geometry helpers ---------------------------------------------------

    def _effective_tune(self) -> TuneUniforms:
        return self._identity_tune if self._show_original else self._tune

    def _uv_matrix(self) -> np.ndarray:
        return uv_matrix_for(self._geo, self._full_w, self._full_h)

    def _display_size(self) -> tuple[float, float]:
        return _display_size_for(self._geo, self._full_w, self._full_h)

    def _viewport(self) -> tuple[float, float]:
        dpr = self.devicePixelRatioF()
        return self.width() * dpr, self.height() * dpr

    def _fit_scale(self) -> float:
        vw, vh = self._viewport()
        dw, dh = self._display_size()
        return min(vw / dw, vh / dh)

    def _widget_center(self) -> QPointF:
        vw, vh = self._viewport()
        return QPointF(vw / 2, vh / 2)

    def _current_scale(self) -> float:
        return self._fit_scale() if self._fit else self._scale

    def _clamped_rect(self) -> tuple[float, float, float, float]:
        vw, vh = self._viewport()
        dw, dh = self._display_size()
        s = self._current_scale()
        w, h = dw * s, dh * s
        if w <= vw:
            x = (vw - w) / 2
        else:
            x = min(0.0, max(vw - w, self._pan.x()))
        if h <= vh:
            y = (vh - h) / 2
        else:
            y = min(0.0, max(vh - h, self._pan.y()))
        self._pan = QPointF(x, y)
        return x, y, w, h

    def changeEvent(self, ev) -> None:
        if ev.type() == QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(ev)

    def paintGL(self) -> None:
        f = self.context().functions()
        bg = self.palette().color(QPalette.ColorRole.Window)
        f.glClearColor(bg.redF(), bg.greenF(), bg.blueF(), 1.0)
        f.glClear(_GL_COLOR_BUFFER_BIT)
        if self._pending_image is not None:
            self._upload_pending()
        if self._program is None:
            return
        held = self._hold if self._texture is None else None
        if held is not None:
            # The next photo is still decoding: keep the outgoing frame on
            # screen, fit-centered (navigation resets to fit anyway), with
            # its own geometry/tune — not the incoming photo's.
            tex, tune, geo, fw, fh, lmap = held
            dw, dh = _display_size_for(geo, fw, fh)
            vw, vh = self._viewport()
            s = min(vw / dw, vh / dh)
            w, h = dw * s, dh * s
            x, y = (vw - w) / 2, (vh - h) / 2
            uv = uv_matrix_for(geo, fw, fh)
            frame = (dw, dh)
        elif self._texture is not None and self._full_w:
            tex, tune, lmap = self._texture, self._effective_tune(), self._lmap
            x, y, w, h = self._clamped_rect()
            vw, vh = self._viewport()
            uv = self._uv_matrix()
            frame = self._display_size()
        else:
            return

        mvp = QMatrix4x4()
        mvp.ortho(0, vw, vh, 0, -1, 1)
        mvp.translate(x, y)
        mvp.scale(w, h)

        prog = self._program
        prog.bind()
        self._vao.bind()
        tex.bind(0)
        prog.setUniformValue("u_mvp", mvp)
        prog.setUniformValue("u_uv", mat3_uniform(uv))
        set_tune_uniforms(prog, tune, self._curve_tex,
                          self._lmap_tex, lmap, self._vig_tex, frame)
        f.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        self._vig_tex.release(3)
        self._lmap_tex.release(2)
        self._curve_tex.release(1)
        tex.release(0)
        self._vao.release()
        prog.release()

        if held is not None:
            return  # overlays reference the incoming photo, not this frame
        if self._crop_mode:
            self._paint_crop_overlay()
        if self._vig_pick:
            self._paint_vig_overlay()
        if self._show_original:
            self._paint_original_badge()

    def _paint_original_badge(self) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = p.font()
        font.setBold(True)
        p.setFont(font)
        text = "Original"
        fm = p.fontMetrics()
        bg = QRectF(12, 12, fm.horizontalAdvance(text) + 16, fm.height() + 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(bg, 6, 6)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(bg, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    # -- interaction -------------------------------------------------------------

    def _set_scale(self, new_scale: float, anchor: QPointF) -> None:
        new_scale = max(MIN_SCALE, min(MAX_SCALE, new_scale))
        old = self._current_scale()
        x, y, _, _ = self._clamped_rect()
        ratio = new_scale / old
        self._pan = QPointF(anchor.x() - (anchor.x() - x) * ratio,
                            anchor.y() - (anchor.y() - y) * ratio)
        self._scale = new_scale
        self._fit = False
        self._maybe_request_full()
        self.update()

    def _maybe_request_full(self) -> None:
        if (self._fid is not None and self._tex_level < 2
                and not self._full_requested and self._texture is not None):
            tex_long = max(self._texture.width(), self._texture.height())
            full_long = max(self._full_w, self._full_h)
            if full_long and self._current_scale() > tex_long / full_long + 0.01:
                self._full_requested = True
                self.needs_full_res.emit(self._fid)

    def wheelEvent(self, ev) -> None:
        if self._crop_mode:
            return
        steps = ev.angleDelta().y() / 120.0
        if not steps:
            return
        anchor = QPointF(ev.position().x() * self.devicePixelRatioF(),
                         ev.position().y() * self.devicePixelRatioF())
        self._set_scale(self._current_scale() * (1.25 ** steps), anchor)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.RightButton:
            if (self._fid is not None and not self._crop_mode
                    and not self._wb_pick and not self._vig_pick):
                self._show_original = True
                self.update()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._crop_mode:
            handle = self._crop_hit(ev.position())
            if handle:
                self._crop_drag = (handle, ev.position(), list(self._crop_rect))
            return
        if self._wb_pick:
            rgb = self._sample_source_rgb(ev.position())
            if rgb is not None:          # clicks off the image keep the mode
                self._end_wb_pick(rgb)
            return
        if self._vig_pick:
            if self._frame_rect_logical().contains(ev.position()):
                self._end_vig_pick(ev.position())
            return
        self._drag_start = ev.position()
        self._pan_start = QPointF(self._pan)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, ev) -> None:
        if self._crop_mode:
            if self._crop_drag is not None:
                frame = self._frame_rect_logical()
                delta = ev.position() - self._crop_drag[1]
                self._apply_crop_drag(self._crop_drag[0],
                                      delta.x() / max(frame.width(), 1.0),
                                      delta.y() / max(frame.height(), 1.0))
            else:
                hit = self._crop_hit(ev.position())
                if hit:
                    self.setCursor(_CROP_CURSORS[hit])
                else:
                    self.unsetCursor()
            return
        if self._wb_pick or self._vig_pick:
            return                        # keep the cross cursor, no panning
        if self._drag_start is not None:
            dpr = self.devicePixelRatioF()
            delta = (ev.position() - self._drag_start) * dpr
            self._pan = self._pan_start + delta
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.RightButton:
            if self._show_original:
                self._show_original = False
                self.update()
            return
        if self._crop_mode:
            self._crop_drag = None
            return
        if self._wb_pick:
            return
        self._drag_start = None
        self.unsetCursor()

    def keyPressEvent(self, ev) -> None:
        k = ev.key()
        if self._crop_mode:
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_crop(True)
            elif k == Qt.Key.Key_Escape:
                self._finish_crop(False)
            ev.accept()
            return
        if self._wb_pick and k == Qt.Key.Key_Escape:
            self._end_wb_pick(None)
            ev.accept()
            return
        if self._vig_pick and k == Qt.Key.Key_Escape:
            self._end_vig_pick(None)
            ev.accept()
            return
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.nav_requested.emit(-1)
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Space):
            self.nav_requested.emit(1)
        elif k == Qt.Key.Key_Z:
            self.toggle_fit()
        elif k == Qt.Key.Key_Escape:
            self.close_requested.emit()
        else:
            super().keyPressEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:
        if self._crop_mode:
            self._finish_crop(True)
            return
        if self._fit:
            anchor = QPointF(ev.position().x() * self.devicePixelRatioF(),
                             ev.position().y() * self.devicePixelRatioF())
            self._set_scale(1.0, anchor)
        else:
            self._fit = True
            self.update()
