"""Shared stack→math mapping: GLSL uniform values and their numpy mirror.

Preview (shaders/adjust.frag) and export both consume *this* module's
numbers, so the two paths cannot drift. The numpy code below mirrors the
shader line by line — keep them in sync when touching either.

Pipeline (PLAN.md "Render pipeline", Snapseed-calibrated tune set):
  1. tone curve  per-channel display-space curve — the composition of
                 warmth (temperature) ∘ tint ∘ brightness ∘ contrast,
                 sampled into one CURVE_N×3 table by build_tone_curve()
                 and applied as a LUT (GPU: texelFetch+mix, CPU: gather+lerp)
  2. ambiance    LOCAL tone map: a luma shift driven by the pixel value and
                 the blurred neighborhood luminance (local_mean_luma —
                 a small map the GPU samples as a texture); its chroma
                 component folds into the saturation stage
  3. highlights/shadows: luma²-masked pull toward white/black
  4. saturation  mix with Rec.709 luma
  5. hue         3×3 rotation matrix around the gray axis

The brightness/contrast/warmth responses are least-squares fits calibrated
against Snapseed's Tune Image behavior (measured from its slider→tone-curve
tables; max deviation ~5/255 at slider extremes, ~1/255 typical). Key
properties the old EV-gain/pivot-slope math lacked: brightening pins black
AND white so +100 never clips; darkening pulls the white point down;
contrast rolls off softly instead of clamping; warming cuts blue at
mid-tones but barely at white, so highlights keep their color. Each
response is delta(x, s) = Σ amp_k(|s|) · shape_k(x) with one or two
(shape, amp) polynomial pairs per slider sign.
"""
from __future__ import annotations

import math

import numpy as np

from editstack import EditStack, FoldedTune, Geometry

K_TINT = 0.30          # tint param ±1 → ±30% G gain swing (+1 = magenta)
K_HUE_DEGREES = 180.0  # hue param ±1 → ±180°
K_HIGHLIGHTS = 0.7     # strength of the highlights pull at ±1
K_SHADOWS = 0.7        # strength of the shadows pull at ±1
# Ambiance chroma term is a vibrance: gain 1 + s·max(a + b·chroma, 0) —
# muted colors move much more than already-saturated ones (measured slopes)
K_AMB_VIB_POS = (0.7108, -0.7593)   # (a, b) for s > 0
K_AMB_VIB_NEG = (0.2297, -0.2288)   # (a, b) for s < 0
AMB_SIGMA_REL = 52.0 / 1440.0  # local-mean gaussian σ ÷ the larger image side
AMB_LMAP_SIDE = 96     # resolution (larger side) of the local-mean map
GAMMA = 2.2

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

CURVE_N = 1024  # tone-curve table resolution (GPU texture width)

