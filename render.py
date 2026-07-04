"""Shared stack→math mapping: GLSL uniform values and their numpy mirror.

Preview (shaders/adjust.frag) and export both consume *this* module's
numbers, so the two paths cannot drift. The numpy code below mirrors the
shader line by line — keep them in sync when touching either.

Pipeline (PLAN.md "Render pipeline", extended with the Snapseed tune set):
  1. brightness rgb *= exp2(exposure * K_EXPOSURE_EV)      (linear space)
  2. temp/WB    per-channel gains                          (linear space)
  3. contrast   pivot 0.5 in linear-ish space (gamma 2.2)
  4. highlights/shadows: luma²-masked pull toward white/black (display space)
  5. saturation mix with Rec.709 luma                      (display space)
  6. hue        3×3 rotation matrix around the gray axis   (display space)

Ambiance is not its own step: it folds into contrast (lowers it when
positive), saturation (raises it) and a shadow lift — +1 reads "colorful
and happy", -1 "contrasty and less colorful".
"""
from __future__ import annotations

import math

import numpy as np

from editstack import EditStack, FoldedTune, Geometry

K_EXPOSURE_EV = 3.0    # exposure param ±1 → ±3 stops
K_TEMPERATURE = 0.30   # temperature param ±1 → ±30% R/B gain swing
K_HUE_DEGREES = 180.0  # hue param ±1 → ±180°
K_HIGHLIGHTS = 0.7     # strength of the highlights pull at ±1
K_SHADOWS = 0.7        # strength of the shadows pull at ±1
K_AMBIANCE_SAT = 0.5       # ambiance ±1 → ±50% saturation factor
K_AMBIANCE_CONTRAST = 0.3  # only ambiance -1 adds contrast (×1.3); positive
                           # opens via the chroma-preserving shadow lift, since
                           # lowering pivot-contrast just washes blacks gray
K_AMBIANCE_SHADOWS = 0.4   # ambiance +1 lifts shadows by 0.4 (pre-K scale)
GAMMA = 2.2

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def wb_gains(temperature: float) -> np.ndarray:
    """Warm (t>0) boosts red / cuts blue; cool is the inverse. Gains multiply,
    so summed temperature params map through exp for symmetric composition."""
    t = temperature * K_TEMPERATURE
    return np.array([math.exp(t), 1.0, math.exp(-t)], dtype=np.float32)


def hue_matrix(hue: float) -> np.ndarray:
    """Rotation around the achromatic (1,1,1) axis by hue*K_HUE_DEGREES."""
    angle = math.radians(hue * K_HUE_DEGREES)
    c, s = math.cos(angle), math.sin(angle)
    k = (1.0 - c) / 3.0
    r3 = math.sqrt(1.0 / 3.0) * s
    return np.array([
        [c + k, k - r3, k + r3],
        [k + r3, c + k, k - r3],
        [k - r3, k + r3, c + k],
    ], dtype=np.float32)


def _clamp1(v: float) -> float:
    return max(-1.0, min(1.0, v))


class TuneUniforms:
    """The exact values the fragment shader receives (and numpy consumes)."""

    def __init__(self, folded: FoldedTune):
        amb = _clamp1(folded.ambiance)
        self.exp_gain = float(2.0 ** (folded.exposure * K_EXPOSURE_EV))
        self.wb = wb_gains(folded.temperature)
        self.contrast = max(0.0, float(
            folded.contrast_factor
            * (1.0 - K_AMBIANCE_CONTRAST * min(amb, 0.0))))
        self.saturation = max(0.0, float(
            folded.saturation_factor * (1.0 + K_AMBIANCE_SAT * amb)))
        self.highlights = _clamp1(folded.highlights) * K_HIGHLIGHTS
        self.shadows = _clamp1(folded.shadows
                               + K_AMBIANCE_SHADOWS * amb) * K_SHADOWS
        self.hue_mat = hue_matrix(folded.hue)
        self.identity = folded.is_identity()


def apply_tune(rgb_float: np.ndarray, u: TuneUniforms) -> np.ndarray:
    """Numpy mirror of shaders/adjust.frag. In/out: float32 RGB in [0, 1]."""
    lin = np.power(np.maximum(rgb_float, 0.0), GAMMA)
    lin *= u.exp_gain
    lin *= u.wb
    lin = (lin - 0.5) * u.contrast + 0.5
    np.maximum(lin, 0.0, out=lin)
    srgb = np.power(lin, 1.0 / GAMMA)
    if u.highlights != 0.0 or u.shadows != 0.0:
        # Luma-masked pull toward white/black, applied as one gain to all
        # channels so color ratios survive a shadow lift (colorful, not washed)
        L0 = srgb @ LUMA
        wh = L0 * L0
        ws = (1.0 - L0) ** 2
        dL = ((max(u.highlights, 0.0) * (1.0 - L0)
               + min(u.highlights, 0.0) * L0) * wh
              + (max(u.shadows, 0.0) * (1.0 - L0)
                 + min(u.shadows, 0.0) * L0) * ws)
        gain = np.clip(L0 + dL, 0.0, 1.0) / np.maximum(L0, 1e-4)
        srgb = np.clip(srgb * gain[..., None], 0.0, 1.0)
    luma = srgb @ LUMA
    out = luma[..., None] + (srgb - luma[..., None]) * u.saturation
    out = out @ u.hue_mat.T
    return np.clip(out, 0.0, 1.0, out=out)


def apply_tune_uint8(rgb: np.ndarray, u: TuneUniforms,
                     chunk_rows: int = 512) -> np.ndarray:
    """Apply the tune to a uint8 image in row chunks so a 24 MP export never
    holds the full float32 buffer (~290 MB) at once."""
    if u.identity:
        return rgb
    out = np.empty_like(rgb)
    for y in range(0, rgb.shape[0], chunk_rows):
        block = rgb[y:y + chunk_rows].astype(np.float32) / 255.0
        block = apply_tune(block, u)
        out[y:y + chunk_rows] = (block * 255.0 + 0.5).astype(np.uint8)
    return out


def apply_geometry(rgb: np.ndarray, geo: Geometry) -> np.ndarray:
    """Rotate then crop, matching the viewer's UV transform."""
    d = geo.cw_degrees % 360
    if d:
        rgb = np.rot90(rgb, k=(360 - d) // 90)  # np.rot90 is CCW; we count CW
    x, y, w, h = geo.rect
    if (x, y, w, h) != (0.0, 0.0, 1.0, 1.0):
        ih, iw = rgb.shape[:2]
        x0, y0 = round(x * iw), round(y * ih)
        x1, y1 = round((x + w) * iw), round((y + h) * ih)
        rgb = rgb[max(0, y0):max(1, y1), max(0, x0):max(1, x1)]
    return np.ascontiguousarray(rgb)


def render_stack(rgb: np.ndarray, stack: EditStack) -> np.ndarray:
    """Full CPU render: geometry then folded tune. uint8 in, uint8 out.
    This is the export path and the edited-thumbnail path."""
    rgb = apply_geometry(rgb, stack.geometry())
    return apply_tune_uint8(rgb, TuneUniforms(stack.folded_tune()))
