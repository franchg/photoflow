"""Design layer: color tokens, the Light/Dark QSS, palettes, and SVG icons.

Light/Dark are fully token-driven (palette + stylesheet share one source of
truth); System mode gets no stylesheet so the platform theme stays native.
Icons are feather-style line SVGs embedded as strings, tinted at runtime to
the active theme's text color — no binary assets anywhere.
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

DARK = {
    # deliberately low-contrast: dim (not white) text, gently lifted
    # backgrounds with a narrow spread between surfaces, quiet borders
    "bg": "#212125", "base": "#242429", "surface": "#2a2a2f",
    "surface_alt": "#313137", "pressed": "#27272c",
    "border": "#37373e", "border_strong": "#42424b",
    "text": "#cfcfd6", "text_muted": "#8f8f99",
    "accent": "#5b84d6", "accent_soft": "rgba(91, 132, 214, 0.22)",
    "slider_track": "#37373e", "handle": "#bcbcc5", "handle_hover": "#dcdce2",
    "scroll": "#44444d", "scroll_hover": "#55555f",
}

LIGHT = {
    "bg": "#f4f4f7", "base": "#ececef", "surface": "#ffffff",
    "surface_alt": "#e7e7ee", "pressed": "#dfdfe7",
    "border": "#d8d8e0", "border_strong": "#c2c2cc",
    "text": "#1d1d22", "text_muted": "#74747e",
    "accent": "#4f83ea", "accent_soft": "rgba(79, 131, 234, 0.18)",
    "slider_track": "#d9d9e1", "handle": "#ffffff", "handle_hover": "#ffffff",
    "scroll": "#c5c5cf", "scroll_hover": "#adadb9",
}


def make_palette(t: dict) -> QPalette:
    p = QPalette()
    roles = QPalette.ColorRole
    p.setColor(roles.Window, QColor(t["bg"]))
    p.setColor(roles.WindowText, QColor(t["text"]))
    p.setColor(roles.Base, QColor(t["base"]))
    p.setColor(roles.AlternateBase, QColor(t["surface"]))
    p.setColor(roles.Text, QColor(t["text"]))
    p.setColor(roles.Button, QColor(t["surface"]))
    p.setColor(roles.ButtonText, QColor(t["text"]))
    p.setColor(roles.Highlight, QColor(t["accent"]))
    p.setColor(roles.HighlightedText, QColor("#ffffff"))
    p.setColor(roles.ToolTipBase, QColor(t["surface"]))
    p.setColor(roles.ToolTipText, QColor(t["text"]))
    p.setColor(roles.PlaceholderText, QColor(t["text_muted"]))
    return p


# ---------------------------------------------------------------------- icons

_STROKE = ('fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" '
           'stroke-linejoin="round"')

ICON_SVGS = {
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1'
              '-2 2H5a2 2 0 0 1-2-2z"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/>'
               '<line x1="9" y1="4" x2="9" y2="20"/>',
    "export": '<path d="M12 15V4"/><polyline points="8 8 12 4 16 8"/>'
              '<path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    "settings": '<line x1="4" y1="7" x2="20" y2="7"/><circle cx="9" cy="7" r="2.5"/>'
                '<line x1="4" y1="17" x2="20" y2="17"/><circle cx="15" cy="17" r="2.5"/>',
    "rotate": '<polyline points="21 4 21 10 15 10"/>'
              '<path d="M19.5 15a8 8 0 1 1-1.9-8.4L21 10"/>',
    "rotate-ccw": '<polyline points="3 4 3 10 9 10"/>'
                  '<path d="M4.5 15a8 8 0 1 0 1.9-8.4L3 10"/>',
    "crop": '<path d="M6 2v14a2 2 0 0 0 2 2h14"/>'
            '<path d="M2 6h14a2 2 0 0 1 2 2v14"/>',
    "pipette": '<path d="m2 22 1-1h3l9-9"/><path d="M3 21v-3l9-9"/>'
               '<path d="m15 6 3.4-3.4a2.1 2.1 0 1 1 3 3L18 9l.4.4'
               'a2.1 2.1 0 1 1-3 3l-3.8-3.8a2.1 2.1 0 1 1 3-3l.4.4Z"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'
             '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "x": '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
    "chevron-up": '<polyline points="6 15 12 9 18 15"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "paste": '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6'
             'a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',
    "paste-plus": '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6'
                  'a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>'
                  '<line x1="12" y1="10" x2="12" y2="18"/>'
                  '<line x1="8" y1="14" x2="16" y2="14"/>',
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "help-circle": '<circle cx="12" cy="12" r="10"/>'
                   '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
                   '<line x1="12" y1="17" x2="12.01" y2="17"/>',
    "chevrons-right": '<polyline points="13 17 18 12 13 7"/>'
                      '<polyline points="6 17 11 12 6 7"/>',
    "grid-small": '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                  '<line x1="9" y1="3" x2="9" y2="21"/>'
                  '<line x1="15" y1="3" x2="15" y2="21"/>'
                  '<line x1="3" y1="9" x2="21" y2="9"/>'
                  '<line x1="3" y1="15" x2="21" y2="15"/>',
    "grid-medium": '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                   '<line x1="12" y1="3" x2="12" y2="21"/>'
                   '<line x1="3" y1="12" x2="21" y2="12"/>',
    "grid-large": '<rect x="3" y="3" width="18" height="18" rx="2"/>',
    "check": '<polyline points="5 12 10 17 19 8"/>',
    "aperture": '<circle cx="12" cy="12" r="10"/>'
                '<line x1="14.31" y1="8" x2="20.05" y2="17.94"/>'
                '<line x1="9.69" y1="8" x2="21.17" y2="8"/>'
                '<line x1="7.38" y1="12" x2="13.12" y2="2.06"/>'
                '<line x1="9.69" y1="16" x2="3.95" y2="6.06"/>'
                '<line x1="14.31" y1="16" x2="2.83" y2="16"/>'
                '<line x1="16.62" y1="12" x2="10.88" y2="21.94"/>',
}

_icon_cache: dict[tuple[str, str], QIcon] = {}


def _svg(name: str, color: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'{_STROKE.format(c=color)}>{ICON_SVGS[name]}</svg>')


def themed_icon(name: str, color: QColor | str) -> QIcon:
    color = QColor(color).name()
    key = (name, color)
    if key not in _icon_cache:
        renderer = QSvgRenderer(QByteArray(_svg(name, color).encode()))
        icon = QIcon()
        for size in (16, 20, 24, 32, 48, 64):
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            icon.addPixmap(pm)
        _icon_cache[key] = icon
    return _icon_cache[key]


def app_icon_svg() -> str:
    """The aperture mark as a standalone SVG document (brand blue)."""
    return _svg("aperture", "#5b8def")


def app_icon() -> QIcon:
    return themed_icon("aperture", "#5b8def")


def write_app_icon(path: str, size: int = 256) -> bool:
    """Render the aperture mark to an icon file (.png/.ico by extension) —
    the exe icon on Windows, the theme icon for Linux desktop entries."""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(app_icon_svg().encode())).render(painter)
    painter.end()
    return img.save(path)


# The UI font for the token themes (fonts/ ships the first family; the rest
# are fallbacks). Lives here so build_qss can bake it into the stylesheet —
# platform themes (GNOME) stamp per-class fonts onto buttons/labels that
# override QApplication.setFont, but stylesheet fonts beat class fonts.
FONT_FAMILIES = ["IBM Plex Sans Condensed",
                 "Inter", "Adwaita Sans", "SF Pro Text", "Segoe UI Variable",
                 "Segoe UI", "Ubuntu Sans", "Cantarell", "Noto Sans",
                 "DejaVu Sans"]
FONT_POINT_SIZE = 10.0  # vs the usual 11: every control compacts with it


# ----------------------------------------------------------------------- QSS

def _asset_dir() -> str:
    if sys.platform == "win32":
        base = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local")),
            "photoflow", "cache")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches/photoflow")
    else:
        base = os.path.join(
            os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
            "photoflow")
    d = os.path.join(base, "icons")
    os.makedirs(d, exist_ok=True)
    return d


def _write_asset(name: str, color: str) -> str:
    path = os.path.join(_asset_dir(), f"{name}-{color.lstrip('#')}.svg")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(_svg(name, color))
    return path


def build_qss(t: dict) -> str:
    chevron = _write_asset("chevron-down", t["text_muted"])
    chevron_up = _write_asset("chevron-up", t["text_muted"])
    check = _write_asset("check", "#ffffff")
    overflow = _write_asset("chevrons-right", t["text"])
    families = ", ".join(f'"{f}"' for f in FONT_FAMILIES)
    return f"""
