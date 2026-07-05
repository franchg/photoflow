"""Ambiance calibration: chart generation + Snapseed round-trip measurement.

Snapseed's Ambiance has no curve table in its assets (unlike brightness/
contrast/warmth, which render.py's fits came from), so it is calibrated by
measurement: run this chart through Snapseed's Tune Image → Ambiance at
several slider values, export, and feed the exports back.

  1. uv run python tools/ambiance_calib.py chart          → ambiance_chart.png
  2. Phone: Snapseed → open the chart → Tune Image → set ONLY Ambiance →
     export at original size, JPG quality 100. One export per value,
     named  ambiance_<value>.jpg  (e.g. ambiance_-100.jpg, ambiance_50.jpg).
     For 0, nudge Ambiance and set it back so Snapseed allows the export.
  3. uv run python tools/ambiance_calib.py measure <dir-with-exports>
     → prints per-region deltas and writes measurements.json for fitting.

The chart carries: a smooth gray ramp + 32 gray patches (tone response),
48 HSV + 8 pastel + 8 skin patches (saturation/hue response), gray probes
of three sizes on black/gray/white surrounds plus two checkerboards
(neighborhood dependence — Ambiance is expected to be a local operator),
and a smooth gradient (halo detection).
"""
from __future__ import annotations

import colorsys
import json
import os
import re
import sys

import numpy as np

W = 1440
RAMP_H = 96          # y 0..96: smooth horizontal gray ramp
GRAY_ROWS = 2        # y 96..276: 2×16 gray patches, 90×90
COLOR_ROWS = 3       # y 276..546: 8 hues × (sat, val) grid, 16 per row
MISC_ROW_H = 90      # y 546..636: 8 pastels + 8 skin tones
FIELD_H = 240        # y 636..876: six 240×240 neighborhood fields
H = RAMP_H + GRAY_ROWS * 90 + COLOR_ROWS * 90 + MISC_ROW_H + FIELD_H

SKIN = [(255, 224, 196), (240, 184, 150), (198, 134, 94), (141, 85, 54),
        (97, 60, 38), (224, 172, 138), (255, 205, 170), (168, 110, 76)]


