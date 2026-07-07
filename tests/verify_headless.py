"""End-to-end headless verification of the non-GUI pipeline.

Run:  uv run python tests/verify_headless.py
Generates synthetic JPEGs (with real EXIF via pyexiv2), then exercises
decode, EXIF parsing, the catalog, stack folding, render math, and every
export path. Asserts throughout; prints PASS lines.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyexiv2

import decode
import render
from catalog import Catalog
from editstack import (EditClipboard, EditStack, Geometry, Op, StackError,
                       StackHistory, validate_op)
from export import ExportItem, ExportOptions, export_one, render_name


def make_test_jpeg(path: str, w: int = 640, h: int = 480,
                   orientation: int = 1, capture: str | None = None) -> np.ndarray:
    """Deterministic gradient + blocks image, written with real EXIF."""
    yy, xx = np.mgrid[0:h, 0:w]
    rgb = np.stack([
        (xx * 255 // max(w - 1, 1)),
        (yy * 255 // max(h - 1, 1)),
        ((xx + yy) * 255 // max(w + h - 2, 1)),
    ], axis=-1).astype(np.uint8)
    rgb[: h // 8, : w // 8] = (255, 0, 0)  # corner marker for rotation checks
    with open(path, "wb") as f:
        f.write(decode.encode_jpeg(rgb, 95))
    exif = {"Exif.Image.Orientation": str(orientation)}
    if capture:
        exif["Exif.Photo.DateTimeOriginal"] = capture
    with pyexiv2.Image(path) as img:
        img.modify_exif(exif)
        thumb_arr = rgb[::8, ::8]
        img.modify_thumbnail(decode.encode_jpeg(np.ascontiguousarray(thumb_arr), 80))
    return rgb


def make_test_png(path: str, w: int = 320, h: int = 240,
                  alpha: bool = False) -> np.ndarray:
    """Deterministic PNG; alpha=True adds a fully transparent corner."""
    yy, xx = np.mgrid[0:h, 0:w]
    rgb = np.stack([
        (xx * 255 // max(w - 1, 1)),
        (yy * 255 // max(h - 1, 1)),
        np.full((h, w), 60),
    ], axis=-1).astype(np.uint8)
    rgb[: h // 8, : w // 8] = (255, 0, 0)
    if alpha:
        from PySide6.QtGui import QImage
        rgba = np.ascontiguousarray(np.dstack(
            [rgb, np.full((h, w), 255, np.uint8)]))
        rgba[-h // 4:, -w // 4:, 3] = 0
        img = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        img.save(path, "PNG")
    else:
        with open(path, "wb") as f:
            f.write(decode.encode_png(rgb))
    return rgb


def make_test_dng(path: str, w: int = 512, h: int = 384,
                  orientation: int = 1,
                  capture: str | None = None) -> None:
    """Minimal uncompressed CFA (RGGB) DNG that LibRaw demosaics — the
    hermetic stand-in for a camera RAW. No embedded preview on purpose:
    it exercises the postprocess fallback. Left half dark, right half
    bright, so orientation is observable after decode."""
    cfa = np.zeros((h, w), dtype="<u2")
    cfa[:, : w // 2] = 8000
    cfa[:, w // 2:] = 60000
    strip = cfa.tobytes()

    cm = b"".join(struct.pack("<ii", v, 10000)          # ColorMatrix1 = I
                  for v in (10000, 0, 0, 0, 10000, 0, 0, 0, 10000))
    asn = struct.pack("<II", 1, 1) * 3                  # AsShotNeutral 1,1,1
    dt = (capture.encode("ascii") + b"\x00") if capture else b""

    # fixed layout: header, IFD0, optional Exif IFD, deferred values, strip
    n0 = 17 + (1 if capture else 0)
    ifd0_off = 8
    exif_off = ifd0_off + 2 + n0 * 12 + 4
    exif_size = (2 + 1 * 12 + 4) if capture else 0
    cm_off = exif_off + exif_size
    asn_off = cm_off + len(cm)
    dt_off = asn_off + len(asn)
    strip_off = dt_off + len(dt) + (len(dt) % 2)

    def entry(tag: int, typ: int, count: int, field: bytes) -> bytes:
        return struct.pack("<HHI", tag, typ, count) + field.ljust(4, b"\x00")

    def long_(v: int) -> bytes:
        return struct.pack("<I", v)

    def short(v: int) -> bytes:
        return struct.pack("<H", v)

    ents = [
        entry(254, 4, 1, long_(0)),                     # NewSubfileType
        entry(256, 4, 1, long_(w)),                     # ImageWidth
        entry(257, 4, 1, long_(h)),                     # ImageLength
        entry(258, 3, 1, short(16)),                    # BitsPerSample
        entry(259, 3, 1, short(1)),                     # Compression: none
        entry(262, 3, 1, short(32803)),                 # Photometric: CFA
        entry(273, 4, 1, long_(strip_off)),             # StripOffsets
        entry(274, 3, 1, short(orientation)),           # Orientation
        entry(277, 3, 1, short(1)),                     # SamplesPerPixel
        entry(278, 4, 1, long_(h)),                     # RowsPerStrip
        entry(279, 4, 1, long_(len(strip))),            # StripByteCounts
        entry(33421, 3, 2, struct.pack("<HH", 2, 2)),   # CFARepeatPatternDim
        entry(33422, 1, 4, b"\x00\x01\x01\x02"),        # CFAPattern RGGB
        entry(50706, 1, 4, b"\x01\x04\x00\x00"),        # DNGVersion 1.4
        entry(50717, 4, 1, long_(65535)),               # WhiteLevel
        entry(50721, 10, 9, long_(cm_off)),             # ColorMatrix1
        entry(50728, 5, 3, long_(asn_off)),             # AsShotNeutral
    ]
    if capture:
        ents.append(entry(34665, 4, 1, long_(exif_off)))  # Exif IFD pointer
    ents.sort(key=lambda e: struct.unpack_from("<H", e)[0])
    assert len(ents) == n0

    with open(path, "wb") as f:
        f.write(b"II*\x00" + struct.pack("<I", ifd0_off))
        f.write(struct.pack("<H", n0) + b"".join(ents) + long_(0))
        if capture:
            f.write(struct.pack("<H", 1)
                    + entry(0x9003, 2, len(dt), long_(dt_off))
                    + long_(0))
        f.write(cm)
        f.write(asn)
        f.write(dt + b"\x00" * (len(dt) % 2))
        f.write(strip)


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"PASS {name}")


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="photoflow-test-")
    src = os.path.join(tmp, "img1.jpg")
    rgb = make_test_jpeg(src, orientation=6, capture="2024:07:04 12:34:56")

    # ---- decode & EXIF -----------------------------------------------------
    data = open(src, "rb").read()
    check("read_header", decode.read_header(data) == (640, 480))

    info = decode.parse_exif(data[:decode.EXIF_PREFIX_BYTES])
    check("exif orientation", info.orientation == 6, str(info))
    check("exif capture_dt", info.capture_dt == "2024-07-04 12:34:56", str(info))
    check("exif thumb", info.thumb is not None and info.thumb[:2] == b"\xff\xd8")

    small = decode.decode_scaled(data, 160)
    check("scaled decode 1/4+", max(small.shape[:2]) in range(160, 321),
          str(small.shape))
    full = decode.decode_scaled(data)
    check("full decode", full.shape == (480, 640, 3))
    check("decode fidelity",
          float(np.mean(np.abs(full.astype(int) - rgb.astype(int)))) < 4.0)

    oriented = decode.apply_orientation(full, 6)
    check("orientation 6 rotates CW", oriented.shape == (640, 480, 3))
    # source top-left marker ends up top-right after 90° CW
    check("orientation pixel check", oriented[5, -5, 0] > 200)

    check("pick_scale", decode.pick_scale(6000, 4000, 750) == (1, 8))

    # ---- editstack ----------------------------------------------------------
    stack = EditStack.from_json(json.dumps({
        "version": 1,
        "stack": [
            {"op": "rotate", "params": {"degrees": 90}},
            {"op": "crop", "params": {"rect": [0.25, 0.25, 0.5, 0.5]}},
            {"op": "tune", "params": {"exposure": 0.3, "contrast": 0.1}},
            {"op": "tune", "params": {"exposure": -0.05}},
        ]}))
    f = stack.folded_tune()
    check("fold exposure sums", abs(f.exposure - 0.25) < 1e-9)
    check("fold contrast multiplies", abs(f.contrast_factor - 1.1) < 1e-9)
    geo = stack.geometry()
    check("geometry", geo.cw_degrees == 90 and geo.rect == (0.25, 0.25, 0.5, 0.5))
    check("only_rotations false", not stack.only_rotations())
    check("roundtrip", EditStack.from_json(stack.to_json()).to_json()
          == stack.to_json())

    rot_only = EditStack([Op("rotate", {"degrees": 270})])
    check("only_rotations true", rot_only.only_rotations())
    try:
        validate_op(Op("rotate", {"degrees": 400}))
        check("validate rejects 400deg", False)
    except StackError:
        check("validate rejects 400deg", True)

    # free-angle rotation: fold decomposes into 90°-part + fine ∈ [-45, 45]
    gf = EditStack([Op("rotate", {"degrees": 120.0})]).geometry()
    check("rotation decomposes 120 = 90 + 30",
          gf.cw_degrees == 90 and abs(gf.fine - 30.0) < 1e-9)
    gf2 = EditStack([Op("rotate", {"degrees": -10.5})]).geometry()
    check("negative fine rotation", gf2.cw_degrees == 0
          and abs(gf2.fine + 10.5) < 1e-9 and not gf2.is_identity())
    st_rot = EditStack()
    st_rot.set_rotation(10.3)
    check("set_rotation round-trip", abs(st_rot.total_rotation() - 10.3) < 1e-9)
    st_rot.set_rotation(0.0)
    check("set_rotation 0 clears the op", not st_rot.ops)
    st_rot.add_rotation(90)
    st_rot.set_rotation(-90.0)
    check("set_rotation composes with 90s",
          abs(st_rot.total_rotation() + 90) < 1e-9
          and st_rot.geometry().fine == 0.0)

    # fine-rotation resample: inscribed auto-crop dims + CW direction
    k = render.inscribed_scale(640, 480, 10.0)
    stripe = np.full((480, 640, 3), 255, dtype=np.uint8)
    stripe[:, :320] = 0                      # black left half
    out_f = render.apply_geometry(stripe, Geometry(0, (0, 0, 1, 1), 20.0))
    oh_f, ow_f = out_f.shape[:2]
    check("fine rotation dims = inscribed rect",
          out_f.shape[:2] == (round(480 * render.inscribed_scale(640, 480, 20)),
                              round(640 * render.inscribed_scale(640, 480, 20)))
          and 0.80 < k < 0.85)
    check("fine rotation turns clockwise",
          out_f[5, ow_f // 2, 0] < 60 and out_f[-5, ow_f // 2, 0] > 200,
          f"{out_f[5, ow_f//2, 0]} {out_f[-5, ow_f//2, 0]}")

    # vignette: op validation, fold, and the spatial falloff itself
    try:
        validate_op(Op("vignette", {"cx": 1.5}))
        check("validate rejects bad vignette", False)
    except StackError:
        check("validate rejects bad vignette", True)
    vst = EditStack([Op("vignette", {"cx": 0.3, "cy": 0.4, "radius": 0.8,
                                     "strength": -0.7})])
    vig = vst.vignette()
    check("vignette fold + has_edits", vig is not None
          and vig["strength"] == -0.7 and vst.has_edits()
          and not vst.only_rotations())
    check("zero-strength vignette is a no-op",
          EditStack([Op("vignette", {"strength": 0.0})]).vignette() is None
          and not EditStack([Op("vignette", {"strength": 0.0})]).has_edits())
    gray_img = np.full((300, 400, 3), 150, dtype=np.uint8)
    u_vig = render.TuneUniforms(vst.folded_tune(), vig)
    out_v = render.apply_tune_uint8(gray_img, u_vig)
    center_px = out_v[int(0.4 * 300), int(0.3 * 400), 0]
    corner_px = out_v[-3, -3, 0]
    check("vignette darkens far corner, spares the center",
          abs(int(center_px) - 150) <= 2 and corner_px < 110,
          f"center {center_px} corner {corner_px}")
    up = render.TuneUniforms(vst.folded_tune(),
                             {**vig, "strength": 0.7})
    out_vp = render.apply_tune_uint8(gray_img, up)
    check("positive vignette brightens edges",
          out_vp[-3, -3, 0] > 190, str(out_vp[-3, -3, 0]))
    try:
        validate_op(Op("tune", {"exposure": 2.0}))
        check("validate rejects |p|>1", False)
    except StackError:
        check("validate rejects |p|>1", True)

    # crop-then-rotate composition: rect must transform into the rotated frame
    cr = EditStack([Op("crop", {"rect": [0.0, 0.0, 0.5, 0.5]}),
                    Op("rotate", {"degrees": 90})])
    g2 = cr.geometry()
    check("crop∘rotate compose", g2.cw_degrees == 90
          and np.allclose(g2.rect, (0.5, 0.0, 0.5, 0.5)), str(g2.rect))

    clip = EditClipboard()
    n = clip.copy(stack)
    check("clipboard copy", n == 4)
    pasted = clip.paste_append(rot_only)
    check("clipboard append", len(pasted.ops) == 5)
    pasted2 = clip.paste_replace(rot_only)
    check("clipboard replace", pasted2.to_json() == stack.to_json())

    hist = StackHistory()
    hist.seed(1, EditStack())
    hist.record(1, rot_only)
    hist.record(1, stack)
    check("undo", hist.undo(1).to_json() == rot_only.to_json())
    check("redo", hist.redo(1).to_json() == stack.to_json())
    check("undo to empty", hist.undo(1) is not None
          and hist.undo(1).to_json() == EditStack().to_json())

    # ---- render math ---------------------------------------------------------
    u_id = render.TuneUniforms(EditStack().folded_tune())
    check("identity uniforms", u_id.identity)
    img_f = full.astype(np.float32) / 255.0
    out_id = render.apply_tune(img_f.copy(), u_id)
    check("identity tune ~= input",
          float(np.max(np.abs(out_id - img_f))) < 1e-3)

    bright = render.TuneUniforms(
        EditStack([Op("tune", {"exposure": 0.5})]).folded_tune())
    out_b = render.apply_tune(img_f.copy(), bright)
    check("exposure brightens", float(out_b.mean()) > float(img_f.mean()) + 0.05)

    chunk_a = render.apply_tune_uint8(full, bright, chunk_rows=64)
    chunk_b = render.apply_tune_uint8(full, bright, chunk_rows=100000)
    check("chunking is exact", np.array_equal(chunk_a, chunk_b))

    # Snapseed tune set: ambiance / highlights / shadows
    two = EditStack([Op("tune", {"ambiance": 0.3, "highlights": 0.2}),
                     Op("tune", {"ambiance": 0.2, "shadows": -0.4})]).folded_tune()
    check("fold ambiance/hl/sh sums",
          abs(two.ambiance - 0.5) < 1e-9 and abs(two.highlights - 0.2) < 1e-9
          and abs(two.shadows + 0.4) < 1e-9)

    # Ambiance: local tone map, measured from Snapseed (tools/ambiance_calib).
    # Same mid-gray square answers to its surround: lifts on black, drops on
    # white at +1 (mirrored at -1); a flat field barely moves; chroma gain
    # folds into saturation (boost ~3× stronger than the negative mute).
    amb_pos = render.TuneUniforms(
        EditStack([Op("tune", {"ambiance": 1.0})]).folded_tune())
    amb_neg = render.TuneUniforms(
        EditStack([Op("tune", {"ambiance": -1.0})]).folded_tune())
    check("ambiance folds: own stage, curve/sat untouched",
          amb_pos.saturation == 1.0 and amb_neg.saturation == 1.0
          and amb_pos.curve_is_identity and amb_pos.ambiance == 1.0
          and amb_neg.ambiance == -1.0)
    # vibrance: muted colors gain chroma hard at +1, saturated ones barely;
    # negative ambiance mutes gently
    vib = np.full((512, 512, 3), 128, dtype=np.uint8)
    vib[248:264, 248:264] = (150, 128, 106)      # muted patch, chroma 44
    vib[100:116, 100:116] = (255, 0, 0)          # saturated patch
    vp = render.apply_tune_uint8(vib, amb_pos)
    vn = render.apply_tune_uint8(vib, amb_neg)
    def _chroma(px):
        return int(px.max()) - int(px.min())
    check("ambiance vibrance: muted boosted, saturated spared",
          _chroma(vp[256, 256]) > 55 and abs(_chroma(vp[108, 108]) - 255) <= 8
          and _chroma(vn[256, 256]) < 40,
          f"{_chroma(vp[256, 256])} {_chroma(vp[108, 108])} "
          f"{_chroma(vn[256, 256])}")
    # geometry matters: the blur σ is ~3.6% of the image side, so on 512²
    # a 16px probe is well inside its surround's influence
    sq = np.zeros((512, 512, 3), dtype=np.uint8)
    sq[248:264, 248:264] = 128                   # mid-gray probe on black
    on_black = render.apply_tune_uint8(sq, amb_pos)[256, 256, 0]
    flat_white = render.apply_tune_uint8(
        np.full((512, 512, 3), 255, np.uint8), amb_pos)[256, 256, 0]
    wsq = np.full((512, 512, 3), 255, dtype=np.uint8)
    wsq[248:264, 248:264] = 128                  # same probe on white
    on_white_sq = render.apply_tune_uint8(wsq, amb_pos)[256, 256, 0]
    neg_black = render.apply_tune_uint8(sq, amb_neg)[256, 256, 0]
    check("ambiance is local: probe follows its surround",
          on_black > 165 and on_white_sq < 75 and neg_black < 95
          and flat_white == 255,
          f"{on_black} {on_white_sq} {neg_black} {flat_white}")

    # Snapseed-calibrated tone responses: brightening pins black and white
    # (never clips), darkening pulls the white point down, warming barely
    # touches white but pulls blue at mid-gray
    bmax = render.build_tone_curve(brightness=1.0)
    check("brightness +1 pins endpoints, lifts mids",
          abs(bmax[0, 0]) < 1e-3 and abs(bmax[-1, 0] - 1.0) < 1e-2
          and bmax[render.CURVE_N // 2, 0] > 0.75)
    bmin = render.build_tone_curve(brightness=-1.0)
    check("brightness -1 pulls the white point down",
          0.55 < bmin[-1, 0] < 0.75)
    warm = render.build_tone_curve(temperature=1.0)
    mid = render.CURVE_N // 2
    check("warmth +1 protects white, shifts mids",
          warm[-1, 2] > 0.9
          and warm[mid, 2] < 0.35 and warm[mid, 0] > 0.5)

    # Snapseed-measured highlights/shadows: piecewise per-channel curves
    # baked into the tone LUT — hl- only touches above ~0.55, sh+ only
    # below ~0.30 (and lifts true black), hl+ is a gain that clips white
    gray = np.array([[[0.15] * 3, [0.85] * 3]], dtype=np.float32)
    hl = render.TuneUniforms(
        EditStack([Op("tune", {"highlights": -0.8})]).folded_tune())
    out_hl = render.apply_tune(gray.copy(), hl)
    check("highlights -: darkens brights, spares darks",
          0.70 < out_hl[0, 1, 0] < 0.75 and abs(out_hl[0, 0, 0] - 0.15) < 0.005,
          str(out_hl))
    sh = render.TuneUniforms(
        EditStack([Op("tune", {"shadows": 0.8})]).folded_tune())
    out_sh = render.apply_tune(gray.copy(), sh)
    check("shadows +: lifts darks, spares brights",
          0.22 < out_sh[0, 0, 0] < 0.27 and abs(out_sh[0, 1, 0] - 0.85) < 0.005,
          str(out_sh))
    bw = np.array([[[0.0] * 3, [0.9] * 3]], dtype=np.float32)
    out_shp = render.apply_tune(bw.copy(), render.TuneUniforms(
        EditStack([Op("tune", {"shadows": 1.0})]).folded_tune()))
    out_hlp = render.apply_tune(bw.copy(), render.TuneUniforms(
        EditStack([Op("tune", {"highlights": 1.0})]).folded_tune()))
    check("shadows +1 lifts pure black, highlights +1 clips white",
          0.12 < out_shp[0, 0, 0] < 0.17 and out_hlp[0, 1, 0] == 1.0
          and out_hlp[0, 0, 0] == 0.0,
          f"{out_shp[0, 0, 0]} {out_hlp[0, 1, 0]}")

    # white-balance eyedropper: the solve neutralizes the picked pixel, and
    # every stage after wb maps neutral to neutral, so it stays gray through
    # a full slider load-out
    check("wb solve neutral is identity",
          render.solve_white_balance((0.5, 0.5, 0.5)) == (0.0, 0.0))
    warm = np.array([[[0.62, 0.55, 0.50]]], dtype=np.float32)
    t, n = render.solve_white_balance(warm[0, 0])
    check("wb solve inside param range", -1 < t < 0 and -1 < n < 1, f"{t} {n}")
    wb_u = render.TuneUniforms(
        EditStack([Op("tune", {"temperature": t, "tint": n, "exposure": 0.2,
                               "contrast": 0.3, "saturation": 0.4,
                               "highlights": -0.2,
                               "shadows": 0.3})]).folded_tune())
    out_wb = render.apply_tune(warm.copy(), wb_u)
    check("wb solve renders picked pixel gray",
          float(out_wb.max() - out_wb.min()) < 1.5 / 255.0, str(out_wb))
    # "hue" is gone: stacks carrying it fail validation (loaders fall back
    # to an empty stack)
    try:
        validate_op(Op("tune", {"hue": 0.5}))
        check("hue key rejected", False)
    except StackError:
        check("hue key rejected", True)
    ct, cn = render.solve_white_balance((0.9, 0.5, 0.2))
    check("wb solve clamps strong casts", ct == -1.0 and -1.0 <= cn <= 1.0,
          f"{ct} {cn}")
    wbf = EditStack([Op("tune", {"temperature": 0.2, "tint": 0.1}),
                     Op("tune", {"temperature": 0.1, "tint": -0.4})]).folded_tune()
    check("fold tint sums", abs(wbf.temperature - 0.3) < 1e-9
          and abs(wbf.tint + 0.3) < 1e-9)

    g = render.apply_geometry(full, geo)  # rotate 90CW then center 50% crop
    check("apply_geometry dims", g.shape == (320, 240, 3), str(g.shape))

    # ---- catalog ----------------------------------------------------------------
    db = os.path.join(tmp, "cat.db")
    cat = Catalog(db)
    st = os.stat(src)
    recs = cat.ingest([(src, st.st_mtime, st.st_size)]).result()
    check("ingest new", len(recs) == 1 and not recs[0].has_thumb
          and recs[0].stack_json is None)
    fid = recs[0].id
    cat.put_thumbs(fid, b"S", b"L", False).result()
    cat.set_rating(fid, 4).result()
    cat.set_stack(fid, stack.to_json()).result()
    recs2 = cat.ingest([(src, st.st_mtime, st.st_size)]).result()
    check("ingest unchanged keeps cache", recs2[0].has_thumb
          and recs2[0].rating == 4 and recs2[0].stack_json == stack.to_json())
    check("thumb read", cat.get_thumb_small(fid) == (b"S", False))
    recs3 = cat.ingest([(src, st.st_mtime + 5, st.st_size)]).result()
    check("mtime change drops thumbs, keeps edits",
          not recs3[0].has_thumb and recs3[0].changed
          and recs3[0].stack_json == stack.to_json())
    cat.close()

    # ---- export paths -------------------------------------------------------------
    plain = os.path.join(tmp, "plain.jpg")
    make_test_jpeg(plain, orientation=1, capture="2023:01:02 03:04:05")
    dest = os.path.join(tmp, "out")
    os.makedirs(dest, exist_ok=True)

    # 1. no edits → byte copy
    out = export_one(ExportItem(1, plain, None), ExportOptions(dest))
    check("export copy identical",
          open(out, "rb").read() == open(plain, "rb").read())

    # 2. rotation-only → lossless jpegtran, dims swap, orientation reset
    out = export_one(ExportItem(1, plain, rot_only.to_json()),
                     ExportOptions(dest))
    od = open(out, "rb").read()
    check("lossless rotate dims", decode.read_header(od) == (480, 640))
    oinfo = decode.parse_exif(od[:decode.EXIF_PREFIX_BYTES])
    check("lossless rotate orientation reset", oinfo.orientation == 1)
    check("lossless keeps exif", oinfo.capture_dt == "2023-01-02 03:04:05")
    rot_px = decode.decode_scaled(od)
    src_px = decode.decode_scaled(open(plain, "rb").read())
    # 270° CW == np.rot90 k=1 (CCW); jpegtran differs by ~1 LSB in chroma
    expected_rot = np.rot90(src_px, k=1)
    rot_diff = float(np.mean(np.abs(rot_px.astype(int) - expected_rot.astype(int))))
    check("lossless rotate pixels", rot_diff < 2.0, f"mad={rot_diff}")
    # source top-left marker lands bottom-left after 270° CW
    check("lossless marker", rot_px[-5, 5, 0] > 200)

    # 2b. fine rotation → must take the pixel path (inscribed-rect dims)
    fine_stack = EditStack([Op("rotate", {"degrees": 10.0})])
    out = export_one(ExportItem(1, plain, fine_stack.to_json()),
                     ExportOptions(dest))
    fw, fh = decode.read_header(open(out, "rb").read())
    kf = render.inscribed_scale(640, 480, 10.0)
    check("fine rotate export = resampled inscribed rect",
          (fw, fh) == (round(640 * kf), round(480 * kf)), f"{fw}x{fh}")

    # 3. tune + crop → pixel path, matches render.render_stack
    tune_stack = EditStack([Op("crop", {"rect": [0.0, 0.0, 0.5, 0.5]}),
                            Op("tune", {"exposure": 0.4, "saturation": 0.2})])
    out = export_one(ExportItem(1, plain, tune_stack.to_json()),
                     ExportOptions(dest, quality=95))
    od = decode.decode_scaled(open(out, "rb").read())
    expected = render.render_stack(src_px, tune_stack)
    check("pixel path dims", od.shape == expected.shape,
          f"{od.shape} vs {expected.shape}")
    diff = float(np.mean(np.abs(od.astype(int) - expected.astype(int))))
    check("pixel path matches preview math", diff < 3.0, f"mad={diff}")
    with pyexiv2.Image(out) as im:
        ex = im.read_exif()
    check("pixel path preserves exif",
          ex.get("Exif.Photo.DateTimeOriginal") == "2023:01:02 03:04:05")
    check("pixel path orientation reset",
          ex.get("Exif.Image.Orientation") == "1")

    # 4. resize long edge
    out = export_one(ExportItem(1, plain, None),
                     ExportOptions(dest, resize_long=320))
    check("resize long edge", decode.read_header(open(out, "rb").read())
          == (320, 240))

    # 5. EXIF-oriented source through pixel path bakes orientation
    out = export_one(ExportItem(1, src, tune_stack.to_json()),
                     ExportOptions(dest, quality=95))
    w, h = decode.read_header(open(out, "rb").read())
    check("oriented source baked", (w, h) == (240, 320), f"{(w, h)}")

    # ---- color management: tagged sources normalize to sRGB ---------------
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColorSpace, QImage

    srgb_cs = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
    adobe_cs = QColorSpace(QColorSpace.NamedColorSpace.AdobeRgb)
    grad = np.zeros((96, 96, 3), np.uint8)
    grad[..., 0] = np.linspace(20, 235, 96).astype(np.uint8)[None, :]
    grad[..., 1] = np.linspace(235, 20, 96).astype(np.uint8)[:, None]
    grad[..., 2] = 96  # saturated mix so the gamut difference is visible
    qi = QImage(grad.data, 96, 96, 288, QImage.Format.Format_RGB888)
    qi.setColorSpace(srgb_cs)
    adobe_qi = qi.copy()
    adobe_qi.convertToColorSpace(adobe_cs)
    adobe_px = decode.qimage_to_rgb(adobe_qi)
    check("adobe numbers differ from sRGB",  # else the tests below prove nothing
          float(np.mean(np.abs(adobe_px.astype(int) - grad.astype(int)))) > 2.0)

    tagged = os.path.join(tmp, "adobe.jpg")
    with open(tagged, "wb") as f:
        f.write(decode.encode_jpeg(adobe_px, 97))
    with pyexiv2.Image(tagged) as im:
        im.modify_icc(bytes(adobe_cs.iccProfile()))
    tagged_bytes = open(tagged, "rb").read()
    check("parse_icc roundtrip",
          decode.parse_icc(tagged_bytes) == bytes(adobe_cs.iccProfile()))
    check("needs_srgb_conversion: tagged yes, untagged no",
          decode.needs_srgb_conversion(tagged_bytes)
          and not decode.needs_srgb_conversion(open(plain, "rb").read()))
    dec = decode.decode_scaled(tagged_bytes, fast=False)
    cm_mad = float(np.mean(np.abs(dec.astype(int) - grad.astype(int))))
    check("tagged JPEG decodes to sRGB", cm_mad < 3.0, f"mad={cm_mad}")

    # exports are ALWAYS sRGB: tagged source → re-encode, pixels + tag sRGB
    out = export_one(ExportItem(1, tagged, None), ExportOptions(dest, quality=97))
    check("tagged export never byte-copies",
          open(out, "rb").read() != tagged_bytes)
    oe = decode.decode_scaled(open(out, "rb").read(), fast=False)
    cm2 = float(np.mean(np.abs(oe.astype(int) - grad.astype(int))))
    check("tagged export pixels are sRGB", cm2 < 4.0, f"mad={cm2}")
    with pyexiv2.Image(out) as im:
        out_icc = im.read_icc()
        out_exif = im.read_exif()
    check("tagged export labeled sRGB",
          out_exif.get("Exif.Photo.ColorSpace") == "1" and bool(out_icc)
          and QColorSpace.fromIccProfile(QByteArray(out_icc)) == srgb_cs)

    # tagged PNG (iCCP) converts at decode too
    pbuf = QBuffer()
    pbuf.open(QIODevice.OpenModeFlag.WriteOnly)
    adobe_qi.save(pbuf, "PNG")
    png_tagged = bytes(pbuf.data())
    check("png iCCP detected", decode.needs_srgb_conversion(png_tagged))
    pdec = decode.decode_scaled(png_tagged)
    pm = float(np.mean(np.abs(pdec.astype(int) - grad.astype(int))))
    check("tagged PNG decodes to sRGB", pm < 2.0, f"mad={pm}")

    # ---- export naming patterns ------------------------------------------------
    named = ExportItem(1, "/x/DSC_0042.jpg", None,
                       capture_dt="2024-07-04 12:34:56", mtime=0.0, seq=7)
    check("pattern tokens", render_name(
        "[Y]-[M]-[D]-[m]-[s]_[FILE_NAME]_export.jpg", named)
        == "2024-07-04-34-56_DSC_0042_export")  # literal .jpg stripped
    check("pattern seq + hour", render_name("[SEQ]-[H]", named) == "007-12")
    check("pattern mtime fallback", render_name(
        "[Y]", ExportItem(1, "/x/a.jpg", None,
                          mtime=time.mktime((2023, 1, 2, 3, 4, 5, 0, 0, -1))))
        == "2023")
    check("pattern sanitized", render_name("a/b:c", named) == "a_b_c")
    check("pattern empty falls back", render_name("", named) == "DSC_0042")

    out = export_one(ExportItem(1, plain, None,
                                capture_dt="2022-05-06 07:08:09", seq=3),
                     ExportOptions(dest, name_pattern="[Y][M][D]_[SEQ]_[FILE_NAME]"))
    check("export honors pattern",
          os.path.basename(out) == "20220506_003_plain.jpg", out)
    out2 = export_one(ExportItem(1, plain, None,
                                 capture_dt="2022-05-06 07:08:09", seq=3),
                      ExportOptions(dest, name_pattern="[Y][M][D]_[SEQ]_[FILE_NAME]"))
    check("name collision suffixed",
          os.path.basename(out2) == "20220506_003_plain_1.jpg", out2)

    # ---- PNG support ---------------------------------------------------------
    png = os.path.join(tmp, "img2.png")
    png_rgb = make_test_png(png)
    pdata = open(png, "rb").read()
    check("png magic + header", decode.is_png(pdata)
          and decode.read_header(pdata) == (320, 240))
    pfull = decode.decode_scaled(pdata)
    check("png decode lossless", np.array_equal(pfull, png_rgb))
    check("png scaled decode",
          max(decode.decode_scaled(pdata, 128).shape[:2]) == 128)
    check("png exif defaults", decode.parse_exif(
        pdata[:decode.EXIF_PREFIX_BYTES]) == decode.ExifInfo())

    apng = os.path.join(tmp, "alpha.png")
    make_test_png(apng, alpha=True)
    aarr = decode.decode_scaled(open(apng, "rb").read())
    check("png alpha flattens to white", bool((aarr[-5, -5] > 250).all()),
          str(aarr[-5, -5]))

    out = export_one(ExportItem(1, png, None), ExportOptions(dest))
    check("png export copy identical (keeps alpha path)",
          open(out, "rb").read() == pdata)
    out = export_one(ExportItem(1, png, tune_stack.to_json()),
                     ExportOptions(dest))
    odata = open(out, "rb").read()
    check("png export stays png", decode.is_png(odata))
    check("png export pixels exact (lossless)", np.array_equal(
        decode.decode_scaled(odata), render.render_stack(pfull, tune_stack)))
    out = export_one(ExportItem(1, png, rot_only.to_json()), ExportOptions(dest))
    odata = open(out, "rb").read()
    check("png rotate export (pixel path, stays png)",
          decode.is_png(odata) and decode.read_header(odata) == (240, 320))
    out = export_one(ExportItem(1, png, None),
                     ExportOptions(dest, resize_long=160))
    check("png resize export", decode.read_header(
        open(out, "rb").read()) == (160, 120))

    # ---- RAW (synthetic CFA DNG, demosaic fallback path) --------------------
    check("raw extensions scanned", ".dng" in decode.SCAN_EXTENSIONS
          and ".nef" in decode.SCAN_EXTENSIONS)
    dng = os.path.join(tmp, "shot.dng")
    make_test_dng(dng, capture="2024:07:01 10:00:00")
    rdata = open(dng, "rb").read()
    check("raw magic detected", decode.is_raw(rdata)
          and not decode.is_raw(open(png, "rb").read()))
    rinfo = decode.parse_exif_data(rdata)
    check("raw exif from tiff header", rinfo.orientation == 1
          and rinfo.capture_dt == "2024-07-01 10:00:00")
    check("raw header dims", decode.read_header(rdata) == (512, 384))
    rarr = decode.decode_scaled(rdata, 256)
    check("raw demosaic decode", rarr.shape == (192, 256, 3))
    check("raw content orientation-observable",
          float(rarr[:, :96].mean()) < 128 < float(rarr[:, -96:].mean()),
          f"{rarr[:, :96].mean():.0f} / {rarr[:, -96:].mean():.0f}")

    dng6 = os.path.join(tmp, "shot6.dng")
    make_test_dng(dng6, orientation=6)
    r6 = open(dng6, "rb").read()
    info6 = decode.parse_exif_data(r6)
    arr6 = decode.apply_orientation(decode.decode_scaled(r6, 256),
                                    info6.orientation)
    check("raw orientation bakes", info6.orientation == 6
          and arr6.shape == (256, 192, 3)
          and float(arr6[:48].mean()) < 128 < float(arr6[-48:].mean()))

    # unedited RAW exports as a byte copy of the original
    out = export_one(ExportItem(1, dng, None), ExportOptions(dest))
    check("raw export untouched = byte copy",
          out.endswith(".dng") and open(out, "rb").read() == rdata)
    # edited RAW develops to JPEG from the same pixels the app shows
    out = export_one(ExportItem(1, dng, tune_stack.to_json()),
                     ExportOptions(dest))
    odata = open(out, "rb").read()
    check("raw edited export is jpeg", out.endswith(".jpg")
          and odata[:2] == b"\xff\xd8")
    want = render.render_stack(decode.decode_scaled(rdata, None, fast=False),
                               tune_stack)
    got = decode.decode_scaled(odata)
    mad = float(np.mean(np.abs(got.astype(np.int16) - want.astype(np.int16))))
    check("raw edited export matches preview math", mad < 3.0, f"mad={mad}")
    # rotation-only stack must not attempt jpegtran on a RAW container
    out = export_one(ExportItem(1, dng, rot_only.to_json()), ExportOptions(dest))
    odata = open(out, "rb").read()
    check("raw rotate export via pixel path", out.endswith(".jpg")
          and decode.read_header(odata) == (384, 512))
    out = export_one(ExportItem(1, dng, None),
                     ExportOptions(dest, resize_long=128))
    check("raw resize export", out.endswith(".jpg") and decode.read_header(
        open(out, "rb").read()) == (128, 96))

    # ---- RAW+JPEG pairing rule ----------------------------------------------
    from models import FileEntry
    from workers import _pair_raw_jpeg

    def fe(i, path, raw):
        return FileEntry(id=i, path=path, name=os.path.basename(path),
                         mtime=0, size=0, is_raw=raw)

    paired = _pair_raw_jpeg([
        fe(1, "/d/a.jpg", False), fe(2, "/d/a.nef", True),   # a clean pair
        fe(3, "/d/b.jpg", False),                            # loner jpeg
        fe(4, "/d/c.nef", True),                             # loner raw
        fe(5, "/d/dup.jpg", False), fe(6, "/d/DUP.JPG", False),
        fe(7, "/d/dup.nef", True),                           # ambiguous stem
    ])
    by_id = {e.id: e for e in paired}
    check("pairing: clean pair collapses",
          2 not in by_id and by_id[1].raw_twin_id == 2
          and by_id[1].raw_twin_path == "/d/a.nef")
    check("pairing: loners untouched",
          by_id[3].raw_twin_id is None and 4 in by_id)
    check("pairing: ambiguous stems stay separate",
          all(i in by_id for i in (5, 6, 7))
          and by_id[5].raw_twin_id is None)

    # ---- Windows Open With / Default Apps registration ----------------------
    from desktopintegration import (WINDOWS_PROGID, register_windows_app,
                                    windows_registry_spec)
    fake_exe = os.path.join(tmp, "photoflow.exe")
    spec = {(k, n): v for k, n, v in windows_registry_spec(fake_exe)}
    check("win spec: quoted open command",
          spec[(r"Software\Classes\Applications\photoflow.exe"
                r"\shell\open\command", None)] == f'"{fake_exe}" "%1"')
    check("win spec: jpeg and raw associations",
          spec[(r"Software\photoflow\Capabilities\FileAssociations",
                ".jpg")] == WINDOWS_PROGID
          and spec[(r"Software\photoflow\Capabilities\FileAssociations",
                    ".nef")] == WINDOWS_PROGID)
    check("win spec: default-apps pointer",
          spec[(r"Software\RegisteredApplications", "photoflow")]
          == r"Software\photoflow\Capabilities")
    check("win register is a no-op off Windows",
          register_windows_app(fake_exe, force=True) is (sys.platform == "win32"))

    if sys.platform == "win32":
        import winreg

        def _deltree(sub: str) -> None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0,
                                    winreg.KEY_ALL_ACCESS) as k:
                    while True:
                        try:
                            child = winreg.EnumKey(k, 0)
                        except OSError:
                            break
                        _deltree(sub + "\\" + child)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError:
                pass

        try:
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Classes\Applications\photoflow.exe"
                    r"\shell\open\command") as k:
                got = winreg.QueryValueEx(k, None)[0]
            check("win register readback",
                  got == f'"{os.path.realpath(fake_exe)}" "%1"', got)
            check("win register idempotent",
                  register_windows_app(fake_exe, force=True))
        finally:
            _deltree(r"Software\Classes\Applications\photoflow.exe")
            _deltree(r"Software\Classes" + "\\" + WINDOWS_PROGID)
            _deltree(r"Software\photoflow")
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\RegisteredApplications", 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, "photoflow")
            except FileNotFoundError:
                pass

    print(f"\nALL PASS  (workdir {tmp})")


if __name__ == "__main__":
    main()
