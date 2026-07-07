# photoflow — Technical Spec

Fast native app for JPEG/PNG browsing, review/culling, and light
non-destructive editing. Bridge-like browsing speed + Snapseed-style
composable edit stacks with bulk copy/paste. Runs on Linux, Windows and
macOS from source or as packaged binaries (see Packaging). JPEG is the
optimized hot path; PNG rides the same pipeline through a Qt-codec decode
fallback (no scaled decode: full decode, then downscale; alpha is
flattened over white through the edit pipeline, and only the no-edit
byte-copy export keeps it).

This document describes the system as built. User-facing docs (keys,
settings, running) live in README.md.

## Stack

- **Language**: Python 3.11+, project managed by uv (`uv sync`, `uv run`).
- **UI**: PySide6 (Qt 6). QWidgets: `QListView` IconMode grid,
  `QOpenGLWidget` viewer (GL 3.3 core), `QFileSystemModel` folder tree.
- **Decode/encode**: libjpeg-turbo via `PyTurboJPEG` (pinned `<2`: 2.x needs
  libjpeg-turbo 3 at runtime). PNG via Qt's codec, RAW via LibRaw
  (`rawpy`) — all dispatched on magic bytes in `decode.py` so the rest of
  the app is format-blind.
- **Metadata**: `pyexiv2` for EXIF copy on export; a hand-rolled APP1/TIFF
  parser in `decode.py` for the hot read path (orientation, capture date,
  embedded thumb).
- **Catalog**: SQLite, WAL mode, single writer thread (queue of Futures),
  thread-local read connections.
- **Preview**: one GLSL fragment shader pass on a screen-resolution texture.

## Performance principles (non-negotiable)

1. **Never decode more pixels than displayed.** libjpeg-turbo DCT-domain
   scaled decode (1/8 … 1/1). PNG has no scaled decode — full decode, then
   smooth downscale (accepted cost of the fallback path).
2. **EXIF thumbnails first.** The grid paints immediately with embedded
   thumbs, replaced asynchronously by 1/8-scale decodes.
3. **Persistent thumbnail cache.** SQLite blobs keyed by
   `(path, mtime, size)`. Cache hit = no decode at all.
4. **Virtualized grid.** Only visible cells instantiated.
5. **UI thread never blocks.** All I/O and decoding on worker pools
   (libjpeg-turbo releases the GIL); results arrive via queued Qt signals.
   Visible thumbs get priority; bulk re-renders trickle in the background.
6. **Edits preview on GPU.** Slider drags re-render the shader on the
   already-uploaded texture. Full-res pixels are touched only at export.
7. **No blank frames on navigation.** Switching photos in the viewer keeps
   painting the outgoing frame (its texture + geometry/tune) until the next
   photo's first texture is uploaded — the placeholder thumb when cached,
   otherwise the fit decode. The background never flashes through.

## RAW: the preview serves what it can, the demosaic serves the rest

Cameras store a developed JPEG inside every RAW; LibRaw (rawpy) extracts
it in ~15 ms. `decode.py` dispatches each request (`_decode_raw`): the
preview serves any decode it can cover — which makes RAW browsing cost
the same as JPEG browsing, riding the TurboJPEG path — and a LibRaw
demosaic (`use_camera_wb`, `user_flip=0` so pixels stay unoriented like
every other decode path, `half_size` when the target allows) serves what
it can't: full resolution when the preview is smaller than the sensor,
previews under 1024 px, or no preview at all. Most makers embed
full-size previews so everything is preview-fast; Sony ARW embeds only
~1616 px, so its thumbs ride the preview while fit views, 100 % zoom and
exports demosaic the real 24 MP. `read_header` reports LibRaw's
processed dimensions — the demosaic-capable size — so fit/zoom/export
are sized by the sensor regardless of which path renders.

EXIF comes from the same hand-rolled TIFF walker — TIFF-based containers
(DNG/CR2/NEF/ARW/PEF/SRW/ORF/RW2) are TIFF from byte zero, so
`_parse_exif` accepts a bare TIFF prefix; CR3/RAF fall back to LibRaw's
flip for orientation (`parse_exif_data`). Export policy: an untouched
RAW byte-copies with its original extension (the keeper flow); anything
that needs pixels (edits, resize) develops to a JPEG; the jpegtran and
PNG paths never see RAW.

Deliberately NOT a RAW developer: no linear 16-bit pipeline, highlight
recovery, or exposure on sensor data. The tune math is calibrated in
display-space sRGB (below) and a RAW development pipeline is a different
app. Known trade-off: the camera preview and the LibRaw demosaic differ
slightly in tone (camera picture styles), so a Sony thumb can look
subtly different from its fit view.

## Edit model: non-destructive, composable stack

Edits never touch source files. Each image has an ordered edit stack
(JSON v1 in SQLite, the source of truth):