def build() -> tuple[np.ndarray, list[dict]]:
    """Deterministic chart + region registry (the single source of truth
    for both `chart` and `measure`)."""
    img = np.zeros((H, W, 3), dtype=np.uint8)
    regions: list[dict] = []

    def add(name, kind, x0, y0, x1, y1, rgb):
        img[y0:y1, x0:x1] = rgb
        regions.append(dict(name=name, kind=kind, x0=x0, y0=y0, x1=x1, y1=y1,
                            rgb=[int(v) for v in rgb]))

    # smooth ramp (sampled, not a patch — registered for curve extraction)
    ramp = np.round(np.linspace(0, 255, W)).astype(np.uint8)
    img[0:RAMP_H] = ramp[None, :, None]
    regions.append(dict(name="ramp", kind="ramp", x0=0, y0=0, x1=W, y1=RAMP_H,
                        rgb=None))

    y = RAMP_H
    vals = np.round(np.linspace(0, 255, 32)).astype(int)
    for i, v in enumerate(vals):
        r, c = divmod(i, 16)
        add(f"gray{v}", "gray", c * 90, y + r * 90, (c + 1) * 90,
            y + (r + 1) * 90, (v, v, v))

    y += GRAY_ROWS * 90
    combos = [(0.5, 0.65), (1.0, 0.65), (1.0, 0.35)]  # (sat, val) per row
    combos += [(0.5, 0.9)]  # spread over the 3 rows: 8 hues × 6 combos = 48
    hues = [i / 8 for i in range(8)]
    cells = [(h, s, v) for (s, v) in [(0.5, 0.65), (1.0, 0.65), (1.0, 0.35),
                                      (0.5, 0.9), (1.0, 0.9), (0.25, 0.5)]
             for h in hues]
    for i, (hh, ss, vv) in enumerate(cells):
        r, c = divmod(i, 16)
        rgb = tuple(int(round(255 * ch)) for ch in colorsys.hsv_to_rgb(hh, ss, vv))
        add(f"hsv_h{hh:.3f}_s{ss}_v{vv}", "color", c * 90, y + r * 90,
            (c + 1) * 90, y + (r + 1) * 90, rgb)

    y += COLOR_ROWS * 90
    for i, hh in enumerate(hues):  # pastels
        rgb = tuple(int(round(255 * ch)) for ch in colorsys.hsv_to_rgb(hh, 0.25, 0.8))
        add(f"pastel_h{hh:.3f}", "color", i * 90, y, (i + 1) * 90,
            y + MISC_ROW_H, rgb)
    for i, rgb in enumerate(SKIN):
        add(f"skin{i}", "color", (8 + i) * 90, y, (9 + i) * 90,
            y + MISC_ROW_H, rgb)

    y += MISC_ROW_H
    # neighborhood fields: probes of gray 64/128/192 on black/gray/white
    for fi, bg in enumerate((0, 128, 255)):
        x = fi * FIELD_H
        img[y:y + FIELD_H, x:x + FIELD_H] = bg
        # measurement box in the bottom-right corner, clear of every probe
        regions.append(dict(name=f"field_bg{bg}", kind="surround",
                            x0=x + 180, y0=y + 180, x1=x + 232, y1=y + 232,
                            rgb=[bg] * 3))
        # center row: 128-gray at three sizes → kernel-scale estimate
        for size, px in ((16, 22), (40, 60), (96, 122)):
            cy = y + FIELD_H // 2 - size // 2
            add(f"probe128_s{size}_bg{bg}", "probe", x + px, cy,
                x + px + size, cy + size, (128, 128, 128))
        for v, py in ((64, 30), (192, FIELD_H - 70)):
            add(f"probe{v}_s40_bg{bg}", "probe", x + 100, y + py,
                x + 140, y + py + 40, (v, v, v))
    # checkerboards (8 px and 32 px cells) — frequency response; register
    # one black and one white cell interior near the field center as probes
    for fi, cell in enumerate((8, 32)):
        x = (3 + fi) * FIELD_H
        yy, xx = np.mgrid[0:FIELD_H, 0:FIELD_H]
        board = (((yy // cell) + (xx // cell)) % 2) * 255
        img[y:y + FIELD_H, x:x + FIELD_H] = board[..., None].astype(np.uint8)
        regions.append(dict(name=f"checker{cell}", kind="checker", x0=x, y0=y,
                            x1=x + FIELD_H, y1=y + FIELD_H, rgb=None))
        half = FIELD_H // (2 * cell) * cell  # cell whose top-left is center-ish
        for name, v, off in ((f"checker{cell}_black", 0, 0),
                             (f"checker{cell}_white", 255, 1)):
            cx = x + half + off * cell       # parity picks black vs white cell
            regions.append(dict(name=name, kind="probe", x0=cx,
                                y0=y + half, x1=cx + cell, y1=y + half + cell,
                                rgb=[v] * 3))
    # smooth horizontal gradient — halo detection
    x = 5 * FIELD_H
    grad = np.round(np.linspace(0, 255, FIELD_H)).astype(np.uint8)
    img[y:y + FIELD_H, x:x + FIELD_H] = grad[None, :, None]
    regions.append(dict(name="gradient", kind="ramp", x0=x, y0=y,
                        x1=x + FIELD_H, y1=y + FIELD_H, rgb=None))
    return img, regions


def _save_png(img: np.ndarray, path: str) -> None:
    from PySide6.QtGui import QImage
    h, w = img.shape[:2]
    q = QImage(np.ascontiguousarray(img).data, w, h, w * 3,
               QImage.Format.Format_RGB888)
    if not q.save(path, "PNG"):
        raise RuntimeError(f"could not write {path}")


def _load_rgb(path: str) -> np.ndarray:
    from PySide6.QtGui import QImage
    q = QImage(path).convertToFormat(QImage.Format.Format_RGB888)
    if q.isNull():
        raise RuntimeError(f"could not read {path}")
    buf = np.frombuffer(bytes(q.constBits()), np.uint8)
    return buf.reshape(q.height(), q.bytesPerLine())[:, :q.width() * 3] \
              .reshape(q.height(), q.width(), 3).copy()


def _sample(img: np.ndarray, r: dict) -> list[float]:
    """Median of the inner 50% of a region — immune to JPEG edge ringing."""
    w, h = r["x1"] - r["x0"], r["y1"] - r["y0"]
    ix0, iy0 = r["x0"] + w // 4, r["y0"] + h // 4
    ix1, iy1 = r["x1"] - w // 4, r["y1"] - h // 4
    patch = img[iy0:iy1, ix0:ix1].reshape(-1, 3)
    return [float(v) for v in np.median(patch, axis=0)]


def measure(exports_dir: str) -> None:
    _, regions = build()
    out = {}
    files = sorted(os.listdir(exports_dir))
    for fn in files:
        m = re.search(r"(-?\d+)", fn)
        if not m or not fn.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        value = int(m.group(1))
        img = _load_rgb(os.path.join(exports_dir, fn))
        if img.shape[:2] != (H, W):
            print(f"SKIP {fn}: size {img.shape[1]}×{img.shape[0]}, "
                  f"expected {W}×{H} (exported resized?)")
            continue
        samples = {r["name"]: _sample(img, r) for r in regions
                   if r["kind"] != "ramp"}
        # ramps: mean over rows, per column (full curves incl. halos)
        for r in regions:
            if r["kind"] == "ramp":
                strip = img[r["y0"] + 8:r["y1"] - 8, r["x0"]:r["x1"]]
                samples[r["name"]] = np.mean(strip, axis=(0, 2)).round(2).tolist()
        out[value] = dict(file=fn, samples=samples)
        print(f"measured {fn} as ambiance {value:+d}")
    if not out:
        print("no usable exports found")
        sys.exit(1)
    dst = os.path.join(exports_dir, "measurements.json")
    with open(dst, "w") as f:
        json.dump(dict(width=W, height=H, regions=regions, exports=out), f)
    print(f"\nwrote {dst}")
    # quick look: gray response + surround dependence at a few values
    for value in sorted(out):
        s = out[value]["samples"]
        gs = [(int(k[4:]), round(s[k][0])) for k in s if k.startswith("gray")]
        gs.sort()
        line = " ".join(f"{a}→{b}" for a, b in gs[::6])
        probes = [round(s[f"probe128_s40_bg{bg}"][0]) for bg in (0, 128, 255)]
        print(f"  {value:+4d}: gray {line} | 128 on blk/gry/wht → {probes}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "chart"
    if cmd == "chart":
        path = sys.argv[2] if len(sys.argv) > 2 else "ambiance_chart.png"
        img, regions = build()
        _save_png(img, path)
        print(f"wrote {path} ({W}×{H}, {len(regions)} regions)")
    elif cmd == "measure":
        measure(sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
