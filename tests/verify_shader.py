"""GPU↔CPU parity check: renders shaders/adjust.frag into an offscreen FBO
(real GL context, no window) and compares against render.apply_tune, plus the
viewer's UV rotation/crop matrices against render.apply_geometry.

Run:  uv run python tests/verify_shader.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSize
from PySide6.QtGui import (QGuiApplication, QImage, QMatrix4x4,
                           QOffscreenSurface, QOpenGLContext, QVector3D)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLShader, QOpenGLShaderProgram,
                              QOpenGLTexture, QOpenGLVertexArrayObject)

import render
from editstack import EditStack, Op
from views.viewer import (_VERT_SRC, CurveTexture, LmapTexture,
                          default_gl_format, mat3_uniform, set_tune_uniforms,
                          uv_matrix_for)
from workers import np_to_qimage

W, H = 256, 192


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"PASS {name}")


def main():
    app = QGuiApplication(sys.argv)
    fmt = default_gl_format()
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    check("GL context create", ctx.create())
    surf = QOffscreenSurface()
    surf.setFormat(fmt)
    surf.create()
    check("offscreen surface", surf.isValid())
    check("make current", ctx.makeCurrent(surf))
    ver = ctx.format().version()
    print(f"  GL {ver[0]}.{ver[1]} {'core' if ctx.format().profile() else ''}")

    f = ctx.functions()
    prog = QOpenGLShaderProgram()
    check("vertex shader", prog.addShaderFromSourceCode(
        QOpenGLShader.ShaderTypeBit.Vertex, _VERT_SRC), prog.log())
    frag_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "shaders", "adjust.frag")
    check("fragment shader", prog.addShaderFromSourceCode(
        QOpenGLShader.ShaderTypeBit.Fragment, open(frag_path).read()), prog.log())
    check("link", prog.link(), prog.log())

    # deterministic test image
    rng = np.random.default_rng(42)
    src = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)

    tex = QOpenGLTexture(np_to_qimage(src),
                         QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
    tex.setMinificationFilter(QOpenGLTexture.Filter.Nearest)
    tex.setMagnificationFilter(QOpenGLTexture.Filter.Nearest)
    tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)

    vao = QOpenGLVertexArrayObject()
    vao.create()
    vao.bind()
    vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
    vbo.create()
    vbo.bind()
    quad = np.array([0, 0, 1, 0, 0, 1, 1, 1], dtype=np.float32)
    vbo.allocate(quad.tobytes(), quad.nbytes)
    prog.bind()
    prog.enableAttributeArray(0)
    prog.setAttributeBuffer(0, 0x1406, 0, 2)

    curve_tex = CurveTexture()
    vig_tex = CurveTexture()
    lmap_tex = LmapTexture()
    lmap = render.local_mean_luma(src)  # == what apply_tune_uint8 computes

    def gl_render(out_w, out_h, tune, uv3x3):
        fbo = QOpenGLFramebufferObject(QSize(out_w, out_h))
        fbo.bind()
        f.glViewport(0, 0, out_w, out_h)
        mvp = QMatrix4x4()
        mvp.ortho(0, out_w, out_h, 0, -1, 1)
        mvp.scale(out_w, out_h)
        prog.bind()
        vao.bind()
        tex.bind(0)
        prog.setUniformValue("u_mvp", mvp)
        prog.setUniformValue("u_uv", mat3_uniform(uv3x3))
        set_tune_uniforms(prog, tune, curve_tex, lmap_tex, lmap, vig_tex,
                          (out_w, out_h))
        f.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
        img = fbo.toImage().convertToFormat(QImage.Format.Format_RGB888)
        fbo.release()
        bpl = img.bytesPerLine()
        buf = np.frombuffer(img.constBits(), np.uint8, bpl * out_h)
        arr = buf.reshape(out_h, bpl)[:, :out_w * 3].reshape(out_h, out_w, 3)
        return arr.copy()

    # --- tune parity ------------------------------------------------------
    stack = EditStack([Op("tune", {"exposure": 0.35, "contrast": 0.25,
                                   "saturation": 0.4,
                                   "temperature": -0.3, "tint": 0.2,
                                   "ambiance": 0.4,
                                   "highlights": 0.5, "shadows": -0.4}),
                       Op("vignette", {"cx": 0.35, "cy": 0.6,
                                       "radius": 0.8, "strength": -0.6})])
    tune = render.TuneUniforms(stack.folded_tune(), stack.vignette())
    gpu = gl_render(W, H, tune, np.eye(3))
    cpu = render.apply_tune_uint8(src, tune)
    mad = float(np.mean(np.abs(gpu.astype(int) - cpu.astype(int))))
    mx = int(np.max(np.abs(gpu.astype(int) - cpu.astype(int))))
    check("shader == numpy (tune)", mad < 0.6 and mx <= 8,
          f"mad={mad:.3f} max={mx}")

    # --- geometry parity (rotate 90 CW + crop) ------------------------------
    geo_stack = EditStack([Op("rotate", {"degrees": 90}),
                           Op("crop", {"rect": [0.25, 0.125, 0.5, 0.75]})])
    geo = geo_stack.geometry()
    cpu_g = render.apply_geometry(src, geo)
    oh, ow = cpu_g.shape[:2]
    uv = uv_matrix_for(geo, W, H)
    ident = render.TuneUniforms(EditStack().folded_tune())
    gpu_g = gl_render(ow, oh, ident, uv)
    mad = float(np.mean(np.abs(gpu_g.astype(int) - cpu_g.astype(int))))
    check("shader UV == numpy geometry", mad < 1.0, f"mad={mad:.3f}")

    # --- fine-rotation parity (free-angle warp, bilinear both sides) --------
    tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
    tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
    fine_stack = EditStack([Op("rotate", {"degrees": 100.0})])  # 90 + 10 fine
    geo_f = fine_stack.geometry()
    cpu_f = render.apply_geometry(src, geo_f)
    fh, fw = cpu_f.shape[:2]
    gpu_f = gl_render(fw, fh, ident, uv_matrix_for(geo_f, W, H))
    d = np.abs(gpu_f.astype(int) - cpu_f.astype(int))
    mad = float(d.mean())
    check("shader UV == numpy fine rotation", mad < 2.0,
          f"mad={mad:.3f} max={int(d.max())}")

    tex.destroy()
    curve_tex.destroy()
    vig_tex.destroy()
    lmap_tex.destroy()
    vbo.destroy()
    vao.destroy()
    ctx.doneCurrent()
    print("\nSHADER PARITY PASS")


if __name__ == "__main__":
    main()
