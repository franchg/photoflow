"""Linux desktop integration: launcher entry, theme icons, and the xdg
default-viewer association for JPEG/PNG. UI-free — app.py wraps these in
dialogs/status messages."""
from __future__ import annotations

import os
import subprocess
import sys

import styles

# The dev-run Exec target; the frozen binary uses sys.executable instead.
_APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")


def register_linux_desktop(data_home: str | None = None, *,
                           force: bool = False) -> None:
    """Install the launcher entry + theme icons for the packaged Linux binary.

    On Wayland the dock/taskbar icon comes from a .desktop file matched to
    the window's app id — a window icon alone shows as a generic gear. Runs
    on every frozen start so Exec= follows the binary if it moves. force=True
    (the default-viewer button) registers dev runs too; data_home overrides
    the destination (tests).
    """
    if sys.platform != "linux":
        return
    if data_home is None:
        if not (force or getattr(sys, "frozen", False)):
            return
        data_home = os.environ.get("XDG_DATA_HOME",
                                   os.path.expanduser("~/.local/share"))
    apps_dir = os.path.join(data_home, "applications")
    scalable = os.path.join(data_home, "icons", "hicolor", "scalable", "apps")
    sized = os.path.join(data_home, "icons", "hicolor", "256x256", "apps")
    for d in (apps_dir, scalable, sized):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(scalable, "photoflow.svg"), "w") as f:
        f.write(styles.app_icon_svg())
    png = os.path.join(sized, "photoflow.png")
    if not os.path.exists(png):
        styles.write_app_icon(png)
    if getattr(sys, "frozen", False):
        exec_cmd = f'"{os.path.realpath(sys.executable)}"'
    else:  # dev run registered via the default-viewer button
        exec_cmd = f'"{sys.executable}" "{_APP_PY}"'
    entry = "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        "Name=photoflow",
        "Comment=Fast JPEG/PNG browser, culling and non-destructive editor",
        f"Exec={exec_cmd} %F",
        "Icon=photoflow",
        "Terminal=false",
        "Categories=Graphics;Photography;Viewer;",
        "MimeType=image/jpeg;image/png;",
        "StartupWMClass=photoflow",
    )) + "\n"
    path = os.path.join(apps_dir, "photoflow.desktop")
    try:
        with open(path) as f:
            if f.read() == entry:
                return
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(entry)


def set_default_viewer() -> tuple[bool, str]:
    """Force-register the desktop entry (dev runs too) and make photoflow
    the xdg default handler for JPEG and PNG. Returns (ok, error)."""
    register_linux_desktop(force=True)
    result = subprocess.run(
        ["xdg-mime", "default", "photoflow.desktop",
         "image/jpeg", "image/png"],
        capture_output=True, text=True)
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.strip() or "xdg-mime failed"
