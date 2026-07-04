# photoflow

Fast native Linux app for JPEG (and PNG) browsing, review/culling, and light
non-destructive editing. See [PLAN.md](PLAN.md) for the full technical spec
(written JPEG-first; PNG support was added later — see below).

## Run

```sh
uv run python app.py
```

System requirements: `libturbojpeg` (`apt install libturbojpeg`), OpenGL 3.3+,
and optionally `jpegtran` (`libjpeg-turbo-progs`) for lossless rotation export.
Python dependencies are managed by uv (`uv sync`).

## Windows

The code is portable: on Windows, install
[libjpeg-turbo](https://github.com/libjpeg-turbo/libjpeg-turbo/releases)
(the official `-vc-x64.exe` installer; PyTurboJPEG finds the DLL in its
default `C:\libjpeg-turbo64` location) and run the same `uv sync` /
`uv run python app.py`. Trash goes to the Recycle Bin, settings to the
registry, and the catalog/cache to `%LOCALAPPDATA%\photoflow`.

A packaged build (no Python required) comes from PyInstaller:
`photoflow.spec` produces a windowed one-dir bundle with `turbojpeg.dll`,
`jpegtran.exe`, the exiv2 runtime and the GLSL shader inside; the exe icon is
rendered from the in-code SVG by `scripts/make_ico.py`. The
`windows-build` GitHub Actions workflow builds it on `windows-latest`
(manual dispatch, or a `v*` tag which also attaches the zip to a release),
running the headless test suite first as a Windows compatibility gate.

## Settings

**Settings…** (Ctrl+,) covers:

- **Theme** — *System* (follows your platform GTK/Qt theme — the default),
  *Light*, or *Dark*. Light/Dark are token-driven modern themes (`styles.py`:
  one palette + QSS source of truth); System gets no stylesheet so it stays
  native. Icons are feather-style SVGs embedded in code and tinted to the
  active theme at runtime — no binary assets.
- **Browsing** — show hidden files and folders (folder tree + folder scans).
- **Catalog** — relocate the SQLite catalog (a new/existing catalog is opened
  at the chosen path; data is not moved), or **empty it** — this permanently
  deletes all edit stacks, ratings, flags and thumbnail caches (never source
  files) and sits behind a hard warning.

## Keys

| Key | Action |
| --- | --- |
| Ctrl+O | Open folder (or use the collapsible folder tree — “Folders” in the toolbar) |
| Toolbar S/M/L | Thumbnail size button group for the grid (persisted) |
| Ctrl+R / F5 | Force re-scan of the folder + full thumbnail regeneration |
| Ctrl+, | Settings (theme, hidden files, catalog location / empty catalog) |
| Del | Move selected image(s) to the system trash (asks only for multi-selections) |
| Enter / double-click | Open viewer; Esc back to grid |
| ← → / Space (viewer) | Previous / next image |
| F / F11 | Fullscreen view in/out (from the grid: opens current photo fullscreen) |
| 0–5 | Rating (press again to clear) |
| P / X / U | Pick / reject / unflag |
| Z / double-click (viewer) | Fit ↔ 100 %; wheel zooms, drag pans |
| C | Interactive crop: drag box/handles, Enter applies, Esc cancels |
| W | White-balance eyedropper: click a spot that should be neutral gray (Esc cancels) |
| Ctrl+Shift+C | Copy edit stack |
| Ctrl+Shift+V | Paste edits onto selection (replace) |
| Ctrl+Alt+Shift+V | Paste edits onto selection (append) |
| Ctrl+L | Apply last edit op to selection |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo edits (per image) |
| Ctrl+E | Export… |

## Architecture notes

- Edits are non-destructive stacks (JSON in SQLite); source JPEGs are never
  modified. Preview runs a single-pass GLSL shader; export runs the identical
  math in numpy (`render.py` owns both, `tests/verify_shader.py` proves parity).
- The tune op covers the Snapseed set: brightness, contrast, saturation,
  ambiance (+1 opens shadows and boosts color, -1 goes contrasty and muted),
  highlights, shadows, temperature, tint. Highlights/shadows apply a
  luma-masked gain so lifted shadows keep their color. The white-balance
  eyedropper (W, or the WB button) samples the clicked source pixel and
  solves temperature/tint in linear space so that pixel renders exactly
  neutral — every stage after white balance maps neutral to neutral.
- Thumbnails: EXIF-embedded thumbs paint first, then 1/8-scale libjpeg-turbo
  decodes replace them; everything is cached in SQLite keyed by
  `(path, mtime, size)`. The catalog lives in `~/.local/share/photoflow/`.
- PNG: decoded via Qt's codec (`decode.py` dispatches on magic bytes; the rest
  of the app is format-blind). No scaled decode or embedded-thumb stage —
  PNGs full-decode then downscale. Alpha is flattened over white in the
  pipeline; a no-edit export is a byte copy and keeps alpha. Exports stay in
  the source format (PNG in → PNG out, lossless; the quality slider is
  JPEG-only), and the jpegtran lossless-rotate path is JPEG-only.
- All I/O and decoding runs on worker thread pools; the UI thread never
  touches a file or a pixel.

## Tests

```sh
uv run python tests/verify_headless.py   # decode/EXIF/catalog/render/export
uv run python tests/verify_shader.py     # GPU shader ≡ CPU export math
uv run python tests/smoke_gui.py         # offscreen end-to-end app drive
```
