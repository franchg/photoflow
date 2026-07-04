"""Single-image viewer: QOpenGLWidget + one textured quad.

The texture holds the *unedited*, orientation-normalized decode at (at least)
screen resolution. The edit stack is applied per-frame in the fragment shader
(tune) and in the UV/vertex transforms (rotate/crop), so slider drags re-render
at 60 fps without touching pixels. Geometry math matches render.apply_geometry.

Coordinates: y-down ortho, quad positions in [0,1]²; uv==a_pos maps texel row 0
(QImage top row) to screen top — no image mirroring needed anywhere.
"""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QMatrix3x3, QMatrix4x4, QPainter,
                           QPainterPath, QPalette, QPen, QSurfaceFormat,
                           QVector3D)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
                              QOpenGLTexture, QOpenGLVertexArrayObject)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from editstack import EditStack, Geometry
from render import TuneUniforms

_GL_FLOAT = 0x1406
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_TRIANGLE_STRIP = 0x0005

_VERT_SRC = """
#version 330 core
layout(location = 0) in vec2 a_pos;
uniform mat4 u_mvp;
uniform mat3 u_uv;
out vec2 v_uv;
void main() {
    v_uv = (u_uv * vec3(a_pos, 1.0)).xy;
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


def set_tune_uniforms(prog: QOpenGLShaderProgram, tune) -> None:
    """Shared by the viewer and the shader parity test. Scalars go through
    setUniformValue1f/1i — PySide6's (str, float) overload is broken."""
    prog.setUniformValue1i("u_tex", 0)
    prog.setUniformValue1f("u_exp_gain", float(tune.exp_gain))
    prog.setUniformValue("u_wb", QVector3D(*[float(v) for v in tune.wb]))
    prog.setUniformValue1f("u_contrast", float(tune.contrast))
    prog.setUniformValue1f("u_hl", float(tune.highlights))
    prog.setUniformValue1f("u_sh", float(tune.shadows))
    prog.setUniformValue1f("u_sat", float(tune.saturation))
    prog.setUniformValue("u_hue", mat3_uniform(tune.hue_mat))


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)  # hover cursors over crop handles

        self._program: QOpenGLShaderProgram | None = None
        self._vao = QOpenGLVertexArrayObject()
        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._texture: QOpenGLTexture | None = None
        self._pending_image = None       # QImage waiting for upload in paintGL
        self._tex_level = -1             # workers.VIEWER_* level of the texture

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

    # ------------------------------------------------------------------ API

    def show_image(self, fid: int, full_w: int, full_h: int,
                   stack: EditStack | None) -> None:
        """Switch to a new photo. Texture arrives later via set_texture_image."""
        self._fid = fid
        self._full_w, self._full_h = full_w, full_h
        self._pending_image = None
        self._full_requested = False
        self._tex_level = -1
        if self._crop_mode:              # navigating away drops the crop
            self._crop_mode = False
            self._crop_drag = None
            self.unsetCursor()
        self._release_texture()
        self.set_stack(stack, _repaint=False)
        self._fit = True
        self.update()

    def set_texture_image(self, fid: int, image, level: int) -> None:
        if fid != self._fid:
            return
        if level < self._tex_level:
            return  # never replace a texture with a lesser one
        self._pending_image = image
        self._tex_level = level
        if not self._full_w:
            self._full_w, self._full_h = image.width(), image.height()
        self.update()

    def set_stack(self, stack: EditStack | None, _repaint: bool = True) -> None:
        stack = stack or EditStack()
        self._tune = TuneUniforms(stack.folded_tune())
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
        self.update()

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
        if handle == "move":
            x = min(max(x + dx, 0.0), 1.0 - w)
            y = min(max(y + dy, 0.0), 1.0 - h)
        else:
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
        self._crop_rect = [x, y, w, h]
        self.update()

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

    def _release_texture(self) -> None:
        if self._texture is not None:
            self.makeCurrent()
            self._texture.destroy()
            self.doneCurrent()
            self._texture = None

    def _release_gl(self) -> None:
        self.makeCurrent()
        if self._texture is not None:
            self._texture.destroy()
            self._texture = None
        self._vbo.destroy()
        self._vao.destroy()
        self._program = None
        self.doneCurrent()

    def _upload_pending(self) -> None:
        img = self._pending_image
        self._pending_image = None
        if self._texture is not None:
            self._texture.destroy()
        tex = QOpenGLTexture(img, QOpenGLTexture.MipMapGeneration.GenerateMipMaps)
        tex.setMinificationFilter(QOpenGLTexture.Filter.LinearMipMapLinear)
        tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self._texture = tex

    # -- geometry helpers ---------------------------------------------------

    def _display_size(self) -> tuple[float, float]:
        """Displayed region size in full-res source pixels (rotate + crop)."""
        w, h = self._full_w, self._full_h
        if self._geo.cw_degrees % 180:
            w, h = h, w
        _, _, cw, ch = self._geo.rect
        return max(w * cw, 1.0), max(h * ch, 1.0)

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
        if self._texture is None or self._program is None or not self._full_w:
            return

        x, y, w, h = self._clamped_rect()
        vw, vh = self._viewport()

        mvp = QMatrix4x4()
        mvp.ortho(0, vw, vh, 0, -1, 1)
        mvp.translate(x, y)
        mvp.scale(w, h)

        cx, cy, cw, ch = self._geo.rect
        crop = np.array([[cw, 0, cx], [0, ch, cy], [0, 0, 1]], dtype=np.float64)
        uv = _ROT_INV[self._geo.cw_degrees % 360] @ crop

        prog = self._program
        prog.bind()
        self._vao.bind()
        self._texture.bind(0)
        prog.setUniformValue("u_mvp", mvp)
        prog.setUniformValue("u_uv", mat3_uniform(uv))
        set_tune_uniforms(prog, self._tune)
        f.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        self._texture.release(0)
        self._vao.release()
        prog.release()

        if self._crop_mode:
            self._paint_crop_overlay()

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
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._crop_mode:
            handle = self._crop_hit(ev.position())
            if handle:
                self._crop_drag = (handle, ev.position(), list(self._crop_rect))
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
        if self._drag_start is not None:
            dpr = self.devicePixelRatioF()
            delta = (ev.position() - self._drag_start) * dpr
            self._pan = self._pan_start + delta
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if self._crop_mode:
            self._crop_drag = None
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
