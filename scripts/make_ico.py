"""Render the in-code aperture SVG to build/photoflow.ico for the Windows exe.

Run by the build workflow before PyInstaller; the spec treats the .ico as
optional, so a failure here only costs the icon, never the build.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

import styles


def main() -> int:
    QGuiApplication(sys.argv)
    img = QImage(256, 256, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(styles._svg("aperture", "#5b8def").encode())).render(painter)
    painter.end()
    os.makedirs("build", exist_ok=True)
    out = os.path.join("build", "photoflow.ico")
    if not img.save(out, "ICO"):
        print("failed to write", out, file=sys.stderr)
        return 1
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
