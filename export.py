"""Batch export: renders each edit stack to a new JPEG with EXIF preserved.

Same math as the preview (render.py). Special cases, fastest first:
  - no visible edits, no resize  → byte-for-byte copy
  - only 90° rotations, no resize → lossless jpegtran transform, no re-encode
  - everything else              → scaled/full decode → geometry → chunked
                                   tune → optional exact resize → turbo encode
Source files are read-only, always; orientation is baked and reset to 1.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pyexiv2
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

import decode
import render
from editstack import EditStack


DEFAULT_PATTERN = "[FILE_NAME]"

# (token, label, description) — order drives the insert-buttons in the dialog
NAME_TOKENS = (
    ("[FILE_NAME]", "Name", "Original file name"),
    ("[Y]", "Year", "Capture year (falls back to file date)"),
    ("[M]", "Month", "Capture month 01–12"),
    ("[D]", "Day", "Capture day 01–31"),
    ("[H]", "Hour", "Capture hour 00–23"),
    ("[m]", "Minute", "Capture minute"),
    ("[s]", "Second", "Capture second"),
    ("[SEQ]", "Counter", "Sequence number 001, 002, …"),
)


@dataclass
class ExportOptions:
    dest_dir: str
    quality: int = 90
    resize_long: int | None = None
    name_pattern: str = DEFAULT_PATTERN


@dataclass
class ExportItem:
    fid: int
    path: str
    stack_json: str | None
    capture_dt: str | None = None   # "YYYY-MM-DD HH:MM:SS" (EXIF)
    mtime: float = 0.0
    seq: int = 1                    # assigned in export order


def _load_stack(stack_json: str | None) -> EditStack:
    return EditStack.from_json_lenient(stack_json)


def render_name(pattern: str, item: ExportItem) -> str:
    """Resolve a filename pattern to a stem (no extension — that is appended
    from the source format). Unsafe characters become '_'; a literal
    .jpg/.png typed into the pattern is stripped."""
    dt = item.capture_dt
    if dt and len(dt) >= 19:
        y, mo, d = dt[0:4], dt[5:7], dt[8:10]
        h, mi, s = dt[11:13], dt[14:16], dt[17:19]
    else:
        t = time.localtime(item.mtime or 0)
        y, mo, d = f"{t.tm_year:04d}", f"{t.tm_mon:02d}", f"{t.tm_mday:02d}"
        h, mi, s = f"{t.tm_hour:02d}", f"{t.tm_min:02d}", f"{t.tm_sec:02d}"
    stem = os.path.splitext(os.path.basename(item.path))[0]
    out = pattern or DEFAULT_PATTERN
    for token, value in (("[FILE_NAME]", stem), ("[Y]", y), ("[M]", mo),
                         ("[D]", d), ("[H]", h), ("[m]", mi), ("[s]", s),
                         ("[SEQ]", f"{item.seq:03d}")):
        out = out.replace(token, value)
    low = out.lower()
    for ext in (".jpeg", ".jpg", ".png"):
        if low.endswith(ext):
            out = out[:-len(ext)]
            break
    out = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", out).strip()
    return out or stem


def _unique_dest(dest_dir: str, filename: str) -> str:
    stem, ext = os.path.splitext(filename)
    out = os.path.join(dest_dir, filename)
    n = 1
    while os.path.exists(out):
        out = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        n += 1
    return out


def _resize_exact(arr: np.ndarray, long_edge: int) -> np.ndarray:
    h, w = arr.shape[:2]
    if max(h, w) <= long_edge:
        return arr
    if w >= h:
        nw, nh = long_edge, max(1, round(h * long_edge / w))
    else:
        nh, nw = long_edge, max(1, round(w * long_edge / h))
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    img = QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
    out = img.scaled(nw, nh, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    bpl = out.bytesPerLine()
    buf = np.frombuffer(out.constBits(), dtype=np.uint8, count=bpl * nh)
    return buf.reshape(nh, bpl)[:, :nw * 3].reshape(nh, nw, 3).copy()


def _copy_metadata(src: str, dst: str, out_w: int, out_h: int) -> None:
    """Carry EXIF/IPTC/XMP over — never the source ICC, exported pixels are
    always sRGB — then fix the tags we invalidated and tag sRGB explicitly."""
    try:
        with pyexiv2.Image(src) as si, pyexiv2.Image(dst) as di:
            si.copy_to_another_image(di, icc=False)
        with pyexiv2.Image(dst) as di:
            di.modify_exif({
                "Exif.Image.Orientation": "1",
                "Exif.Photo.PixelXDimension": str(out_w),
                "Exif.Photo.PixelYDimension": str(out_h),
                "Exif.Photo.ColorSpace": "1",  # 1 = sRGB
            })
            di.modify_icc(decode.srgb_icc_profile())
    except Exception:
        pass  # metadata is best-effort; pixels already exported


def export_one(item: ExportItem, opts: ExportOptions) -> str:
    """Returns the output path; raises on failure."""
    stack = _load_stack(item.stack_json)
    geo = stack.geometry()
    tune = render.TuneUniforms(stack.folded_tune(), stack.vignette())
    has_edits = stack.has_edits()
    src_ext = os.path.splitext(item.path)[1].lower() or ".jpg"
    out_path = _unique_dest(opts.dest_dir,
                            render_name(opts.name_pattern, item) + src_ext)

    with open(item.path, "rb") as f:
        data = f.read()
    src_is_png = decode.is_png(data)
    orientation = decode.parse_exif(data[:decode.EXIF_PREFIX_BYTES]).orientation
    # Exports are ALWAYS sRGB: sources tagged with another profile must go
    # through decode→convert→re-encode, never the byte-preserving paths.
    bytes_are_srgb = not decode.needs_srgb_conversion(data)

    # Byte copy (also the only path that preserves PNG alpha).
    if (not has_edits and opts.resize_long is None and orientation == 1
            and bytes_are_srgb):
        shutil.copyfile(item.path, out_path)
        return out_path

    # Lossless path (JPEG only): net effect is a pure 90°-multiple rotation.
    if (bytes_are_srgb and not src_is_png and opts.resize_long is None
            and stack.only_rotations() and geo.fine == 0.0
            and orientation in decode.ORIENTATION_TO_CW_DEGREES):
        total = (decode.ORIENTATION_TO_CW_DEGREES[orientation]
                 + geo.cw_degrees) % 360
        if decode.lossless_rotate(item.path, out_path, total):
            try:
                with pyexiv2.Image(out_path) as di:
                    di.modify_exif({"Exif.Image.Orientation": "1"})
            except Exception:
                pass
            return out_path
        if os.path.exists(out_path):  # jpegtran left a partial file
            os.unlink(out_path)

    # Pixel path. Decode no more than needed when downsizing.
    target = None
    if opts.resize_long is not None:
        crop_frac = min(geo.rect[2], geo.rect[3])
        target = math.ceil(opts.resize_long / max(crop_frac, 0.05))
    arr = decode.decode_scaled(data, target, fast=False)
    arr = decode.apply_orientation(arr, orientation)
    arr = render.apply_geometry(arr, geo)
    arr = render.apply_tune_uint8(arr, tune)
    if opts.resize_long is not None:
        arr = _resize_exact(arr, opts.resize_long)
    # Stay in the source format: PNG in → PNG out (lossless, quality N/A).
    blob = (decode.encode_png(arr) if src_is_png
            else decode.encode_jpeg(arr, opts.quality))
    with open(out_path, "wb") as f:
        f.write(blob)
    _copy_metadata(item.path, out_path, arr.shape[1], arr.shape[0])
    return out_path


class Exporter(QObject):
    progress = Signal(int, int, str, object)  # done, total, path, error|None
    finished = Signal(int, int)               # ok, failed

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = threading.Event()
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._done = self._ok = self._failed = 0
        self._total = 0

    def run(self, items: list[ExportItem], opts: ExportOptions) -> None:
        os.makedirs(opts.dest_dir, exist_ok=True)
        self._cancel.clear()
        self._done = self._ok = self._failed = 0
        self._total = len(items)
        self._pool = ThreadPoolExecutor(os.cpu_count() or 4,
                                        thread_name_prefix="pf-export")
        for seq, item in enumerate(items, 1):
            item.seq = seq
            self._pool.submit(self._job, item, opts)
        self._pool.shutdown(wait=False)

    def cancel(self) -> None:
        self._cancel.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _job(self, item: ExportItem, opts: ExportOptions) -> None:
        if self._cancel.is_set():
            return
        error = None
        out_path = item.path
        try:
            out_path = export_one(item, opts)
        except Exception as e:
            error = str(e)
        with self._lock:
            self._done += 1
            if error:
                self._failed += 1
            else:
                self._ok += 1
            done, total = self._done, self._total
            finished = done == total
        self.progress.emit(done, total, out_path, error)
        if finished:
            self.finished.emit(self._ok, self._failed)
