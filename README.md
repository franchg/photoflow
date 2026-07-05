# photoflow

Fast native app (Linux + Windows) for JPEG/PNG browsing, review/culling,
and light non-destructive editing. See [PLAN.md](PLAN.md) for the technical
spec of the system as built.

## Run

```sh
uv run python app.py [folder | image]
```

A folder argument opens it in the grid; an image argument opens its folder
and shows that image fullscreen — which is what double-clicking an image in
the file manager does once photoflow is the default viewer (see Settings).

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

Packaged builds (no Python required) come from PyInstaller.
`photoflow.spec` produces a windowed one-dir bundle with the native pieces
inside (`turbojpeg`, `jpegtran`, the exiv2 runtime, the GLSL shader; the exe
icon is rendered from the in-code SVG by `scripts/make_ico.py`); with
`PHOTOFLOW_ONEFILE=1` it produces a single self-extracting binary instead.
The `build` GitHub Actions workflow makes both release archives — a Windows
one-dir zip and a Linux one-file binary (built on the oldest LTS runner for
glibc compatibility, `.tar.gz` to keep the executable bit) — on manual
dispatch or a `v*` tag, which also attaches them to the GitHub release.
Each platform job runs the headless test suite first as a compatibility
gate.

On first launch the packaged Linux binary registers itself with the
desktop — a launcher entry plus the aperture icon under
`~/.local/share` — so the dock/taskbar shows the real logo (on Wayland
that icon can only come from a `.desktop` entry) and photoflow appears in
the app launcher. The entry's `Exec=` is refreshed on every start, so
moving the binary is fine.

## macOS

Releases include `photoflow-macos-arm64.zip` — a `photoflow.app` bundle
for Apple Silicon (all Macs since 2020), built and gate-tested by the same
workflow. The build is **unsigned** (no Apple Developer ID), and macOS 15
Sequoia removed the old right-click → Open bypass, so the first launch
needs one of:

```sh
xattr -dr com.apple.quarantine photoflow.app
```

or the GUI route: double-click (it gets blocked), then System Settings →
Privacy & Security → scroll down to "photoflow was blocked" → **Open
Anyway**. Either is one-time.

Running from source works too: `brew install jpeg-turbo`, then the usual
`uv sync` / `uv run python app.py` (the Apple Silicon Homebrew lib path is
picked up automatically).

## Settings

**Settings…** (Ctrl+,) covers:

- **Theme** — *System* (follows your platform GTK/Qt theme, font included —
  the default), *Light*, or *Dark*. Light/Dark are token-driven modern
  themes (`styles.py`: one palette + QSS source of truth) using the bundled
  IBM Plex Sans Condensed UI font (`fonts/`, SIL OFL) at a compact 10 pt.
  Icons are feather-style SVGs embedded in code and tinted to the active
  theme at runtime. **UI scale** (90–150 %) scales the whole interface on
  top of the system DPI scaling; it applies on restart (the dialog offers
  one).
- **Browsing** — show hidden files and folders (folder tree + folder scans).
- **File associations** (Linux) — make photoflow the system default viewer
  for JPEG and PNG (`xdg-mime`; registers the launcher entry first, so it
  also works from a source checkout).
- **Catalog** — relocate the SQLite catalog (a new/existing catalog is opened
  at the chosen path; data is not moved); **clear the thumbnail cache**
  (compacts the file, edits/ratings/flags are kept); **remove missing
  files** — scans every cataloged path with a progress bar and, after
  confirmation, drops entries whose file was moved or deleted (their edits
  go with them); or **empty it** — this permanently deletes all edit stacks,
  ratings, flags and thumbnail caches (never source files) and sits behind
  a hard warning.

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
| C | Interactive crop: drag box/handles, Enter applies, Esc cancels. Aspect presets in the panel (Free/Orig/1:1/3:2/4:3/16:9, orientation-aware) |
| W | White-balance eyedropper: click a spot that should be neutral gray (Esc cancels) |
| V | Vignette: click to place the center; strength (± = darken/brighten edges) and size sliders in the panel |
| Right-click (hold) | Compare with the original: edits bypassed while held (crop stays) |
| Ctrl+Shift+C | Copy edit stack |
| Ctrl+Shift+V | Paste edits onto selection (replace) |
| Ctrl+Alt+Shift+V | Paste edits onto selection (append) |
| Ctrl+L | Apply last edit op to selection |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo edits (per image) |
| Ctrl+E | Export… |
| F1 / ? | Keyboard-shortcut help (also the Help toolbar button) |

## Architecture notes

- Edits are non-destructive stacks (JSON in SQLite); source JPEGs are never
  modified. Preview runs a single-pass GLSL shader; export runs the identical
  math in numpy (`render.py` owns both, `tests/verify_shader.py` proves parity).
- Rotation: the 90° button plus a free-angle slider (−180…180, 0.1° steps).
  Multiples of 90 stay exact (and export losslessly via jpegtran); any other
  angle resamples and auto-crops to the largest same-aspect frame, exactly
  like a straighten tool.
- The tune op covers the Snapseed set: brightness, contrast, saturation,
  ambiance, highlights, shadows, temperature, tint. The
  brightness/contrast/warmth slider responses are calibrated to match
  Snapseed's Tune Image tone tables (within ~1–2/255 across the range):
  brightening never clips white, contrast rolls off softly, warming
  shifts mid-tones while highlights keep their color. Ambiance is a true
  local tone map calibrated by measuring Snapseed itself
  (`tools/ambiance_calib.py`): the same pixel value opens up when its
  neighborhood is dark and calms down when it is bright, plus a vibrance
  that boosts muted colors far more than already-saturated ones. Highlights/shadows apply a luma-masked gain
  so lifted shadows keep their color. The white-balance eyedropper (W, or
  the WB button) samples the clicked source pixel and solves
  temperature/tint against the warmth curves so that pixel renders exactly
  neutral — every stage after white balance maps neutral to neutral.
- Color: sRGB is the working space. ICC-tagged sources (Adobe RGB cameras,
  Display P3 phones) convert to sRGB at decode — one spot in `decode.py`
  covers thumbs, viewer and export alike. Exports are **always sRGB** for
  maximum compatibility: tagged explicitly (sRGB ICC + EXIF ColorSpace),
  source profiles never copied, and tagged sources skip the byte-copy /
  lossless-rotate shortcuts so pixels actually get converted. Thumbnails
  cached before this feature refresh after Ctrl+R or Settings → Clear
  thumbnail cache.
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
