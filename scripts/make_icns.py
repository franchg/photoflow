"""Render the in-code aperture SVG to build/photoflow.icns (macOS bundle
icon). Uses iconutil, so this only runs on macOS; the spec treats the icon
as optional, so a failure here only costs the icon, never the build.
"""
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QGuiApplication

import styles


def main() -> int:
    QGuiApplication(sys.argv)
    iconset = os.path.join("build", "photoflow.iconset")
    os.makedirs(iconset, exist_ok=True)
    for size in (16, 32, 64, 128, 256, 512):
        styles.write_app_icon(
            os.path.join(iconset, f"icon_{size}x{size}.png"), size)
        styles.write_app_icon(
            os.path.join(iconset, f"icon_{size}x{size}@2x.png"), size * 2)
    out = os.path.join("build", "photoflow.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
