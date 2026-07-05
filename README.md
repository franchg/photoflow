<p align="center">
  <img src="docs/logo.png" width="88" alt="photoflow logo">
</p>
<h1 align="center">photoflow</h1>
<p align="center">
  Fast photo browsing, culling and non-destructive editing for JPEG &amp; PNG.<br>
  <b>Linux · Windows · macOS</b>
</p>

![Browsing and culling a folder of photos](docs/screenshot-browse.png)

photoflow is the step between a full memory card and the photos you keep.
Point it at a folder and it shows thumbnails instantly; flip through images
full-screen, rate and flag the keepers, fix them up with quick edits that
never touch your original files, and export the results.

## What it is — and isn't

**Built for:** dumping a folder of JPEGs on it and getting through them
fast. Browsing is tuned to feel instant even on large folders; edits
preview on the GPU in real time; everything works from the keyboard.

**Not built to be:** a photo manager or a Lightroom replacement. photoflow
browses one folder at a time — there is no all-your-disk library, no
albums, no face recognition, no cloud.

**Current limitations, so you know before you start:**

- **JPEG and PNG only.** No RAW, HEIC, WebP, TIFF, or video.
- **Edits are global.** Crop, rotate, tune sliders and vignette apply to
  the whole image — there are no local/selective adjustments, healing
  brush, or manual curves.
- **Ratings, flags and edits live in photoflow's own catalog**, not inside
  your files. They survive restarts and folder moves *within* photoflow,
  but they don't travel with files you copy elsewhere, and renaming or
  moving files with other tools disconnects them (Settings offers a
  cleanup for such orphans).
- **Color:** photos tagged with a color profile (Adobe RGB cameras,
  Display P3 phones) are handled correctly, and exports are always
  standard sRGB. Wide-gamut *monitor* calibration is left to the OS.
- The macOS build is **Apple Silicon only** and unsigned (one-time
  security prompt — see below). On Windows, making photoflow the default
  viewer is a manual "Open with" step. Opening a second image from the
  file manager starts a second window.

## Install

### The easy way — download a release