* {{ outline: none; font-family: {families};
    font-size: {FONT_POINT_SIZE:g}pt; }}
QMainWindow, QDialog, QMessageBox {{ background: {t['bg']}; }}

QToolBar {{ background: {t['bg']}; border: none; padding: 5px; spacing: 4px; }}
QToolButton {{ background: transparent; border: none; border-radius: 8px;
    padding: 5px 10px; color: {t['text']}; }}
QToolButton:hover {{ background: {t['surface_alt']}; }}
QToolButton:pressed {{ background: {t['pressed']}; }}
QToolButton:checked {{ background: {t['accent_soft']}; }}

/* toolbar overflow (»): the base style draws its arrow from the native
   palette — invisible on our themes — so give it an explicit themed icon.
   The layout grants it a slim fixed extent: no padding, no border. */
QToolButton#qt_toolbar_ext_button {{
    qproperty-icon: url({overflow});
    background: {t['surface_alt']};
    border: none; border-radius: 4px;
    padding: 0px; }}
QToolButton#qt_toolbar_ext_button:hover {{ background: {t['pressed']}; }}

QPushButton {{ background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: 8px; padding: 6px 14px; color: {t['text']}; }}
QPushButton:hover {{ background: {t['surface_alt']};
    border-color: {t['border_strong']}; }}
