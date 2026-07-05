"""App-wide theming: System restores the platform style/palette captured at
startup; Light/Dark apply the token-driven palette + QSS from styles.py."""
from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

import styles

THEMES = ("system", "light", "dark")

_native_style: str | None = None
_native_palette: QPalette | None = None


def capture_native_theme(app: QApplication) -> None:
    """Remember the platform's style and palette before we touch anything,
    so 'System' can always be restored at runtime."""
    global _native_style, _native_palette
    _native_style = app.style().objectName()
    _native_palette = QPalette(app.palette())


def apply_theme(app: QApplication, mode: str) -> None:
    if mode in ("light", "dark"):
        tokens = styles.DARK if mode == "dark" else styles.LIGHT
        app.setStyle("Fusion")
        app.setPalette(styles.make_palette(tokens))
        app.setStyleSheet(styles.build_qss(tokens))
    else:  # system: whatever the platform theme (GTK/KDE/…) provides
        app.setStyleSheet("")
        app.setStyle(_native_style or "Fusion")
        app.setPalette(_native_palette or QPalette())
