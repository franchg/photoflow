"""Build the Debian package around the PyInstaller one-file binary.

Run after `PHOTOFLOW_ONEFILE=1 pyinstaller photoflow.spec`:

    uv run python scripts/make_deb.py [--binary dist/photoflow] [--out dist]

Produces dist/photoflow-linux-x64.deb (version-less asset name so the
GitHub releases/latest/download URL stays stable; the real version lives
in the package control fields). The binary is self-contained, so Depends
lists only the system graphics/session libraries the bundle deliberately
leaves to the host (see photoflow.spec).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DEPENDS = ("libegl1, libgl1, libxkbcommon0, libfontconfig1, "
           "libglib2.0-0, libdbus-1-3")

DESCRIPTION = """\
Fast photo browser, culling and non-destructive editor
 Fast photo browsing, culling and non-destructive editing for JPEG, PNG
 and camera RAW files. Point it at a folder for instant thumbnails, cull
 with keyboard ratings and flags, edit with Snapseed-calibrated tune
 sliders, crop, rotation, white balance and vignette, then export; the
 original files are never modified.
"""


def build_control(version: str, installed_size_kb: int) -> str:
    return "".join((
        "Package: photoflow\n",
        f"Version: {version}\n",
        "Section: graphics\n",
        "Priority: optional\n",
        "Architecture: amd64\n",
        f"Installed-Size: {installed_size_kb}\n",
        "Maintainer: Gabriele Franch <franch@gmail.com>\n",
        "Homepage: https://github.com/franchg/photoflow\n",
        f"Depends: {DEPENDS}\n",
        f"Description: {DESCRIPTION}",
    ))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=os.path.join(REPO, "dist", "photoflow"))
    ap.add_argument("--out", default=os.path.join(REPO, "dist"))
    args = ap.parse_args()
    if not os.path.isfile(args.binary):
        sys.exit(f"one-file binary not found: {args.binary} "
                 "(build with PHOTOFLOW_ONEFILE=1 first)")

    with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
        version = tomllib.load(f)["project"]["version"]

    # icon rendering needs a Qt application
    from PySide6.QtGui import QGuiApplication
    _app = QGuiApplication([])  # noqa: F841 — must stay referenced
    import styles
    from desktopintegration import desktop_entry

    root = os.path.join(args.out, "deb-root")
    shutil.rmtree(root, ignore_errors=True)
    usr = os.path.join(root, "usr")
    apps = os.path.join(usr, "share", "applications")
    scalable = os.path.join(usr, "share", "icons", "hicolor", "scalable", "apps")
    sized = os.path.join(usr, "share", "icons", "hicolor", "256x256", "apps")
    doc = os.path.join(usr, "share", "doc", "photoflow")
    for d in (os.path.join(root, "DEBIAN"), os.path.join(usr, "bin"),
              apps, scalable, sized, doc):
        os.makedirs(d)

    shutil.copy2(args.binary, os.path.join(usr, "bin", "photoflow"))
    os.chmod(os.path.join(usr, "bin", "photoflow"), 0o755)
    with open(os.path.join(apps, "photoflow.desktop"), "w") as f:
        f.write(desktop_entry("/usr/bin/photoflow"))
    with open(os.path.join(scalable, "photoflow.svg"), "w") as f:
        f.write(styles.app_icon_svg())
    styles.write_app_icon(os.path.join(sized, "photoflow.png"))
    shutil.copy2(os.path.join(REPO, "LICENSE"),
                 os.path.join(doc, "copyright"))

    # normalize permissions (the build umask must not leak into /usr):
    # directories 755, files 644, the binary 755
    size_kb = 0
    for dirpath, dirs, files in os.walk(root):
        for name in dirs:
            os.chmod(os.path.join(dirpath, name), 0o755)
        for name in files:
            path = os.path.join(dirpath, name)
            os.chmod(path, 0o644)
            size_kb += os.path.getsize(path) // 1024
    os.chmod(os.path.join(usr, "bin", "photoflow"), 0o755)
    with open(os.path.join(root, "DEBIAN", "control"), "w") as f:
        f.write(build_control(version, size_kb))

    out = os.path.join(args.out, "photoflow-linux-x64.deb")
    # xz -1: the payload is an already-compressed PyInstaller binary,
    # higher levels only burn CI minutes
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                    "-Zxz", "-z1", root, out], check=True)
    shutil.rmtree(root)
    print(f"built {out} (version {version})")


if __name__ == "__main__":
    main()
