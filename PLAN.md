# photoflow — Technical Spec

Fast native Linux app for JPEG browsing, review/culling, and light non-destructive editing. Bridge-like browsing speed + Snapseed-style composable edit stack with bulk copy/paste. **JPEG only** — no RAW, no other formats; this assumption simplifies decode, cache, and export paths and must be exploited throughout.

## Stack

- **Language**: Python 3.11+
- **UI**: PySide6 (Qt 6). QWidgets-based: `QListView` in IconMode for the grid, `QOpenGLWidget` for the single-image viewer.
- **Decode/encode**: libjpeg-turbo via `PyTurboJPEG`. All hot paths run in C or on the GPU; Python only orchestrates.
- **Metadata**: `pyexiv2` (EXIF thumbnail, orientation, capture date).
- **Catalog**: SQLite, single writer thread, WAL mode.
- **Preview rendering**: OpenGL fragment shader (GLSL) applied to a screen-resolution texture.

## Performance principles (non-negotiable)

1. **Never decode more pixels than displayed.** libjpeg-turbo DCT-domain scaled decode (1/2, 1/4, 1/8). A 24 MP JPEG at 1/8 decodes in a few ms. JPEG-only makes this universally applicable.
2. **EXIF thumbnails first.** Paint the grid immediately with embedded ~160×120 EXIF thumbs (few KB of I/O each), replace asynchronously with proper 1/8-scale decodes.
3. **Persistent thumbnail cache.** SQLite blobs keyed by `(path, mtime, size)`. Cache hit = no decode at all.
4. **Virtualized grid.** Only visible cells instantiated (QListView gives this for free).
5. **UI thread never blocks.** All I/O and decoding in a `ThreadPoolExecutor` (libjpeg-turbo releases the GIL → real parallelism). Results delivered to the UI via queued Qt signals.
6. **Edits preview on GPU.** Slider drags re-render a fragment shader on the already-uploaded texture at 60 fps. Full-res pixels are touched only at export.

## Edit model: non-destructive, composable stack

This is the core design decision. Edits are **never applied to source files**. Each image has an ordered **edit stack** (a la Snapseed): a list of operations, each with its own parameters, composable and individually removable/reorderable.

```json
{
  "version": 1,
  "stack": [
    {"op": "rotate",   "params": {"degrees": 90}},
    {"op": "crop",     "params": {"rect": [0.05, 0.0, 0.9, 0.95]}},
    {"op": "tune",     "params": {"exposure": 0.3, "contrast": 0.1,
                                   "saturation": 0.0, "hue": 0.0,
                                   "temperature": -0.1}},
    {"op": "tune",     "params": {"exposure": -0.05}}
  ]
}
```

Rules:

- Scalar params normalized to [-1, 1]; geometry in normalized coordinates (crop rect) or degrees (rotate).
- Stack entries compose in order. Consecutive `tune` ops are algebraically foldable at render time (exposure sums, gains multiply) so the preview shader always runs a single pass regardless of stack depth.
- Undo/redo = pop/push on the stack; history is free.
- The stack is the unit of copy/paste: it can be serialized, copied, and applied wholesale or merged onto other images.
- Source JPEGs are read-only, always. Export renders the stack to new files.

### Bulk copy/paste of edits (first-class feature)

- **Copy edits** (Ctrl+Shift+C) from the current image → the full stack (or a user-selected subset of ops, e.g. only `tune`, excluding `crop`) goes to an internal edit clipboard.
- **Paste edits** (Ctrl+Shift+V) onto any selection of N images. Two modes:
  - *Replace*: target stacks are overwritten.
  - *Append*: pasted ops are appended to existing stacks (composability makes this well-defined).
- Paste on 500 images = 500 SQLite row writes + thumbnail re-render jobs queued to the worker pool. No pixel work on the UI thread; grid thumbnails update progressively to reflect edits.
- Also expose "Apply last edit to selection" for the common cull-then-fix-exposure workflow.

## Architecture

```
workers (ThreadPoolExecutor)          catalog (SQLite)              UI (Qt main thread)
  - directory scan                      - files table                 - grid view (thumbnails)
  - EXIF thumb extraction               - thumbs table                - filmstrip + viewer
  - scaled decode (turbo)               - edits table (stacks)        - edit stack panel + sliders
  - edited-thumb re-render              - single writer thread        - keyboard-driven culling
  - export pipeline                                                   - edit clipboard
        └──── Qt signals ────────────────────────────────────────────────┘
```

