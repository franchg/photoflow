# PyInstaller spec for photoflow (windowed). One-dir by default; set
# PHOTOFLOW_ONEFILE=1 for a single self-extracting binary (the Linux
# release build — slower startup, but one file to ship).
#
# Native pieces that PyInstaller cannot discover on its own:
#   - turbojpeg (loaded via ctypes by PyTurboJPEG) — path taken from
#     $TURBOJPEG_DLL, defaulting to the official Windows installer location.
#     decode.py looks for it in the bundle dir when frozen.
#   - jpegtran (subprocess, optional) — same treatment; its absence only
#     disables lossless-rotation exports.
#   - pyexiv2 ships its own exiv2 native lib → collect_all.
#   - shaders/adjust.frag is read from disk relative to views/viewer.py,
#     so it must land at the same relative path inside the bundle.
#
# The exe icon is optional: scripts/make_ico.py renders build/photoflow.ico
# from the in-code SVG; without it the build simply has no icon.
import os
import sys

from PyInstaller.utils.hooks import collect_all

pyexiv2_datas, pyexiv2_binaries, pyexiv2_hidden = collect_all("pyexiv2")

binaries = list(pyexiv2_binaries)
datas = list(pyexiv2_datas) + [("shaders/adjust.frag", "shaders")]

_DEFAULT_TJ = r"C:\libjpeg-turbo64\bin\turbojpeg.dll" if sys.platform == "win32" else ""
_DEFAULT_JT = r"C:\libjpeg-turbo64\bin\jpegtran.exe" if sys.platform == "win32" else ""
for env, default in (("TURBOJPEG_DLL", _DEFAULT_TJ), ("JPEGTRAN_EXE", _DEFAULT_JT)):
    p = os.environ.get(env, default)
    if p and os.path.exists(p):
        binaries.append((p, "."))

a = Analysis(
    ["app.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=pyexiv2_hidden,
    excludes=["tkinter"],
)

if sys.platform.startswith("linux"):
    # The graphics/session stack must resolve from the *user's* system, never
    # from the build machine: a bundled (older) glib/EGL/wayland shadows the
    # system's, and then Mesa EGL and gio modules fail to load against it —
    # "EGL not available" on Wayland means the window never appears at all.
    # ".so"-anchored so Qt plugins like libdrm-egl-server.so don't match
    _system_lib_prefixes = (
        "libEGL.so", "libGL.so", "libGLX.so", "libGLX_", "libGLdispatch.so",
        "libOpenGL.so", "libglapi.so", "libgbm.so", "libdrm.so",
        "libwayland-client.so", "libwayland-cursor.so", "libwayland-egl.so",
        "libwayland-server.so",
        "libglib-2.0.so", "libgobject-2.0.so", "libgio-2.0.so",
        "libgmodule-2.0.so", "libgthread-2.0.so",
        # ...and everything the system stack itself resolves against: the
        # system Mesa/LLVM/glib load into our process and must find *their*
        # (newer) runtime deps, not our bundled older copies. Proven case: a
        # bundled 22.04 libstdc++ lacks GLIBCXX_3.4.32 that libLLVM needs,
        # killing EGL. All of these exist on any desktop Linux.
        "libstdc++.so", "libgcc_s.so", "libffi.so", "libpcre2-8.so",
        "libz.so", "libzstd.so",
    )
    a.binaries = [
        entry for entry in a.binaries
        if not os.path.basename(entry[0]).startswith(_system_lib_prefixes)
    ]

pyz = PYZ(a.pure)

icon = "build/photoflow.ico" if os.path.exists("build/photoflow.ico") else None

if os.environ.get("PHOTOFLOW_ONEFILE") == "1":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name="photoflow",
        console=False,
        icon=icon,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="photoflow",
        console=False,
        icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="photoflow")
