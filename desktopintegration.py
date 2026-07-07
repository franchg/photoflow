"""Desktop integration. Linux: launcher entry, theme icons, and the xdg
default-viewer association for JPEG/PNG. Windows: HKCU registration
(Applications key + ProgID + Capabilities) so Explorer's "Open with" lists
photoflow by name and it appears under Settings → Default apps. UI-free —
app.py wraps these in dialogs/status messages."""
from __future__ import annotations

import os
import subprocess
import sys

import styles

# The dev-run Exec target; the frozen binary uses sys.executable instead.
_APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")

# RAW mimes make "Open with" offer photoflow for camera files; the
# *default*-viewer button stays JPEG/PNG only.
MIME_TYPES = (
    "image/jpeg", "image/png",
    "image/x-adobe-dng", "image/x-canon-cr2", "image/x-canon-cr3",
    "image/x-nikon-nef", "image/x-nikon-nrw", "image/x-sony-arw",
    "image/x-fuji-raf", "image/x-olympus-orf", "image/x-panasonic-rw2",
    "image/x-pentax-pef", "image/x-samsung-srw",
)


def desktop_entry(exec_cmd: str) -> str:
    """The .desktop file content — shared by the self-registration below
    and the .deb package (scripts/make_deb.py)."""
    return "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        "Name=photoflow",
        "Comment=Fast photo browser, culling and non-destructive editor",
        f"Exec={exec_cmd} %F",
        "Icon=photoflow",
        "Terminal=false",
        "Categories=Graphics;Photography;Viewer;",
        "MimeType=" + ";".join(MIME_TYPES) + ";",
        "StartupWMClass=photoflow",
    )) + "\n"


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
        if getattr(sys, "frozen", False) and sys.executable.startswith("/usr/"):
            return  # package-managed install: the .deb ships a system entry
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
    entry = desktop_entry(exec_cmd)
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


# ---------------------------------------------------------------- Windows

WINDOWS_PROGID = "photoflow.image"
_WIN_APP_KEY = r"Software\Classes\Applications\photoflow.exe"
_WIN_PROG_KEY = r"Software\Classes" + "\\" + WINDOWS_PROGID
_WIN_CAPS_KEY = r"Software\photoflow\Capabilities"


def windows_registry_spec(exe: str) -> list[tuple[str, str | None, str]]:
    """The HKCU registration as data: (subkey, value name (None = the
    key's default value), string data) triples. Split out so the test
    suite can check it on any platform."""
    from decode import SCAN_EXTENSIONS
    cmd = f'"{exe}" "%1"'
    icon = f'"{exe}",0'
    spec = [
        (_WIN_APP_KEY, "FriendlyAppName", "photoflow"),
        (_WIN_APP_KEY + r"\shell\open\command", None, cmd),
        (_WIN_APP_KEY + r"\DefaultIcon", None, icon),
        (_WIN_PROG_KEY, None, "Image (photoflow)"),
        (_WIN_PROG_KEY + r"\shell\open\command", None, cmd),
        (_WIN_PROG_KEY + r"\DefaultIcon", None, icon),
        (_WIN_CAPS_KEY, "ApplicationName", "photoflow"),
        (_WIN_CAPS_KEY, "ApplicationDescription",
         "Fast photo browsing, culling and non-destructive editing"),
        # Settings → Default apps discovers photoflow through this pointer
        (r"Software\RegisteredApplications", "photoflow", _WIN_CAPS_KEY),
    ]
    for ext in sorted(SCAN_EXTENSIONS):
        spec.append((_WIN_APP_KEY + r"\SupportedTypes", ext, ""))
        spec.append((_WIN_CAPS_KEY + r"\FileAssociations", ext,
                     WINDOWS_PROGID))
    return spec


def register_windows_app(exe: str | None = None, *,
                         force: bool = False) -> bool:
    """Register photoflow in HKCU (no admin rights involved) so "Open
    with" offers it by name and Settings → Default apps lists it. Runs on
    every frozen start, like the Linux .desktop entry, so the registered
    command follows the exe if its folder moves. Actually *becoming* the
    default stays a user click — Windows forbids apps defaulting
    themselves, by design.
    """
    if sys.platform != "win32":
        return False
    if not (force or getattr(sys, "frozen", False)):
        return False
    import ctypes
    import winreg

    exe = os.path.realpath(exe or sys.executable)
    try:
        changed = False
        for subkey, name, data in windows_registry_spec(exe):
            with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER, subkey, 0,
                    winreg.KEY_READ | winreg.KEY_WRITE) as key:
                try:
                    if winreg.QueryValueEx(key, name)[0] == data:
                        continue
                except OSError:
                    pass
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
                changed = True
        if changed:
            # SHCNE_ASSOCCHANGED: tell Explorer the associations moved
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        return True
    except OSError:
        return False