```json
{
  "version": 1,
  "stack": [
    {"op": "rotate", "params": {"degrees": 90}},
    {"op": "crop",   "params": {"rect": [0.05, 0.0, 0.9, 0.95]}},
    {"op": "tune",   "params": {"exposure": 0.3, "temperature": -0.1,
                                 "tint": 0.05}}
  ]
}
```

- Tune keys: `exposure` (shown as Brightness), `contrast`, `saturation`,
  `ambiance`, `highlights`, `shadows`, `temperature`, `tint`. (An early
  `hue` key was dropped; stacks carrying it fail validation and the
  loaders fall back to an empty stack.) Scalars normalized to [-1, 1];
  geometry in
  normalized rects; rotation is any angle (slider −180…180 in 0.1° steps
  plus the 90° button). The fold decomposes total rotation into the
  nearest 90° multiple (exact `rot90`, lossless-able) + a fine residual
  in [-45°, 45°] that resamples bilinearly with an automatic crop to the
  largest same-aspect inscribed rect (`render.inscribed_scale`).
- Stacks compose in order. Consecutive `tune` ops fold algebraically
  (additive params sum; contrast/saturation fold as `(1+p)` factors) so the
  preview shader is a single pass regardless of stack depth. Geometry folds
  by transforming crop rects through rotations.
- Undo/redo is per-image stack history; the stack is the unit of
  copy/paste — replace or append onto any selection, plus "apply last edit
  to selection". A paste on N images is N row writes + N queued thumb
  re-renders; the grid converges progressively.
- The white-balance eyedropper solves `temperature`/`tint` so the clicked
  pixel renders exactly neutral (`render.solve_white_balance`): warmth is
  the *first* tune stage and every later stage maps neutral to neutral.
  Temperature is found by bisecting the warmth curves until the pixel's R
  and B meet; tint then scales G closed-form. Set on the edited op so the
  *folded* stack lands on the solve.

## Render pipeline (preview ≡ export, by construction)

`render.py` owns the stack→math mapping. The GLSL shader
(`shaders/adjust.frag`) and a line-by-line numpy mirror both consume
`TuneUniforms`; `tests/verify_shader.py` proves GPU ≡ CPU on a real GL
context. Order:

1. tone curve: warmth (temperature) ∘ tint ∘ brightness ∘ contrast — all
   per-channel display-space curves, composed on the CPU into one
   `CURVE_N×3` table per slider change (`render.build_tone_curve`). The
   GPU samples it as a 1024×1 RGB32F texture (explicit texelFetch+mix);
   the CPU export mirrors the same gather+lerp. No per-pixel
   transcendentals remain in the shader.
2. ambiance: LOCAL tone map — a luma delta driven by (pixel luma, blurred
   neighborhood luma) plus a vibrance term (chroma-weighted saturation).
   The neighborhood comes from `render.local_mean_luma`: a ≤96px
   box-reduced map blurred with a gaussian σ = 3.6% of the image side,
   uploaded as a small texture (bilinear-sampled with explicit
   texelFetch so CPU export matches exactly; the viewer computes it once
   per photo from its CPU-side copy).
3. highlights/shadows: measured Snapseed responses — global, linear in
   the slider, driven by (channel value, pixel luma):
   `delta = |s|·[D(L) + b·(D(p)−D(L)) + (p−L)·h(p,L)]` with per-side
   gray curves D (hl+ a gain from black that clips white; hl− a hinge
   above 0.55; sh+ a hinge below 0.30 that lifts true black; sh− a gain
   from white that clips black) — `render.HLSH_CHROMA` / `_hlsh_apply`.
4. saturation: mix with Rec.709 luma
5. vignette: blend toward the brightness curve, radial-falloff weighted

Brightness, contrast and warmth responses are least-squares fits
calibrated against Snapseed's Tune Image slider→curve tables (measured
from `tune_image_ssm_*.png` / `tunehue_0to200_mitte100.png`; only our
fitted polynomial coefficients ship — `render._TONE_MODEL`). End-to-end
deviation ≤2/255 across the slider range (5/255 at contrast +100). The
properties that make the sliders "usable": brightening pins black and
white so +100 never clips; darkening pulls the white point down; contrast
rolls off softly instead of hard-clamping; warming shifts mid-tones but
barely touches white. Each response is `x + Σ ampₖ(|s|)·shapeₖ(x)` with
one or two polynomial/sine (shape, amp) pairs per slider sign.

Vignette is its own op (`{cx, cy, radius, strength}`, center placeable by
clicking, radius as a fraction of the half frame diagonal). It renders as
a blend toward the fitted brightness curve at `strength`, weighted by a
radial falloff fit to Snapseed's vignette mask (smoothstep knots 0.10/1.24,
amp 1.174) — so edges get tone-mapped darkening/lift that never crushes to
black. GPU: a second curve texture + the frame-position varying; CPU: the
same math in apply_tune.