# Fitted tone-response model. Component = (shape_kind, shape_coefs, amp_coefs):
#   delta(x, s) += amp(|s|) · shape(x)   for the matching sign of s
# shape kinds ("pinned" is zero at both ends, "poly"/"sine" at x=0 only):
#   poly    Σ c_k x^k             cpoly   c_0 + Σ c_k x^k
#   pinned  Σ c_k x^k (1-x)       sine    Σ c_k sin(kπx)
# amp: Σ a_k |s|^k (k ≥ 1, so s=0 is always the identity curve).
_TONE_MODEL = {
    ("bright", +1): (
        ("pinned", (2.0064646, -0.94271698, -0.026622878, -0.063554506),
         (0.17255963, 1.4038862, -2.1952333, 3.6187401, -2.8962317, 0.8969699)),
    ),
    ("bright", -1): (
        ("poly", (-0.80893781, 1.5417038, -6.4668569, 13.993527, -14.087899,
                  5.4856326),
         (0.31161366, 0.20645171, 1.792642, -2.8650501, 2.2390727, -0.6851044)),
        ("poly", (0.083278019, -0.80603181, 3.3849539, -6.8207955, 6.5157677,
                  -2.3812348),
         (-2.8789347, 13.457112, -50.928393, 94.771198, -77.641977, 24.229958)),
    ),
    ("contrast", +1): (
        ("sine", (-0.0041437292, -0.16034238, -0.0013802614, 0.03939314,
                  -0.00083624982, -0.017281031, -0.0005907537, 0.0083992577,
                  -0.00045946759, -0.0050501946),
         (0.29235163, -0.40474669, 3.1746929, -3.7256003, 2.1285468,
          -0.46388101)),
        ("sine", (0.0029740078, 0.0020356203, 0.0009893803, 0.0064203849,
                  0.00061171321, -0.0033129082),
         (-3.2688241, 17.59542, -60.006531, 99.028943, -74.466811, 22.14313)),
    ),
    ("contrast", -1): (
        ("cpoly", (0.17361178, -0.37165352, -0.064827673, 0.44129798,
                   -0.59689014, 0.23865062),
         (-0.22113247, 2.9452999, -4.7981982, 5.9754498, -3.8051271,
          0.90327448)),
    ),
    ("warmR", +1): (
        ("pinned", (0.12120137, 0.52517108, -1.6131284, 2.8138608, -1.5749214),
         (1.3648373, -3.6228716, 9.1860258, -7.2723136, -1.2016089, 2.5531777)),
    ),
    ("warmR", -1): (
        ("poly", (-0.82632269, 0.28775499, -0.057285476, 1.1936978,
                  -0.61034575, -0.031887942),
         (1.5478026, -4.2876242, 15.265685, -26.973748, 22.735085, -7.2809067)),
        ("poly", (0.031953426, -0.11309164, 0.44785655, -0.92731127,
                  0.73214485, -0.1978894),
         (-2.2817935, 40.76048, -220.06814, 506.83608, -525.0257, 200.87455)),
    ),
    ("warmG", +1): (
        ("pinned", (-0.10087745, -0.30220436, 0.81649044, -1.2200187,
                    0.55737745),
         (1.7557238, -9.0585908, 38.121803, -71.255334, 60.446705, -19.025806)),
    ),
    ("warmG", -1): (
        ("poly", (-0.024577879, -0.0048735551, 0.034960465, -0.024497637),
         (-1.6040734, 25.327134, -133.21091, 308.06497, -323.59232, 126.14853)),
    ),
    ("warmB", +1): (
        ("poly", (-0.53696314, -0.52784957, 3.5322493, -7.2163743, 6.9087993,
                  -2.2215685),
         (1.1543433, -1.2002518, 3.2221177, -3.0319508, 0.32009283,
          0.54314157)),
    ),
    ("warmB", -1): (
        ("pinned", (0.5572289, 0.35755922, 1.732418, -5.6773188, 5.9391656,
                    -2.3188863),
         (1.263043, -2.5161793, 11.738038, -25.904494, 26.363337, -9.9353003)),
    ),
}


# Ambiance response surface, least-squares fit of the measured Snapseed
# behavior (tools/ambiance_calib.py round-trip; rms 0.9/255, max 4/255 over
# 1795 observations). delta(p, L) at slider s=1 is
#     (1-p) · Σ_i p^i · Horner_j(_AMB_COEFS[i], L)      i=1..4, j=0..5
# where p is the pixel's post-curve luma and L the blurred neighborhood
# luma — ambiance is a LOCAL operator: the same pixel value lifts when its
# surroundings are dark and drops when they are bright (and vice versa at
# negative s, which is linear in s throughout). Pinned at p=0 and p=1, so
# flat black/white stays put and nothing ever clips harshly.
_AMB_COEFS = (
    (1.002303, 2.02708, -36.98477, 140.9541, -222.5687, 128.2808),
    (-3.183862, 66.59705, -476.1331, 1211.945, -1232.374, 369.4713),
    (-19.50465, 134.2402, 374.7766, -2583.244, 3844.742, -1674.701),
    (52.76057, -563.3507, 1453.711, -898.8191, -807.9057, 739.3197),
)


