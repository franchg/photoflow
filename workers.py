"""Worker pools: directory scan, thumbnail pipeline, edited-thumb re-render,
viewer decodes. The UI thread never touches a file or a pixel.

Delivery is via WorkerHub signals; the hub lives on the UI thread so every
emit from a worker becomes a queued connection. Every job carries the
generation it was started for and bails silently once the folder changes.

Pools:
  fast    — tiny jobs: scans, cached-blob loads (I/O bound)
  thumb   — fresh decodes for visible thumbnails (CPU, sized to cores)
  bg      — background trickle + bulk re-renders (kept narrow so it never
            starves the visible-first path)
  viewer  — full-size decodes for the viewer; separate so navigation never
            queues behind thumbnail work
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

import decode
import render
from catalog import Catalog
from editstack import EditStack, StackError
from models import FileEntry

THUMB_LARGE = 1024
THUMB_SMALL = 256


def np_to_qimage(arr: np.ndarray) -> QImage:
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


def _jpeg_to_qimage(blob: bytes) -> QImage:
    return np_to_qimage(decode.decode_scaled(blob))


def _load_stack(stack_json: str | None) -> EditStack | None:
    if not stack_json:
        return None
    try:
        stack = EditStack.from_json(stack_json)
    except StackError:
        return None
    return stack if stack.has_edits() else None


class WorkerHub(QObject):
    """Lives on the UI thread; workers emit, views/models consume."""
    scan_done = Signal(int, object)                    # gen, list[FileEntry]
    thumb_ready = Signal(int, int, QImage, bool)       # gen, fid, small, edited
    meta_ready = Signal(int, int, int, int, int, object)  # gen, fid, w, h, orient, dt
    viewer_ready = Signal(int, int, QImage, int)       # gen, fid, image, level
    scan_failed = Signal(int, str)                     # gen, message


# viewer_ready levels: a higher level never gets replaced by a lower one
VIEWER_PLACEHOLDER, VIEWER_FIT, VIEWER_FULL = 0, 1, 2


class Workers:
    def __init__(self, catalog: Catalog, hub: WorkerHub):
        self.catalog = catalog
        self.hub = hub
        cores = os.cpu_count() or 4
        self._fast = ThreadPoolExecutor(4, thread_name_prefix="pf-fast")
        self._thumb = ThreadPoolExecutor(max(2, cores - 1), thread_name_prefix="pf-thumb")
        self._bg = ThreadPoolExecutor(2, thread_name_prefix="pf-bg")
        self._viewer = ThreadPoolExecutor(2, thread_name_prefix="pf-viewer")
        self._gen = 0
        self._lock = threading.Lock()
        self._inflight: set[int] = set()
        self._redo_after: dict[int, str | None] = {}  # fid -> newest stack_json

    # -- generation ---------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._gen

    def bump_generation(self) -> int:
        with self._lock:
            self._gen += 1
            self._redo_after.clear()
            return self._gen

    def _stale(self, gen: int) -> bool:
        return gen != self._gen

    def shutdown(self) -> None:
        for pool in (self._fast, self._thumb, self._bg, self._viewer):
            pool.shutdown(wait=False, cancel_futures=True)

    # -- folder scan -----------------------------------------------------------

    def scan_folder(self, folder: str, include_hidden: bool = False) -> int:
        gen = self.bump_generation()
        self._fast.submit(self._scan_job, folder, gen, include_hidden)
        return gen

    def _scan_job(self, folder: str, gen: int, include_hidden: bool) -> None:
        try:
            found = []
            with os.scandir(folder) as it:
                for de in it:
                    if not de.is_file():
                        continue
                    if de.name.startswith(".") and not include_hidden:
                        continue
                    if os.path.splitext(de.name)[1].lower() not in decode.SCAN_EXTENSIONS:
                        continue
                    st = de.stat()
                    found.append((de.path, st.st_mtime, st.st_size))
            found.sort(key=lambda t: os.path.basename(t[0]).lower())
            records = self.catalog.ingest(found).result()
        except Exception as e:
            self.hub.scan_failed.emit(gen, str(e))
            return
        if self._stale(gen):
            return
        entries = []
        for r in records:
            stack = _load_stack(r.stack_json)
            entries.append(FileEntry(
                id=r.id, path=r.path, name=os.path.basename(r.path),
                mtime=r.mtime, size=r.size, width=r.width, height=r.height,
                orientation=r.orientation, capture_dt=r.capture_dt,
                rating=r.rating, flag=r.flag, stack_json=r.stack_json,
                has_edits=stack is not None, has_thumb_cache=r.has_thumb))
        self.hub.scan_done.emit(gen, entries)
        # Background trickle: warm the cache for everything not yet thumbed.
        # On-demand (visible) requests run on the wider `thumb` pool and the
        # in-flight set keeps the two from colliding.
        for e in entries:
            if not e.has_thumb_cache:
                self._bg.submit(self._thumb_job, e.id, e.path, e.stack_json,
                                gen, True)

    # -- thumbnails -----------------------------------------------------------------

    def request_thumb(self, entry: FileEntry) -> None:
        """Called (indirectly) from model.data() for a visible row."""
        gen = self._gen
        if entry.has_thumb_cache:
            self._fast.submit(self._cached_thumb_job, entry.id, entry.path,
                              entry.stack_json, gen)
        else:
            self._thumb.submit(self._thumb_job, entry.id, entry.path,
                               entry.stack_json, gen, True)

    def rerender_thumb(self, entry: FileEntry, bulk: bool = False) -> None:
        """Stack changed: rebuild both cached thumbs through the new stack."""
        gen = self._gen
        pool = self._bg if bulk else self._thumb
        pool.submit(self._thumb_job, entry.id, entry.path, entry.stack_json,
                    gen, False)

    def _cached_thumb_job(self, fid: int, path: str, stack_json: str | None,
                          gen: int) -> None:
        if self._stale(gen):
            return
        try:
            row = self.catalog.get_thumb_small(fid)
            if row and row[0]:
                blob, edited = row
                self.hub.thumb_ready.emit(gen, fid, _jpeg_to_qimage(blob),
                                          bool(edited))
                return
        except Exception:
            pass
        # Cache miss despite the flag (e.g. deleted mid-flight): rebuild.
        self._thumb.submit(self._thumb_job, fid, path, stack_json, gen, True)

    def _begin(self, fid: int) -> bool:
        with self._lock:
            if fid in self._inflight:
                return False
            self._inflight.add(fid)
            return True

    def _end(self, fid: int, gen: int) -> None:
        with self._lock:
            self._inflight.discard(fid)
            redo = self._redo_after.pop(fid, _MISSING)
        if redo is not _MISSING and not self._stale(gen):
            # A newer stack arrived while we rendered; run again with it.
            self._thumb.submit(self._thumb_job, fid, redo[0], redo[1], gen, False)

    def _thumb_job(self, fid: int, path: str, stack_json: str | None,
                   gen: int, provisional_exif: bool) -> None:
        if self._stale(gen):
            return
        if not self._begin(fid):
            if not provisional_exif:  # a re-render must not be lost: coalesce
                with self._lock:
                    self._redo_after[fid] = (path, stack_json)
            return
        try:
            self._build_thumb(fid, path, stack_json, gen, provisional_exif)
        except Exception:
            pass  # unreadable/corrupt file: leave the placeholder
        finally:
            self._end(fid, gen)

    def _build_thumb(self, fid: int, path: str, stack_json: str | None,
                     gen: int, provisional_exif: bool) -> None:
        with open(path, "rb") as f:
            data = f.read()
        info = decode.parse_exif(data[:decode.EXIF_PREFIX_BYTES])
        stack = _load_stack(stack_json)

        try:
            w, h = decode.read_header(data)
        except Exception:
            w = h = 0
        if info.orientation in (5, 6, 7, 8):
            w, h = h, w
        self.catalog.set_meta(fid, w, h, info.orientation, info.capture_dt)
        if not self._stale(gen):
            self.hub.meta_ready.emit(gen, fid, w, h, info.orientation,
                                     info.capture_dt)

        # Stage 1: paint something *now* — the embedded EXIF thumb (only
        # valid as a stand-in when there are no edits to show).
        if provisional_exif and info.thumb and stack is None and not self._stale(gen):
            try:
                arr = decode.apply_orientation(
                    decode.decode_scaled(info.thumb), info.orientation)
                self.hub.thumb_ready.emit(gen, fid, np_to_qimage(arr), False)
            except Exception:
                pass

        # Stage 2: proper 1/N-scale decode → 1024 + 256 cached thumbs.
        if self._stale(gen):
            return
        arr = decode.apply_orientation(
            decode.decode_scaled(data, THUMB_LARGE), info.orientation)
        edited = stack is not None
        if edited:
            arr = render.render_stack(arr, stack)
        large = decode.encode_jpeg(arr, 87)
        factor = max(1, round(max(arr.shape[:2]) / THUMB_SMALL))
        small_arr = decode.box_downsample(arr, factor)
        small = decode.encode_jpeg(small_arr, 85)
        self.catalog.put_thumbs(fid, small, large, edited)
        if not self._stale(gen):
            self.hub.thumb_ready.emit(gen, fid, np_to_qimage(small_arr), edited)

    # -- viewer ---------------------------------------------------------------------------

    def request_viewer_placeholder(self, fid: int) -> None:
        """Instant viewer feedback: the cached ~1024px thumb, but only when it
        is unedited — the viewer's shader applies the stack itself, and an
        edited blob would get the edits twice."""
        gen = self._gen
        self._fast.submit(self._viewer_placeholder_job, fid, gen)

    def _viewer_placeholder_job(self, fid: int, gen: int) -> None:
        if self._stale(gen):
            return
        try:
            row = self.catalog.get_thumb_large(fid)
            if row and row[0] and not row[1]:
                self.hub.viewer_ready.emit(gen, fid, _jpeg_to_qimage(row[0]),
                                           VIEWER_PLACEHOLDER)
        except Exception:
            pass

    def request_viewer_image(self, fid: int, path: str,
                             target_long: int | None) -> None:
        """Decode for the viewer at (at most) target_long px on the long edge;
        None means full resolution. Emits unedited, orientation-normalized
        pixels — the viewer's shader applies the stack."""
        gen = self._gen
        self._viewer.submit(self._viewer_job, fid, path, target_long, gen)

    def _viewer_job(self, fid: int, path: str, target_long: int | None,
                    gen: int) -> None:
        if self._stale(gen):
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            info = decode.parse_exif(data[:decode.EXIF_PREFIX_BYTES])
            arr = decode.apply_orientation(
                decode.decode_scaled(data, target_long, fast=True),
                info.orientation)
        except Exception:
            return
        if not self._stale(gen):
            level = VIEWER_FULL if target_long is None else VIEWER_FIT
            self.hub.viewer_ready.emit(gen, fid, np_to_qimage(arr), level)


_MISSING = object()
