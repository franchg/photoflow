"""Render the in-code aperture SVG to build/photoflow.ico for the Windows exe.

Run by the build workflow before PyInstaller; the spec treats the .ico as
optional, so a failure here only costs the icon, never the build.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QGuiApplication

import styles


def main() -> int:
    QGuiApplication(sys.argv)
    os.makedirs("build", exist_ok=True)
    out = os.path.join("build", "photoflow.ico")
    if not styles.write_app_icon(out):
        print("failed to write", out, file=sys.stderr)
        return 1
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
