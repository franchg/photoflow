"""App version, read from pyproject.toml — the single source of truth.

The build workflow's version gate keeps release tags in sync with it, and
photoflow.spec bundles the file so packaged builds resolve it too (from the
PyInstaller extraction dir instead of the source tree).
"""
from __future__ import annotations

import os
import sys
import tomllib

_UNKNOWN = "unknown"


def app_version() -> str:
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(base, "pyproject.toml"), "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return _UNKNOWN