QPushButton:pressed {{ background: {t['pressed']}; }}
QPushButton:checked {{ background: {t['accent']}; color: #ffffff;
    border-color: {t['accent']}; }}
QPushButton:disabled {{ color: {t['text_muted']}; }}

QComboBox {{ background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: 8px; padding: 4px 26px 4px 10px; color: {t['text']}; }}
QComboBox:hover {{ border-color: {t['border_strong']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {t['surface']};
    border: 1px solid {t['border']}; border-radius: 8px; padding: 4px;
    selection-background-color: {t['accent']}; selection-color: #ffffff; }}

QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {t['surface']};
    border: 1px solid {t['border']}; border-radius: 8px; padding: 5px 8px;
    color: {t['text']}; selection-background-color: {t['accent']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {t['accent']}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ border: none; width: 18px;
    subcontrol-position: top right; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ border: none; width: 18px;
    subcontrol-position: bottom right; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({chevron_up});
    width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({chevron});
    width: 10px; height: 10px; }}

QSlider::groove:horizontal {{ height: 4px; border-radius: 2px;
    background: {t['slider_track']}; }}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 15px; height: 15px; margin: -6px 0;
    border-radius: 8px; background: {t['handle']};
    border: 1px solid {t['border_strong']}; }}
QSlider::handle:horizontal:hover {{ background: {t['handle_hover']};
    border-color: {t['accent']}; }}

QListView, QListWidget {{ background: {t['base']}; border: none;
    border-radius: 8px; color: {t['text']}; }}
QListWidget::item {{ padding: 3px 4px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {t['accent_soft']};
    color: {t['text']}; }}
QTreeView {{ background: {t['bg']}; border: none; color: {t['text']}; }}
QTreeView::item {{ padding: 2px 4px; border-radius: 6px; }}
QTreeView::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QTreeView::item:hover {{ background: {t['surface_alt']}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['scroll']}; border-radius: 4px;
    min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['scroll_hover']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['scroll']}; border-radius: 4px;
    min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {t['scroll_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {t['border']}; }}
QSplitter::handle:horizontal {{ margin: 0 3px; }}
QSplitter::handle:vertical {{ margin: 3px 0; }}
QSplitter::handle:hover {{ background: {t['accent']}; }}

QGroupBox {{ border: 1px solid {t['border']}; border-radius: 10px;
    margin-top: 12px; padding-top: 6px; color: {t['text']}; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}

QCheckBox {{ color: {t['text']}; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid {t['border_strong']}; background: {t['surface']}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']};
    border-color: {t['accent']}; image: url({check}); }}

QStatusBar {{ background: {t['bg']}; color: {t['text_muted']};
    border-top: 1px solid {t['border']}; }}
QToolTip {{ background: {t['surface']}; color: {t['text']};
    border: 1px solid {t['border']}; padding: 4px 8px; }}

QProgressBar {{ border: none; border-radius: 5px; background: {t['surface_alt']};
    height: 10px; text-align: center; color: {t['text_muted']}; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 5px; }}

QMenu {{ background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 5px 20px; border-radius: 6px; color: {t['text']}; }}
QMenu::item:selected {{ background: {t['accent_soft']}; }}
"""
