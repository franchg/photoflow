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
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pyexiv2
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSlider, QSpinBox)

import decode
import render
from editstack import EditStack, StackError


@dataclass
class ExportOptions:
    dest_dir: str
    quality: int = 90
    resize_long: int | None = None


@dataclass
class ExportItem:
    fid: int
    path: str
    stack_json: str | None


def _load_stack(stack_json: str | None) -> EditStack:
    if not stack_json:
        return EditStack()
    try:
        return EditStack.from_json(stack_json)
    except StackError:
        return EditStack()


def _unique_dest(dest_dir: str, src_path: str) -> str:
    base = os.path.basename(src_path)
    stem, ext = os.path.splitext(base)
    out = os.path.join(dest_dir, base)
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
    """Carry EXIF/IPTC/XMP/ICC over, then fix the tags we invalidated."""
    try:
        with pyexiv2.Image(src) as si, pyexiv2.Image(dst) as di:
            try:
                si.copy_to_another_image(di)
            except Exception:
                # exiv2 raises on absent/invalid ICC and aborts the whole
                # copy — retry without it
                si.copy_to_another_image(di, icc=False)
        with pyexiv2.Image(dst) as di:
            di.modify_exif({
                "Exif.Image.Orientation": "1",
                "Exif.Photo.PixelXDimension": str(out_w),
                "Exif.Photo.PixelYDimension": str(out_h),
            })
    except Exception:
        pass  # metadata is best-effort; pixels already exported


def export_one(item: ExportItem, opts: ExportOptions) -> str:
    """Returns the output path; raises on failure."""
    stack = _load_stack(item.stack_json)
    geo = stack.geometry()
    tune = render.TuneUniforms(stack.folded_tune())
    has_edits = stack.has_edits()
    out_path = _unique_dest(opts.dest_dir, item.path)

    with open(item.path, "rb") as f:
        prefix = f.read(decode.EXIF_PREFIX_BYTES)
    orientation = decode.parse_exif(prefix).orientation

    if not has_edits and opts.resize_long is None and orientation == 1:
        shutil.copyfile(item.path, out_path)
        return out_path

    # Lossless path: net effect is a pure 90° rotation of the source bytes.
    if (opts.resize_long is None and stack.only_rotations()
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
    with open(item.path, "rb") as f:
        data = f.read()
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
    blob = decode.encode_jpeg(arr, opts.quality)
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
        for item in items:
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


class ExportDialog(QDialog):
    def __init__(self, count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Export {count} image{'s' if count != 1 else ''}")
        form = QFormLayout(self)

        dest_row = QHBoxLayout()
        self._dest = QLineEdit(os.path.expanduser("~/Pictures/photoflow-export"))
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse)
        dest_row.addWidget(self._dest, 1)
        dest_row.addWidget(browse)
        form.addRow("Destination", dest_row)

        q_row = QHBoxLayout()
        self._quality = QSlider(Qt.Orientation.Horizontal)
        self._quality.setRange(60, 100)
        self._quality.setValue(90)
        self._q_label = QLabel("90")
        self._quality.valueChanged.connect(lambda v: self._q_label.setText(str(v)))
        q_row.addWidget(self._quality, 1)
        q_row.addWidget(self._q_label)
        form.addRow("Quality", q_row)

        r_row = QHBoxLayout()
        self._resize_on = QCheckBox("Long edge")
        self._resize_px = QSpinBox()
        self._resize_px.setRange(64, 30000)
        self._resize_px.setValue(2048)
        self._resize_px.setEnabled(False)
        self._resize_on.toggled.connect(self._resize_px.setEnabled)
        r_row.addWidget(self._resize_on)
        r_row.addWidget(self._resize_px, 1)
        form.addRow("Resize", r_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export destination",
                                             self._dest.text())
        if d:
            self._dest.setText(d)

    def options(self) -> ExportOptions:
        return ExportOptions(
            dest_dir=self._dest.text().strip(),
            quality=self._quality.value(),
            resize_long=self._resize_px.value() if self._resize_on.isChecked() else None)