### Catalog schema (initial)

```sql
files(id, path UNIQUE, mtime, size, width, height, orientation,
      capture_dt, rating INT DEFAULT 0, flag INT DEFAULT 0)   -- flag: 0 none, 1 pick, -1 reject
thumbs(file_id PK, small BLOB, large BLOB, edited BOOL)        -- ~256px and ~1024px JPEG blobs
edits(file_id PK, stack JSON, updated_at)                      -- the edit stack, source of truth
```

Grid thumbnails show the *edited* preview (re-rendered by workers on stack change); a badge marks images with a non-empty stack.

### Render pipeline (preview and export share the same math)

Stack → fold consecutive `tune` ops → geometry (rotate, crop) → single fragment shader pass:

1. Exposure: `rgb *= exp2(exposure * k)`
2. Temperature/white balance: per-channel gains
3. Contrast: pivot around 0.5 in linear-ish space (approx `pow(rgb, 2.2)` in, re-encode out)
4. Saturation: mix with luma (`dot(rgb, vec3(0.2126, 0.7152, 0.0722))`)
5. Hue: 3×3 hue-rotation matrix

~30 lines of GLSL. **Export applies identical math** on the full-res decode — vectorized numpy or an offscreen FBO render — then encodes with libjpeg-turbo at chosen quality, preserving EXIF. Special case: a stack containing only 90° rotations exports via lossless jpegtran-style transform, no re-encode. A shared `render.py` module owns the stack-folding and the parameter→math mapping so preview and export cannot drift.

## Features (v1 scope)

- Open folder → instant grid (EXIF thumbs → async replacement). Sort by name/date.
- Single-image viewer: fit/100% toggle, pan/zoom, prefetch ±2 neighbors at viewer resolution.
- Keyboard culling: 0–5 rating, P pick, X reject, arrows navigate. Filter bar by rating/flag/edited.
- Edit stack panel: per-op enable/disable/remove, live GPU preview via sliders.
- Bulk copy/paste of edit stacks (replace/append), apply-to-selection.
- Batch export: destination folder, quality slider, optional resize (long edge), EXIF preserved, worker pool with progress.

## Explicitly out of scope (v1)

Local/selective edits, healing, curves, RAW or any non-JPEG format, color management (assume sRGB), XMP sidecars (SQLite is the source of truth; the versioned stack JSON keeps the door open).

## Project layout

```
photoflow/
  app.py              # entry point, QApplication
  catalog.py          # SQLite access, writer thread, schema migration
  workers.py          # scan, thumb extraction, scaled decode, edited-thumb re-render, export
  decode.py           # PyTurboJPEG wrappers (scaled decode, EXIF thumb, encode, lossless rotate)
  editstack.py        # stack model: ops, validation, folding, serialization, clipboard
  render.py           # shared stack→math mapping (GLSL uniforms + numpy mirror)
  models.py           # QAbstractListModel for the grid, roles for thumb/rating/flag/edited
  views/
    grid.py           # QListView IconMode + delegate (rating/flag/edited overlays)
    viewer.py         # QOpenGLWidget, texture mgmt, shader, pan/zoom
    stackpanel.py     # edit stack UI + sliders
  shaders/adjust.frag
  export.py           # full-res pipeline
```

## Milestones

1. **Grid MVP**: folder scan → EXIF thumbs → async 1/8 decodes → SQLite cache → virtualized grid. *Target: 1000-photo folder browsable in <1 s cold, instant warm.*
2. **Viewer + culling**: full viewer with prefetch, ratings/flags, filtering.
3. **Edit stack**: `editstack.py` + `render.py` + shader preview + persistence + undo/redo.
4. **Bulk + export**: copy/paste stacks across selections, batch export, lossless rotations.

## Dependencies

```
PySide6, PyTurboJPEG, pyexiv2, numpy
system: libturbojpeg (apt: libturbojpeg0-dev), OpenGL 3.3+
```

## Verification targets

- Grid scroll at 60 fps on a 5000-photo folder (warm cache).
- Slider drag → preview update <16 ms, independent of stack depth (folding works).
- Paste edits on 500 images: UI stays responsive, thumbnails converge in the background.
- Batch export of 100 × 24 MP JPEGs saturates all cores; output pixel-identical to preview math.
- UI thread never blocks >50 ms.