Ambiance, highlights and shadows were calibrated by measurement, not from
assets: Snapseed ships no curve tables for them, so `tools/ambiance_calib.py`
generates a chart (gray ramps, color patches, surround-probes,
checkerboards) that was run through the real app at several slider values
per tool and measured back (`measure` subcommand). Findings baked into the
fit: the response is linear in the slider; the tone term depends on the
*neighborhood* (a mid-gray square lifts on a dark surround and drops on a
bright one — σ≈3.6% of the image side); flat black/white and pixel
extremes are pinned; the chroma term is a vibrance (muted colors move ~10×
more than saturated ones, and +s boosts ~3× harder than −s mutes). Fit
quality: ~1/255 rms on the achromatic family, mean ≤6.5/255 full-chart at
slider extremes. The calibration exports themselves stay out of the repo.

**Color**: sRGB is the working space. ICC-tagged sources (JPEG APP2, PNG
iCCP — Adobe RGB, Display P3) convert to sRGB at decode via Qt's
QColorSpace/QColorTransform, so thumbs, viewer, edit math and export all
see sRGB; untagged/sRGB input pays nothing. Exports are always sRGB:
tagged explicitly (sRGB ICC + `Exif.Photo.ColorSpace`), source profiles
never copied.

Export renders the identical math full-res in numpy (chunked), encodes with
libjpeg-turbo, and copies EXIF via pyexiv2 (never the source ICC). PNG in →
PNG out, lossless; quality slider is JPEG-only. Special cases: rotation-only
stacks export via lossless `jpegtran -perfect` when available; a no-edit
export is a byte copy — both shortcuts only for sources that are already
sRGB/untagged, since preserved bytes can't be converted. File naming is a
token pattern (`[FILE_NAME] [Y] [M] [D] [H] [m] [s] [SEQ]`) with EXIF-date
fallback to mtime, sanitization, and collision suffixing.

## Architecture

```
workers (thread pools)               catalog (SQLite WAL)          UI (Qt main thread)
  - directory scan                     - files table                 - folder tree + grid (S/M/L)
  - EXIF thumb extraction              - thumbs table                - filmstrip + GL viewer
  - scaled decode (turbo/Qt)           - edits table (stacks)        - edit stack panel + sliders
  - edited-thumb re-render             - single writer thread        - keyboard culling + filters
  - viewer decodes (3 quality levels)  - thread-local readers        - edit clipboard, undo/redo
  - export pipeline                                                  - fullscreen state machine
        └──── queued Qt signals; generation counter kills stale work ────┘
```

### Catalog schema

```sql
files(id, path UNIQUE, mtime, size, width, height, orientation,
      capture_dt, rating INT DEFAULT 0, flag INT DEFAULT 0)   -- flag: 0/1/-1
thumbs(file_id PK, small BLOB, large BLOB, edited BOOL)        -- ~256px / ~1024px JPEG
edits(file_id PK, stack JSON, updated_at)
```

Maintenance ops (Settings): clear thumbnail cache (DELETE + VACUUM, edits
kept), missing-file sweep (existence scan with progress → confirmed row
removal), catalog relocation, full wipe behind a hard warning.

## Platform integration