Grab the archive for your platform from the
**[latest release](https://github.com/franchg/photoflow/releases/latest)**.
Everything is bundled; there is nothing else to install.

#### Linux (x86-64)

```sh
tar -xzf photoflow-linux-x64.tar.gz
./photoflow
```

A single self-contained binary. Works on any reasonably recent
distribution (Ubuntu 22.04 or newer, and equivalents) with OpenGL 3.3+.
The first launch registers photoflow in your app launcher and dock with
its icon; you can move the binary wherever you like — the entry follows
it. To make it your system default viewer for JPEG/PNG, use
**Settings → File associations** inside the app.

#### Windows (x86-64)

1. Unzip `photoflow-windows-x64.zip`.
2. Open the `photoflow` folder and run `photoflow.exe`.

The build is unsigned, so SmartScreen may show "Windows protected your
PC" on first run — click **More info → Run anyway** (one-time). To open
images with photoflow by default: right-click an image → **Open with →
Choose another app → photoflow → Always**.

#### macOS (Apple Silicon — all Macs since 2020)

1. Unzip `photoflow-macos-arm64.zip` and (optionally) drag
   `photoflow.app` into Applications.
2. The build is unsigned, so the first launch is blocked. Either run

   ```sh
   xattr -dr com.apple.quarantine photoflow.app
   ```

   or take the GUI route: double-click (it gets blocked), then
   **System Settings → Privacy & Security** → scroll to "photoflow was
   blocked" → **Open Anyway**. Either way it's one-time.

### From source

Needs [uv](https://docs.astral.sh/uv/) and libjpeg-turbo:

```sh
# Linux (Debian/Ubuntu)
sudo apt install libturbojpeg libjpeg-turbo-progs
# macOS
brew install jpeg-turbo
# Windows: install the official libjpeg-turbo -vc-x64.exe from
# https://github.com/libjpeg-turbo/libjpeg-turbo/releases

git clone https://github.com/franchg/photoflow.git
cd photoflow
uv sync
uv run python app.py [folder | image]
```

A folder argument opens it in the grid; an image argument opens its
folder with that image fullscreen — which is exactly what double-clicking
an image in your file manager does once photoflow is the default viewer.

## User guide

### Browse

Open a folder with **Ctrl+O**, the toolbar button, or the collapsible
**Folders** tree. Thumbnails appear immediately and sharpen as full
decodes arrive; everything is cached, so the second visit to a folder is
instant. The toolbar has **S/M/L** thumbnail sizes and sorting by name or
capture date; the status bar counts items, selection and hidden photos.
**Ctrl+R** re-scans the folder and rebuilds its thumbnails.

### Cull

Work the keyboard: **0–5** rates the selected photo (press again to
clear), **P** picks, **X** rejects, **U** unflags. Ratings show as stars
on the thumbnail; picks get a green edge, rejects a red one. The toolbar
filters narrow the grid to a minimum rating, a flag state, or edited
photos only — so "show me the 4-star picks" is two clicks. **Del** moves
photos to the system trash (with a confirmation for multi-selections;
originals are never deleted any other way).

### View

**Enter** or double-click opens the viewer; **← →** or **Space** moves
through the folder, with the filmstrip below for jumping around. **Z** or
double-click toggles fit ↔ 100%, the wheel zooms, dragging pans. **F**
goes fullscreen. Hold the **right mouse button** anywhere to peek at the
original with your edits bypassed — release to come back.

### Edit

![Editing a photo: tune sliders, edit stack, filmstrip](docs/screenshot-edit.png)

All edits are **non-destructive**: they form a per-photo edit stack
stored in the catalog, your files are never modified, and every edit can
be undone (**Ctrl+Z / Ctrl+Shift+Z**), toggled, or removed in the panel's
stack list at any time — including after a restart.

- **Tune sliders** — Brightness, Contrast, Saturation, Ambiance,
  Highlights, Shadows, Temperature, Tint. Their responses are calibrated
  against Snapseed's, so they stay usable across the whole range:
  brightening doesn't blow out whites, ambiance opens up shadows like a
  local tone map, highlights/shadows recover without washing things out.
- **White balance** — press **W** (or the WB button) and click something
  that should be neutral gray; temperature and tint are solved so that
  spot renders exactly neutral. Esc cancels.
- **Crop** — press **C**, drag the box or its handles, **Enter** applies.
  Aspect presets in the panel: Free, Original, 1:1, 3:2, 4:3, 16:9
  (orientation-aware — 3:2 becomes 2:3 on a portrait shot).
- **Rotate** — the 90° button for orientation, plus a free-angle slider
  (−180…180°, 0.1° steps) that works like a straighten tool: the photo is
  auto-cropped to the largest clean frame.
- **Vignette** — press **V** (or the Vig button), click where the
  vignette should center, then set strength (darken or brighten the
  edges) and size with the sliders.

![Cropping with the rule-of-thirds overlay](docs/screenshot-crop.png)

Edits move between photos: **Ctrl+Shift+C** copies the current stack,
**Ctrl+Shift+V** pastes it onto the whole selection (**Ctrl+Alt+Shift+V**
appends instead of replacing), and **Ctrl+L** applies just the last edit
to the selection — rate a batch, fix one photo, paste onto the rest.

### Export

**Ctrl+E** exports the selection: choose a destination, a file-name
pattern (tokens for the original name, capture date/time and a counter,
with a live preview), JPEG quality, and an optional long-edge resize.
PNGs stay PNG (lossless); JPEGs re-encode only when needed — an unedited
photo exports as an exact copy, and pure 90° rotations are applied
losslessly when possible. EXIF metadata is preserved.

### Settings

**Ctrl+,** opens Settings: **theme** (System / Light / Dark) and UI
scale; hidden-file visibility; **file associations** (Linux: one click to
become the default JPEG/PNG viewer); and catalog maintenance — relocate
the catalog, clear the thumbnail cache, remove entries for
missing/renamed files, or wipe the catalog entirely (edits and ratings
live there; sources are never touched).

## Keyboard shortcuts

Press **F1** or **?** in the app for this list.

| Key | Action |
| --- | --- |
| **Browse** | |
| Ctrl+O | Open folder |
| Ctrl+R / F5 | Re-scan folder, rebuild its thumbnails |
| Enter / double-click | Open the viewer; Esc goes back |
| Space / → / ↓ | Next image |
| ← / ↑ | Previous image |
| F / F11 | Fullscreen in/out (from the grid: current photo) |
| Del | Move selection to the system trash |
| **Cull** | |
| 0–5 | Rating (press again to clear) |
| P / X / U | Pick / reject / unflag |
| **Viewer** | |
| Z / double-click | Fit ↔ 100 % (wheel zooms, drag pans) |
| C | Interactive crop — Enter applies, Esc cancels |
| W | White-balance eyedropper — click a neutral gray |
| V | Vignette — click to place the center |
| Right-click (hold) | Compare with the original |
| **Edit stacks** | |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo edits (per image) |
| Ctrl+Shift+C | Copy edit stack |
| Ctrl+Shift+V | Paste edits onto selection (replace) |
| Ctrl+Alt+Shift+V | Paste edits onto selection (append) |
| Ctrl+L | Apply last edit op to selection |
| **App** | |
| Ctrl+E | Export… |
| Ctrl+, | Settings… |
| F1 / ? | Keyboard-shortcut help |

## For the curious

The technical story lives in **[PLAN.md](PLAN.md)**.

Development in one line: `uv sync`, then
`uv run python tests/verify_headless.py` / `verify_shader.py` /
`smoke_gui.py` are the three suites that gate every release.

## License

[MIT](LICENSE). The bundled UI font (IBM Plex Sans Condensed) is licensed
under the [SIL Open Font License 1.1](fonts/OFL.txt).