def local_mean_luma(rgb: np.ndarray) -> np.ndarray:
    """Small blurred-luma map of an image (float32, ≤AMB_LMAP_SIDE on the
    larger side): box-reduce then gaussian σ = AMB_SIGMA_REL·side, reflect
    padding — the neighborhood term of the ambiance model. The GPU gets
    this exact array as a texture; sample it with _sample_lmap."""
    h, w = rgb.shape[:2]
    side = max(h, w)
    sh = min(h, max(1, round(h * AMB_LMAP_SIDE / side)))
    sw = min(w, max(1, round(w * AMB_LMAP_SIDE / side)))
    lum = np.asarray(rgb[..., :3], dtype=np.float32) @ LUMA
    if rgb.dtype == np.uint8:
        lum /= 255.0
    ye = np.arange(sh) * h // sh
    xe = np.arange(sw) * w // sw
    red = np.add.reduceat(np.add.reduceat(lum, ye, axis=0), xe, axis=1)
    red /= np.outer(np.diff(np.append(ye, h)), np.diff(np.append(xe, w)))
    sigma = AMB_SIGMA_REL * max(sh, sw)
    r = max(1, int(math.ceil(3.0 * sigma)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    for axis, size in ((0, sh), (1, sw)):
        rr = min(r, size - 1)
        if rr < 1:
            continue
        kk = k[r - rr:r + rr + 1] / k[r - rr:r + rr + 1].sum()
        red = np.apply_along_axis(
            lambda v: np.convolve(np.pad(v, rr, mode="reflect"), kk, "valid"),
            axis, red)
    return red.astype(np.float32)


def _sample_lmap(lmap: np.ndarray, y0: int, rows: int,
                 full_h: int, full_w: int) -> np.ndarray:
    """Bilinear local-mean for image rows [y0, y0+rows) — the numpy mirror
    of the shader's clamped texelFetch+mix on u_lmap."""
    sh, sw = lmap.shape
    ys = (np.arange(y0, y0 + rows, dtype=np.float32) + 0.5) / full_h * sh - 0.5
    xs = (np.arange(full_w, dtype=np.float32) + 0.5) / full_w * sw - 0.5
    yi = np.floor(ys).astype(np.int32)
    xi = np.floor(xs).astype(np.int32)
    yf = (ys - yi)[:, None]
    xf = xs - xi
    ya, yb = np.clip(yi, 0, sh - 1), np.clip(yi + 1, 0, sh - 1)
    xa, xb = np.clip(xi, 0, sw - 1), np.clip(xi + 1, 0, sw - 1)
    top = lmap[ya][:, xa] * (1.0 - xf) + lmap[ya][:, xb] * xf
    bot = lmap[yb][:, xa] * (1.0 - xf) + lmap[yb][:, xb] * xf
    return top * (1.0 - yf) + bot * yf


def _amb_delta(p: np.ndarray, L: np.ndarray, s: float) -> np.ndarray:
    """Ambiance luma delta — keep in sync with the AMB block in adjust.frag."""
    acc = np.zeros_like(p)
    pw = p
    for row in _AMB_COEFS:
        h = np.full_like(L, row[5])
        for j in range(4, -1, -1):
            h = h * L + row[j]
        acc += pw * h
        pw = pw * p
    return np.float32(s) * (1.0 - p) * acc


def _eval_shape(kind: str, coefs, x: np.ndarray) -> np.ndarray:
    if kind == "sine":
        out = np.zeros_like(x)
        for k, c in enumerate(coefs, start=1):
            out += c * np.sin(np.pi * k * x)
        return out
    const = 0.0
    if kind == "cpoly":
        const, coefs = coefs[0], coefs[1:]
    acc = np.zeros_like(x)
    for c in reversed(coefs):     # Horner: x·(c1 + x·(c2 + …))
        acc = (acc + c) * x
    if kind == "pinned":
        acc *= 1.0 - x
    return acc + const


def _eval_amp(coefs, s: float) -> float:
    acc = 0.0
    for c in reversed(coefs):
        acc = (acc + c) * s
    return acc


def _tone_delta(name: str, s: float, x: np.ndarray) -> np.ndarray:
    """delta(x) of one fitted response at slider s ∈ [-1, 1]."""
    if s == 0.0:
        return np.zeros_like(x)
    out = np.zeros_like(x)
    for kind, shape_c, amp_c in _TONE_MODEL[(name, 1 if s > 0 else -1)]:
        out += _eval_amp(amp_c, abs(s)) * _eval_shape(kind, shape_c, x)
    return out


def build_tone_curve(brightness: float = 0.0, contrast: float = 0.0,
                     temperature: float = 0.0, tint: float = 0.0) -> np.ndarray:
    """Compose warmth ∘ tint ∘ brightness ∘ contrast into one per-channel
    display-space curve, sampled at CURVE_N uniform input points.
    Returns float32 (CURVE_N, 3). All params clamp to [-1, 1].

    Warmth must stay the first stage: the WB eyedropper solve relies on
    every later stage applying one identical curve to all three channels."""
    xs = np.linspace(0.0, 1.0, CURVE_N)
    b, c, t = _clamp1(brightness), _clamp1(contrast), _clamp1(temperature)
    tint_scale = math.exp(-_clamp1(tint) * K_TINT) ** (1.0 / GAMMA)
    chans = []
    for name in ("warmR", "warmG", "warmB"):
        v = xs + _tone_delta(name, t, xs)
        if name == "warmG" and tint_scale != 1.0:
            v = v * tint_scale   # linear-space gain ≡ display-space scale
        np.clip(v, 0.0, 1.0, out=v)
        v = v + _tone_delta("bright", b, v)
        np.clip(v, 0.0, 1.0, out=v)
        v = v + _tone_delta("contrast", c, v)
        np.clip(v, 0.0, 1.0, out=v)
        chans.append(v)
    return np.stack(chans, axis=1).astype(np.float32)


_IDENTITY_CURVE: np.ndarray | None = None


def identity_curve() -> np.ndarray:
    global _IDENTITY_CURVE
    if _IDENTITY_CURVE is None:
        _IDENTITY_CURVE = build_tone_curve()
    return _IDENTITY_CURVE


def sample_curve(rgb: np.ndarray, curve: np.ndarray) -> np.ndarray:
    """Per-channel linear interpolation into the tone-curve table — the numpy
    mirror of the shader's tone_curve() (texelFetch + mix, same index math)."""
    f = np.clip(rgb, 0.0, 1.0) * np.float32(CURVE_N - 1)
    i0 = np.minimum(f.astype(np.int32), CURVE_N - 2)
    fr = f - i0
    out = np.empty_like(f)
    for c in range(3):
        col = curve[:, c]
        idx = i0[..., c]
        a = col[idx]
        out[..., c] = a + (col[idx + 1] - a) * fr[..., c]
    return out


def solve_white_balance(rgb) -> tuple[float, float]:
    """(temperature, tint) that neutralize a source-space sRGB pixel — the
    eyedropper. Temperature is bisected until the warmth curves bring the
    pixel's R and B together; tint then scales G to match. Warmth is the
    first tune stage and every stage after it maps neutral to neutral, so
    the picked pixel renders exactly gray. Values are clamped to the param
    range; a cast beyond it is only reduced."""
    eps = 1.0 / 255.0
    r, g, b = (min(max(float(ch), eps), 1.0) for ch in rgb)
    xr, xg, xb = (np.array([v]) for v in (r, g, b))

    def rb_gap(t: float) -> float:
        return float((xr + _tone_delta("warmR", t, xr))[0]
                     - (xb + _tone_delta("warmB", t, xb))[0])

    if rb_gap(-1.0) >= 0.0:      # gap grows with t; cap the cooling
        t = -1.0
    elif rb_gap(1.0) <= 0.0:     # cap the warming
        t = 1.0
    else:
        lo, hi = -1.0, 1.0
        t = 0.0
        for _ in range(40):
            t = 0.5 * (lo + hi)
            gap = rb_gap(t)
            if gap == 0.0:
                break
            if gap > 0.0:
                hi = t
            else:
                lo = t
    wr = float((xr + _tone_delta("warmR", t, xr))[0])
    wb = float((xb + _tone_delta("warmB", t, xb))[0])
    wg = float((xg + _tone_delta("warmG", t, xg))[0])
    target = 0.5 * (wr + wb)     # == both when t wasn't clamped
    k = target / max(wg, eps)    # display-space G scale → tint param
    n = -GAMMA * math.log(max(k, 1e-6)) / K_TINT
    return t, _clamp1(n)


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


# Vignette falloff: weight(d) = min(VIG_AMP·smoothstep(K0, K1, d), 1) with
# d = pixel distance from the center ÷ (radius · half frame diagonal) — a
# least-squares fit of Snapseed's radial vignette mask (rms 0.014). The
# weight blends toward the fitted brightness response at `strength`, so
# edges get real tone-mapped darkening/lift instead of a flat multiply.
VIG_KNOTS = (0.10, 1.24)
VIG_AMP = 1.174


def vignette_weight(d: np.ndarray) -> np.ndarray:
    t = np.clip((d - VIG_KNOTS[0]) / (VIG_KNOTS[1] - VIG_KNOTS[0]), 0.0, 1.0)
    return np.minimum(VIG_AMP * t * t * (3.0 - 2.0 * t), 1.0)


class TuneUniforms:
    """The exact values the fragment shader receives (and numpy consumes)."""

    def __init__(self, folded: FoldedTune, vignette: dict | None = None):
        amb = _clamp1(folded.ambiance)
        contrast = float(folded.contrast_factor) - 1.0
        self.curve_is_identity = (folded.exposure == 0 and contrast == 0.0
                                  and folded.temperature == 0
                                  and folded.tint == 0)
        self.curve = (identity_curve() if self.curve_is_identity
                      else build_tone_curve(folded.exposure, contrast,
                                            folded.temperature, folded.tint))
        self.ambiance = amb
        self.saturation = max(0.0, float(folded.saturation_factor))
        self.highlights = _clamp1(folded.highlights) * K_HIGHLIGHTS
        self.shadows = _clamp1(folded.shadows) * K_SHADOWS
        self.hue_mat = hue_matrix(folded.hue)
        if vignette is not None and vignette.get("strength", 0.0):
            self.vig_strength = _clamp1(float(vignette["strength"]))
            self.vig_center = (float(vignette["cx"]), float(vignette["cy"]))
            self.vig_radius = max(0.1, min(2.0, float(vignette["radius"])))
            self.vig_curve = build_tone_curve(brightness=self.vig_strength)
        else:
            self.vig_strength = 0.0
            self.vig_center = (0.5, 0.5)
            self.vig_radius = 1.0
            self.vig_curve = identity_curve()
        self.identity = folded.is_identity() and self.vig_strength == 0.0


def apply_tune(rgb_float: np.ndarray, u: TuneUniforms, lmap=None,
               y0: int = 0, full_size=None) -> np.ndarray:
    """Numpy mirror of shaders/adjust.frag. In/out: float32 RGB in [0, 1].
    lmap (from local_mean_luma) drives the ambiance stage; y0/full_size
    locate rgb_float's rows inside the full image when chunked."""
    srgb = sample_curve(rgb_float, u.curve)
    if u.ambiance != 0.0 and lmap is not None:
        fh, fw = full_size if full_size is not None else srgb.shape[:2]
        L = _sample_lmap(lmap, y0, srgb.shape[0], fh, fw)
        delta = _amb_delta(srgb @ LUMA, L, u.ambiance)
        srgb = np.clip(srgb + delta[..., None], 0.0, 1.0)
        # vibrance: chroma-weighted saturation, strong on muted colors
        a, b = K_AMB_VIB_POS if u.ambiance > 0 else K_AMB_VIB_NEG
        c = srgb.max(axis=-1) - srgb.min(axis=-1)
        gain = (1.0 + u.ambiance * np.maximum(a + b * c, 0.0))[..., None]
        pl = (srgb @ LUMA)[..., None]
        srgb = np.clip(pl + (srgb - pl) * gain, 0.0, 1.0)
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
    np.clip(out, 0.0, 1.0, out=out)
    if u.vig_strength != 0.0:
        fh, fw = full_size if full_size is not None else out.shape[:2]
        ys = (np.arange(y0, y0 + out.shape[0], dtype=np.float32) + 0.5) / fh
        xs = (np.arange(out.shape[1], dtype=np.float32) + 0.5) / fw
        dx = (xs - u.vig_center[0]) * fw
        dy = (ys - u.vig_center[1]) * fh
        half_diag = 0.5 * math.sqrt(fw * fw + fh * fh)
        d = np.sqrt(dx[None, :] ** 2 + dy[:, None] ** 2) / np.float32(
            u.vig_radius * half_diag)
        m = vignette_weight(d)[..., None].astype(np.float32)
        out = out + (sample_curve(out, u.vig_curve) - out) * m
    return out


def apply_tune_uint8(rgb: np.ndarray, u: TuneUniforms,
                     chunk_rows: int = 512) -> np.ndarray:
    """Apply the tune to a uint8 image in row chunks so a 24 MP export never
    holds the full float32 buffer (~290 MB) at once."""
    if u.identity:
        return rgb
    lmap = local_mean_luma(rgb) if u.ambiance != 0.0 else None
    full_size = rgb.shape[:2]
    out = np.empty_like(rgb)
    for y in range(0, rgb.shape[0], chunk_rows):
        block = rgb[y:y + chunk_rows].astype(np.float32) / 255.0
        block = apply_tune(block, u, lmap, y, full_size)
        out[y:y + chunk_rows] = (block * 255.0 + 0.5).astype(np.uint8)
    return out


def inscribed_scale(w: float, h: float, fine_degrees: float) -> float:
    """Scale of the largest w:h-aspect axis-aligned rect inscribed in a w×h
    rect rotated by fine_degrees — the automatic crop of the fine rotation.
    Keep in sync with the viewer's UV chain (views.viewer.uv_matrix_for)."""
    phi = math.radians(abs(fine_degrees))
    c, s = math.cos(phi), math.sin(phi)
    return min(w / (w * c + h * s), h / (w * s + h * c))


def _rotate_fine(rgb: np.ndarray, fine: float) -> np.ndarray:
    """Rotate by `fine` degrees CW and crop to the inscribed rect (bilinear,
    chunked). The inverse mapping mirrors the viewer's M_fine matrix."""
    h, w = rgb.shape[:2]
    k = inscribed_scale(w, h, fine)
    ow, oh = max(1, round(w * k)), max(1, round(h * k))
    phi = math.radians(fine)
    c, s = math.cos(phi), math.sin(phi)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    out = np.empty((oh, ow, 3), dtype=np.uint8)
    # continuous frame offsets — the exact mapping of the viewer's M_fine
    # (normalize by the rounded grid, scale by the true k·size), so the GL
    # preview and this warp sample identical source positions
    px = ((np.arange(ow, dtype=np.float32) + 0.5) / ow - 0.5) * np.float32(k * w)
    for y0 in range(0, oh, 256):
        py = (((np.arange(y0, min(y0 + 256, oh), dtype=np.float32) + 0.5) / oh
               - 0.5) * np.float32(k * h))[:, None]
        # display point → source point: rotate by -fine (y-down CW frame)
        sx = c * px[None, :] + s * py + cx
        sy = -s * px[None, :] + c * py + cy
        x0i = np.clip(sx.astype(np.int32), 0, w - 2)
        y0i = np.clip(sy.astype(np.int32), 0, h - 2)
        fx = np.clip(sx - x0i, 0.0, 1.0)[..., None]
        fy = np.clip(sy - y0i, 0.0, 1.0)[..., None]
        p00 = rgb[y0i, x0i].astype(np.float32)
        p01 = rgb[y0i, x0i + 1]
        p10 = rgb[y0i + 1, x0i].astype(np.float32)  # float before subtracting
        p11 = rgb[y0i + 1, x0i + 1]                 # — uint8 would wrap
        top = p00 + (p01 - p00) * fx
        bot = p10 + (p11 - p10) * fx
        out[y0:y0 + sx.shape[0]] = (top + (bot - top) * fy + 0.5).astype(np.uint8)
    return out


def apply_geometry(rgb: np.ndarray, geo: Geometry) -> np.ndarray:
    """Rotate (90° part exactly, fine part resampled with the inscribed-rect
    auto-crop) then crop, matching the viewer's UV transform."""
    d = geo.cw_degrees % 360
    if d:
        rgb = np.rot90(rgb, k=(360 - d) // 90)  # np.rot90 is CCW; we count CW
    if geo.fine != 0.0:
        rgb = _rotate_fine(np.ascontiguousarray(rgb), geo.fine)
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
    return apply_tune_uint8(rgb, TuneUniforms(stack.folded_tune(),
                                              stack.vignette()))