- **Theming**: System (native style/palette/font captured at startup,
  restorable at runtime) or token-driven Light/Dark (`styles.py` is the
  single palette + QSS source), which use the bundled IBM Plex Sans
  Condensed (`fonts/`, OFL — the repo's one binary asset) at a compact
  10 pt. All icons are feather-style SVGs generated in code and tinted to
  the theme at runtime.
- **Linux desktop**: the frozen binary self-registers a `.desktop` entry +
  hicolor icons on every start (Wayland derives the dock icon from the
  desktop entry matched to the window's app id; `Exec=` follows the binary
  if it moves). Settings can make photoflow the xdg default viewer for
  JPEG/PNG. Deletion goes to the system trash via `QFile.moveToTrash`.
  Catalog and thumbnail cache live in `~/.local/share/photoflow/`.
- **CLI / file manager**: `photoflow [folder|image]` — a folder opens in
  the grid, an image opens its parent folder fullscreen on that image
  (the `Exec=%F` double-click path).
- **Windows**: same code; catalog/cache land in `%LOCALAPPDATA%`, trash is
  the Recycle Bin, settings the registry. The frozen exe self-registers
  in HKCU on every start (`register_windows_app`: Applications key +
  ProgID + Capabilities + RegisteredApplications pointer), so "Open with"
  lists photoflow by name, Settings → Default apps shows it, and the
  registered command heals if the exe moves — the exact analog of the
  Linux `.desktop` refresh. Actually *becoming* the default stays a user
  click; programmatic self-defaulting is OS-forbidden since Windows 10.

## Packaging & CI

`photoflow.spec` (PyInstaller): windowed one-dir bundle by default
(Windows zip); `PHOTOFLOW_ONEFILE=1` builds the single self-extracting
binary (Linux). Bundled: turbojpeg, jpegtran, exiv2 runtime, the shader,
an icon rendered from the in-code SVG.

**Hard-won rule**: on Linux the system graphics/session stack (EGL/GL,
gbm/drm, wayland, glib) *and* the runtime family beneath it (libstdc++,
libgcc_s, libffi, libpcre2-8, libz, libzstd) must never be bundled — the
host's Mesa/LLVM load into our process and fail against older bundled
copies ("EGL not available", no window on Wayland). The spec excludes them
(`.so`-anchored so Qt plugins like `libdrm-egl-server.so` survive) and CI
fails the build if any reappear.

The `build` workflow (manual dispatch or `v*` tag): `windows`, `linux` and
`macos` jobs each run the headless suite as a platform gate, build,
sanity-check bundle contents (including the exclusion guard on Linux),
launch-test, and upload; a single `release` job attaches all archives to
the GitHub release. The linux job additionally wraps the one-file binary
in a Debian package (`scripts/make_deb.py`: `/usr/bin/photoflow`, a
system-wide desktop entry + hicolor icons, Depends on exactly the host
graphics/session libs the bundle excludes, permissions normalized,
`dpkg-deb --root-owner-group`), then apt-installs it on the runner and
launch-tests `/usr/bin/photoflow` before attaching. A package-managed
install (frozen exe under `/usr`) skips the per-user desktop
self-registration — the system entry from the package wins. Linux builds on the oldest LTS runner for glibc
compatibility and ships `.tar.gz` (artifacts drop the executable bit);
macOS ships an unsigned arm64 `.app` (Gatekeeper: right-click → Open on
first launch — notarization would need an Apple Developer ID).

## Project layout

```
photoflow/
  app.py              # MainWindow, shortcuts, fullscreen, settings, CLI open
  theme.py            # System/Light/Dark switching (styles.py owns tokens)
  desktopintegration.py  # .desktop + icon registration, xdg default viewer
  catalog.py          # SQLite writer thread, schema, maintenance ops
  workers.py          # pools: scan, thumbs (visible-first), bulk, viewer
  decode.py           # TurboJPEG + PNG + RAW-preview dispatch, EXIF parse,
                      # jpegtran, frozen-bundle lib resolution
  editstack.py        # ops, validation, folding, clipboard, history
  render.py           # stack→math (GLSL uniforms + numpy mirror), WB solve
  models.py           # grid list model + filter/sort proxy
  export.py           # export engine + naming tokens (UI-free)
  styles.py           # theme tokens, palette, QSS, in-code SVG icons
  views/              # grid, viewer (GL + crop + WB pick), stackpanel,
                      # foldertree, settingsdialog, exportdialog
  shaders/adjust.frag
  scripts/make_ico.py # Windows exe icon from the in-code SVG
  tests/              # verify_headless, verify_shader, smoke_gui
  photoflow.spec      # PyInstaller (one-dir / one-file)
  .github/workflows/build.yml
```

## Verification

Three suites, all in CI-gating or local use (`uv run python tests/…`):

- `verify_headless.py` — decode/EXIF/catalog/render/export math, ~90
  checks; runs on both CI platforms as the build gate.
- `verify_shader.py` — offscreen-FBO proof that the GLSL shader and the
  numpy export path produce identical pixels for a stack exercising every
  tune parameter.
- `smoke_gui.py` — drives the real MainWindow offscreen through ~30
  scenarios (scan→thumbs→culling→edits→crop/WB drag→fullscreen→trash→
  settings→catalog maintenance→CLI open). Fully hermetic: QSettings are
  process-redirected to a temp dir, destructive catalog tests run on temp
  DBs only.

Standing targets: slider→preview under 16 ms independent of stack depth;
UI thread never blocks on I/O or pixels; paste-on-500 stays responsive.

## Deliberately not built (revisit on demand)

- Formats beyond JPEG/PNG/RAW (HEIC, WebP, TIFF, video). RAW itself is
  read through its embedded preview (see "RAW: the preview is the
  image"); linear RAW *development* is the part that stays unbuilt.
- Monitor-profile color management (wide-gamut display mapping) — input
  profiles *are* handled (converted to sRGB at decode); the output side is
  left to the compositor, which Wayland is increasingly taking over.
- Local/selective edits, healing, curves.
- XMP sidecars (SQLite is the source of truth; versioned stack JSON keeps
  the door open).
- Single-instance reuse (double-clicking a second image spawns a second
  process today).
