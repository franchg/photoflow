"""End-to-end headless verification of the non-GUI pipeline.

Run:  uv run python tests/verify_headless.py
Generates synthetic JPEGs (with real EXIF via pyexiv2), then exercises
decode, EXIF parsing, the catalog, stack folding, render math, and every
export path. Asserts throughout; prints PASS lines.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyexiv2

import decode
import render
from catalog import Catalog
from editstack import (EditClipboard, EditStack, Op, StackError, StackHistory,
                       validate_op)
from export import ExportItem, ExportOptions, export_one


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
        validate_op(Op("rotate", {"degrees": 45}))
        check("validate rejects 45deg", False)
    except StackError:
        check("validate rejects 45deg", True)
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

    amb_pos = render.TuneUniforms(
        EditStack([Op("tune", {"ambiance": 1.0})]).folded_tune())
    check("ambiance +1 = colorful and happy",
          amb_pos.saturation > 1.3 and amb_pos.contrast == 1.0
          and amb_pos.shadows > 0.2)
    amb_neg = render.TuneUniforms(
        EditStack([Op("tune", {"ambiance": -1.0})]).folded_tune())
    check("ambiance -1 = contrasty, less colorful",
          amb_neg.saturation < 0.7 and amb_neg.contrast > 1.15
          and amb_neg.shadows < 0)

    gray = np.array([[[0.15] * 3, [0.85] * 3]], dtype=np.float32)
    hl = render.TuneUniforms(
        EditStack([Op("tune", {"highlights": -0.8})]).folded_tune())
    out_hl = render.apply_tune(gray.copy(), hl)
    check("highlights -: darkens brights, spares darks",
          out_hl[0, 1, 0] < 0.70 and abs(out_hl[0, 0, 0] - 0.15) < 0.02,
          str(out_hl))
    sh = render.TuneUniforms(
        EditStack([Op("tune", {"shadows": 0.8})]).folded_tune())
    out_sh = render.apply_tune(gray.copy(), sh)
    check("shadows +: lifts darks, spares brights",
          out_sh[0, 0, 0] > 0.30 and abs(out_sh[0, 1, 0] - 0.85) < 0.02,
          str(out_sh))

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

    print(f"\nALL PASS  (workdir {tmp})")


if __name__ == "__main__":
    main()
