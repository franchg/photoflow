"""App-wide theming: System restores the platform style/palette/font
captured at startup; Light/Dark apply the token-driven palette + QSS from
styles.py plus the bundled compact UI font."""
from __future__ import annotations

import os
import sys

from PySide6.QtGui import QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

import styles

THEMES = ("system", "light", "dark")

_native_style: str | None = None
_native_palette: QPalette | None = None
_native_font: QFont | None = None
_fonts_loaded = False


def _load_bundled_fonts() -> None:
    """Register fonts/*.ttf with Qt (idempotent). In frozen builds the
    fonts dir sits in the bundle root (_MEIPASS)."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    font_dir = os.path.join(base, "fonts")
    if os.path.isdir(font_dir):
        for name in sorted(os.listdir(font_dir)):
            if name.endswith((".ttf", ".otf")):
                QFontDatabase.addApplicationFont(os.path.join(font_dir, name))


def capture_native_theme(app: QApplication) -> None:
    """Remember the platform's style, palette and font before we touch
    anything, so 'System' can always be restored at runtime."""
    global _native_style, _native_palette, _native_font
    _native_style = app.style().objectName()
    _native_palette = QPalette(app.palette())
    _native_font = QFont(app.font())


def apply_theme(app: QApplication, mode: str) -> None:
    if mode in ("light", "dark"):
        _load_bundled_fonts()
        tokens = styles.DARK if mode == "dark" else styles.LIGHT
        # Default font for metrics; the QSS repeats it because platform
        # themes (GNOME) override per-class fonts, and stylesheets win.
        font = QFont()
        font.setFamilies(styles.FONT_FAMILIES)
        font.setPointSizeF(styles.FONT_POINT_SIZE)
        app.setFont(font)
        app.setStyle("Fusion")
        app.setPalette(styles.make_palette(tokens))
        app.setStyleSheet(styles.build_qss(tokens))
    else:  # system: whatever the platform theme (GTK/KDE/…) provides
        app.setFont(_native_font or QFont())
        app.setStyleSheet("")
        app.setStyle(_native_style or "Fusion")
        app.setPalette(_native_palette or QPalette())
