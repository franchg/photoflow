"""Image decode/encode: JPEG via libjpeg-turbo (hot path, scaled DCT decode),
PNG via Qt's codec, RAW via LibRaw (rawpy). Format is dispatched on magic
bytes right here so the rest of the app stays format-blind.

Everything here is thread-safe: TurboJPEG handles are thread-local,
libjpeg-turbo releases the GIL, and QImage decode/scale works off the GUI
thread. PNG has no scaled decode or embedded EXIF thumb — those stages
degrade gracefully (full decode + smooth downscale, no provisional thumb).
PNG alpha is flattened over white everywhere except byte-copy exports.

RAW is read through its embedded camera preview wherever the preview can
serve the request: cameras store a developed JPEG inside every RAW (most
full-size, Sony ~1616 px), extracting it costs ~15 ms, and it is what the
photographer saw on the camera. Requests the preview can't cover — full
resolution when the preview is smaller than the sensor, or no preview at
all — demosaic via LibRaw instead. Both paths return unoriented pixels
like the JPEG path; callers apply EXIF orientation.
"""
from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import threading
import zlib
from typing import NamedTuple

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColorSpace, QImage
from turbojpeg import TJFLAG_FASTDCT, TJFLAG_FASTUPSAMPLE, TJPF_RGB, TurboJPEG

JPEG_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif"}
PNG_EXTENSIONS = {".png"}
RAW_EXTENSIONS = {".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw",
                  ".raf", ".orf", ".rw2", ".pef", ".srw"}
SCAN_EXTENSIONS = JPEG_EXTENSIONS | PNG_EXTENSIONS | RAW_EXTENSIONS

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*")  # dng/cr2/nef/nrw/arw/pef/srw
_RAW_MAGICS = _TIFF_MAGICS + (
    b"IIRO", b"IIRS", b"MMOR",  # Olympus ORF variants
    b"IIU\x00",                 # Panasonic RW2
)


def is_png(data: bytes) -> bool:
    return data[:8] == _PNG_MAGIC


def is_raw(data: bytes) -> bool:
    head = data[:16]
    return (head[:4] in _RAW_MAGICS
            or head[4:12] == b"ftypcrx "        # Canon CR3 (BMFF)
            or head[:15] == b"FUJIFILMCCD-RAW")  # Fujifilm RAF

# DCT-domain downscale factors supported by libjpeg-turbo, smallest first.
SCALING_FACTORS = ((1, 8), (1, 4), (3, 8), (1, 2), (5, 8), (3, 4), (7, 8), (1, 1))

_local = threading.local()


def _bundle_dir() -> str | None:
    """PyInstaller bundle dir when frozen (native libs live there), else None."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return None


def _turbojpeg_lib_path() -> str | None:
    """Bundled turbojpeg when frozen; None lets PyTurboJPEG search the
    platform default locations (system lib on Linux, C:\\libjpeg-turbo64 on
    Windows)."""
    bundle = _bundle_dir()
    if bundle is not None:
        name = {"win32": "turbojpeg.dll", "darwin": "libturbojpeg.dylib"}.get(
            sys.platform, "libturbojpeg.so.0")
        path = os.path.join(bundle, name)
        if os.path.exists(path):
            return path
    if sys.platform == "darwin":
        # Apple Silicon Homebrew prefix, which PyTurboJPEG 1.x doesn't search
        path = "/opt/homebrew/opt/jpeg-turbo/lib/libturbojpeg.dylib"
        if os.path.exists(path):
            return path
    return None


def _tj() -> TurboJPEG:
    tj = getattr(_local, "tj", None)
    if tj is None:
        tj = _local.tj = TurboJPEG(_turbojpeg_lib_path())
    return tj


def read_header(data: bytes) -> tuple[int, int]:
    """(width, height) without decoding pixels. For RAW these are LibRaw's
    processed dimensions — what a demosaic would produce — so zoom, fit
    and export are sized by the sensor even when browsing rides a smaller
    embedded preview (Sony ARW embeds only ~1616 px)."""
    if is_png(data):
        return struct.unpack(">II", data[16:24])  # IHDR is always first
    if is_raw(data):
        return _raw_probe(data)[1:]
    width, height, _subsample, _colorspace = _tj().decode_header(data)
    return width, height


def qimage_to_rgb(img: QImage) -> np.ndarray:
    """QImage (any format) → HxWx3 uint8; alpha is flattened over white."""
    if img.hasAlphaChannel():
        img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        h, w, bpl = img.height(), img.width(), img.bytesPerLine()
        buf = np.frombuffer(img.constBits(), np.uint8, bpl * h)
        rgba = buf.reshape(h, bpl)[:, :w * 4].reshape(h, w, 4).astype(np.float32)
        a = rgba[..., 3:4] / 255.0
        return (rgba[..., :3] * a + 255.0 * (1.0 - a) + 0.5).astype(np.uint8)
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    h, w, bpl = img.height(), img.width(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, bpl * h)
    return buf.reshape(h, bpl)[:, :w * 3].reshape(h, w, 3).copy()


# ---------------------------------------------------------------------------
# Color management: sRGB is the app's working space. ICC-tagged sources
# (Adobe RGB cameras, Display P3 phones) are converted right here at decode,
# so everything downstream — thumbs, viewer, edit math, export — stays sRGB.
# (EXIF-embedded provisional thumbs are the one knowing exception: they show
# unconverted for a moment until the real decode replaces them.)
# ---------------------------------------------------------------------------

_ICC_MARKER = b"ICC_PROFILE\x00"
_srgb: QColorSpace | None = None


def _srgb_space() -> QColorSpace:
    global _srgb
    if _srgb is None:
        _srgb = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
    return _srgb


def srgb_icc_profile() -> bytes:
    """A standard sRGB ICC blob (for tagging exports explicitly)."""
    return bytes(_srgb_space().iccProfile())


def parse_icc(data: bytes) -> bytes | None:
    """Assemble the ICC profile from JPEG APP2 chunks; None if untagged."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    chunks: dict[int, bytes] = {}
    total = 0
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xDA, 0xD9):  # SOS/EOI — no more metadata segments
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        payload = data[i + 4:i + 2 + seg_len]
        if marker == 0xE2 and payload.startswith(_ICC_MARKER) and len(payload) > 14:
            total = payload[13]
            chunks[payload[12]] = payload[14:]
        i += 2 + seg_len
    if not chunks or total == 0 or len(chunks) != total:
        return None
    return b"".join(chunks[k] for k in sorted(chunks))


def _png_icc(data: bytes) -> bytes | None:
    """Extract the iCCP profile via a cheap chunk walk (Qt tags even sRGB
    PNGs with one, so presence alone means nothing — callers compare)."""
    i, n = 8, len(data)
    while i + 8 <= n:
        (length,) = struct.unpack(">I", data[i:i + 4])
        ctype = data[i + 4:i + 8]
        if ctype == b"iCCP":
            payload = data[i + 8:i + 8 + length]
            z = payload.find(b"\x00")  # name NUL, then 1 compression byte
            if z < 0 or z + 2 >= len(payload):
                return None
            try:
                return zlib.decompress(payload[z + 2:])
            except zlib.error:
                return None
        if ctype == b"IDAT":
            break
        i += 12 + length  # length + type + data + crc
    return None


def needs_srgb_conversion(data: bytes) -> bool:
    """True when the file carries a non-sRGB ICC profile — such sources must
    re-encode on export (exports are always sRGB), never byte-copy."""
    icc = _png_icc(data) if is_png(data) else parse_icc(data)
    if icc is None:
        return False
    cs = QColorSpace.fromIccProfile(QByteArray(icc))
    return cs.isValid() and cs != _srgb_space()


def convert_to_srgb(rgb: np.ndarray, icc: bytes | None) -> np.ndarray:
    """Map ICC-tagged pixels into sRGB. Untagged, already-sRGB, or
    unparseable (LUT-based etc.) profiles return the input unchanged."""
    if not icc:
        return rgb
    cs = QColorSpace.fromIccProfile(QByteArray(icc))
    if not cs.isValid() or cs == _srgb_space():
        return rgb
    if not rgb.flags["C_CONTIGUOUS"]:
        rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
    img.applyColorTransform(cs.transformationToColorSpace(_srgb_space()))
    return qimage_to_rgb(img)


def _decode_png(data: bytes, target_long_edge: int | None) -> np.ndarray:
    img = QImage.fromData(data)
    if img.isNull():
        raise ValueError("cannot decode PNG")
    cs = img.colorSpace()
    if cs.isValid() and cs != _srgb_space():
        img.convertToColorSpace(_srgb_space())
    if (target_long_edge is not None
            and max(img.width(), img.height()) > target_long_edge):
        if img.width() >= img.height():
            img = img.scaledToWidth(target_long_edge,
                                    Qt.TransformationMode.SmoothTransformation)
        else:
            img = img.scaledToHeight(target_long_edge,
                                     Qt.TransformationMode.SmoothTransformation)
    return qimage_to_rgb(img)


def pick_scale(src_w: int, src_h: int, target_long_edge: int) -> tuple[int, int]:
    """Smallest DCT scale whose long edge still covers target_long_edge."""
    long_edge = max(src_w, src_h)
    for num, den in SCALING_FACTORS:
        if long_edge * num >= target_long_edge * den:
            return num, den
    return 1, 1


# ---------------------------------------------------------------------------
# RAW: the embedded camera preview is the image (see module docstring).
# ---------------------------------------------------------------------------

_RAW_PREVIEW_MIN = 1024  # smaller embedded previews aren't worth showing


def _raw_probe(data: bytes) -> tuple[bytes | None, int, int]:
    """One LibRaw open: (usable embedded JPEG preview or None, processed
    width, height). Deliberately uncached — a cache keyed by whole-file
    bytes retains hundreds of MB at flagship RAW sizes, and the open
    itself costs ~10 ms on a worker thread."""
    import rawpy
    with rawpy.imread(io.BytesIO(data)) as raw:
        s = raw.sizes
        try:
            thumb = raw.extract_thumb()
        except Exception:
            return None, s.width, s.height
    if thumb.format != rawpy.ThumbFormat.JPEG:
        return None, s.width, s.height
    jpeg = bytes(thumb.data)
    try:
        w, h = _tj().decode_header(jpeg)[:2]
    except Exception:
        return None, s.width, s.height
    if max(w, h) < _RAW_PREVIEW_MIN:
        return None, s.width, s.height
    return jpeg, s.width, s.height


def _smooth_resize(rgb: np.ndarray, target_long_edge: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    if max(w, h) <= target_long_edge:
        return rgb
    if not rgb.flags["C_CONTIGUOUS"]:
        rgb = np.ascontiguousarray(rgb)
    img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
    if w >= h:
        img = img.scaledToWidth(target_long_edge,
                                Qt.TransformationMode.SmoothTransformation)
    else:
        img = img.scaledToHeight(target_long_edge,
                                 Qt.TransformationMode.SmoothTransformation)
    return qimage_to_rgb(img)


def _raw_postprocess(data: bytes, target_long_edge: int | None) -> np.ndarray:
    """Demosaic fallback for RAWs without a usable preview. user_flip=0
    keeps the pixels unoriented, matching every other decode path."""
    import rawpy
    with rawpy.imread(io.BytesIO(data)) as raw:
        s = raw.sizes
        half = (target_long_edge is not None
                and target_long_edge * 2 <= max(s.width, s.height))
        rgb = raw.postprocess(use_camera_wb=True, user_flip=0,
                              half_size=half, output_bps=8)
    if target_long_edge is not None:
        rgb = _smooth_resize(rgb, target_long_edge)
    return np.ascontiguousarray(rgb)


def _decode_raw(data: bytes, target_long_edge: int | None) -> np.ndarray | bytes:
    """RAW dispatch: the embedded preview serves every request it can
    cover (that is what makes RAW browsing JPEG-fast), the demosaic
    serves the rest — full-size views and exports of files whose preview
    is smaller than the sensor, like Sony ARW. Returns preview JPEG
    bytes (ride the JPEG path) or a demosaiced array."""
    jpeg, sensor_w, sensor_h = _raw_probe(data)
    if jpeg is None:
        return _raw_postprocess(data, target_long_edge)
    plong = max(_tj().decode_header(jpeg)[:2])
    if target_long_edge is not None and plong >= target_long_edge:
        return jpeg
    if plong >= 0.98 * max(sensor_w, sensor_h):  # preview IS full resolution
        return jpeg
    return _raw_postprocess(data, target_long_edge)


def _raw_flip_orientation(data: bytes) -> int:
    """LibRaw's flip (dcraw convention) as an EXIF orientation — the
    fallback for containers the TIFF walker can't read (CR3, RAF)."""
    import rawpy
    with rawpy.imread(io.BytesIO(data)) as raw:
        return {0: 1, 3: 3, 5: 8, 6: 6}.get(raw.sizes.flip, 1)


def decode_scaled(data: bytes, target_long_edge: int | None = None,
                  fast: bool = True) -> np.ndarray:
    """Decode to RGB uint8, never producing more pixels than needed.

    target_long_edge=None decodes full resolution. `fast` trades a little
    accuracy for speed (thumbnails); export passes fast=False. PNG input is
    dispatched to Qt's codec (no scaled decode there; `fast` is ignored).
    RAW input unwraps to its embedded preview and rides the JPEG path;
    previewless files demosaic instead.
    """
    if is_png(data):
        return _decode_png(data, target_long_edge)
    if is_raw(data):
        result = _decode_raw(data, target_long_edge)
        if isinstance(result, np.ndarray):
            return result
        data = result  # the preview JPEG rides the normal path below
    tj = _tj()
    if target_long_edge is None:
        sf = (1, 1)
    else:
        w, h = read_header(data)
        sf = pick_scale(w, h, target_long_edge)
    flags = (TJFLAG_FASTDCT | TJFLAG_FASTUPSAMPLE) if fast else 0
    rgb = tj.decode(data, pixel_format=TJPF_RGB, scaling_factor=sf, flags=flags)
    return convert_to_srgb(rgb, parse_icc(data))


def encode_jpeg(rgb: np.ndarray, quality: int = 87) -> bytes:
    if not rgb.flags["C_CONTIGUOUS"]:
        rgb = np.ascontiguousarray(rgb)
    return _tj().encode(rgb, quality=quality, pixel_format=TJPF_RGB)


def encode_png(rgb: np.ndarray) -> bytes:
    if not rgb.flags["C_CONTIGUOUS"]:
        rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
    img.setColorSpace(_srgb_space())  # write the sRGB chunk — exports are sRGB
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def box_downsample(rgb: np.ndarray, factor: int) -> np.ndarray:
    """Cheap high-quality integer downscale (for small thumbs from large ones)."""
    if factor <= 1:
        return rgb
    h, w = rgb.shape[:2]
    h2, w2 = (h // factor) * factor, (w // factor) * factor
    trimmed = rgb[:h2, :w2].reshape(h2 // factor, factor, w2 // factor, factor, 3)
    return trimmed.mean(axis=(1, 3)).astype(np.uint8)


def apply_orientation(rgb: np.ndarray, orientation: int) -> np.ndarray:
    """Bake EXIF orientation (1..8) into pixel order."""
    if orientation == 2:
        rgb = rgb[:, ::-1]
    elif orientation == 3:
        rgb = rgb[::-1, ::-1]
    elif orientation == 4:
        rgb = rgb[::-1]
    elif orientation == 5:
        rgb = rgb.transpose(1, 0, 2)
    elif orientation == 6:
        rgb = np.rot90(rgb, 3)
    elif orientation == 7:
        rgb = rgb.transpose(1, 0, 2)[::-1, ::-1]
    elif orientation == 8:
        rgb = np.rot90(rgb, 1)
    else:
        return rgb
    return np.ascontiguousarray(rgb)


ORIENTATION_TO_CW_DEGREES = {1: 0, 3: 180, 6: 90, 8: 270}  # pure-rotation subset


# --------------------------------------------------------------------------
# Minimal EXIF parsing. We only need three things in the hot path
# (orientation, DateTimeOriginal, the embedded thumbnail) and reading them
# straight out of the APP1/TIFF structure avoids per-file library overhead.
# pyexiv2 is used only on the export path for full metadata preservation.
# --------------------------------------------------------------------------

EXIF_PREFIX_BYTES = 128 * 1024  # APP1 caps at 64 KB and precedes scan data


class ExifInfo(NamedTuple):
    orientation: int = 1
    capture_dt: str | None = None  # "YYYY-MM-DD HH:MM:SS", lexicographically sortable
    thumb: bytes | None = None     # embedded JPEG thumbnail


def parse_exif(prefix: bytes) -> ExifInfo:
    try:
        return _parse_exif(prefix)
    except Exception:
        return ExifInfo()


def parse_exif_data(data: bytes) -> ExifInfo:
    """parse_exif over full file bytes, with a LibRaw orientation fallback
    for the RAW containers the TIFF walker can't read (CR3, RAF)."""
    info = parse_exif(data[:EXIF_PREFIX_BYTES])
    if (info == ExifInfo() and is_raw(data)
            and data[:2] not in (b"II", b"MM")):
        try:
            return ExifInfo(_raw_flip_orientation(data), None, None)
        except Exception:
            pass
    return info


def _find_app1(prefix: bytes) -> bytes | None:
    if prefix[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(prefix)
    while i + 4 <= n:
        if prefix[i] != 0xFF:
            return None
        marker = prefix[i + 1]
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:  # start of scan: no APP1 ahead
            return None
        seglen = struct.unpack_from(">H", prefix, i + 2)[0]
        if marker == 0xE1 and prefix[i + 4:i + 10] == b"Exif\x00\x00":
            return prefix[i + 10:i + 2 + seglen]
        i += 2 + seglen
    return None


def _parse_exif(prefix: bytes) -> ExifInfo:
    # TIFF-based RAW containers are TIFF from byte zero; JPEG carries the
    # same structure inside APP1.
    if prefix[:2] in (b"II", b"MM"):
        tiff = prefix
    else:
        tiff = _find_app1(prefix)
    if not tiff or tiff[:2] not in (b"II", b"MM"):
        return ExifInfo()
    fmt = "<" if tiff[:2] == b"II" else ">"

    def u16(off: int) -> int:
        return struct.unpack_from(fmt + "H", tiff, off)[0]

    def u32(off: int) -> int:
        return struct.unpack_from(fmt + "I", tiff, off)[0]

    def read_ifd(off: int) -> tuple[dict[int, tuple[int, int, int]], int]:
        """tag -> (type, count, offset of the 4-byte value field); plus next-IFD offset."""
        count = u16(off)
        entries = {}
        for k in range(count):
            eoff = off + 2 + 12 * k
            entries[u16(eoff)] = (u16(eoff + 2), u32(eoff + 4), eoff + 8)
        return entries, u32(off + 2 + 12 * count)

    def short_value(entry: tuple[int, int, int]) -> int | None:
        typ, cnt, voff = entry
        if typ == 3 and cnt >= 1:
            return u16(voff)
        if typ == 4 and cnt >= 1:
            return u32(voff)
        return None

    def ascii_value(entry: tuple[int, int, int]) -> str | None:
        typ, cnt, voff = entry
        if typ != 2:
            return None
        raw = tiff[voff:voff + cnt] if cnt <= 4 else tiff[u32(voff):u32(voff) + cnt]
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip() or None

    ifd0, next_ifd = read_ifd(u32(4))

    orientation = 1
    if 0x0112 in ifd0:
        val = short_value(ifd0[0x0112])
        if val is not None and 1 <= val <= 8:
            orientation = val

    capture_dt = None
    if 0x8769 in ifd0:  # Exif sub-IFD pointer
        exif_ptr = short_value(ifd0[0x8769])
        if exif_ptr:
            sub, _ = read_ifd(exif_ptr)
            for tag in (0x9003, 0x9004, 0x0132):  # DateTimeOriginal preferred
                if tag in sub:
                    raw = ascii_value(sub[tag])
                    if raw and len(raw) >= 19:
                        capture_dt = raw[:10].replace(":", "-") + raw[10:19]
                        break

    thumb = None
    if next_ifd:
        ifd1, _ = read_ifd(next_ifd)
        if 0x0201 in ifd1 and 0x0202 in ifd1:
            toff, tlen = short_value(ifd1[0x0201]), short_value(ifd1[0x0202])
            if toff and tlen:
                candidate = tiff[toff:toff + tlen]
                # length check: RAW prefixes may truncate the slice
                if len(candidate) == tlen and candidate[:2] == b"\xff\xd8":
                    thumb = bytes(candidate)

    return ExifInfo(orientation, capture_dt, thumb)


# --------------------------------------------------------------------------
# Lossless rotation via jpegtran (libjpeg-turbo-progs).
# --------------------------------------------------------------------------

def _jpegtran_cmd() -> str | None:
    """Bundled jpegtran when frozen, else whatever is on PATH."""
    bundle = _bundle_dir()
    if bundle is not None:
        exe = os.path.join(
            bundle, "jpegtran.exe" if sys.platform == "win32" else "jpegtran")
        if os.path.exists(exe):
            return exe
    return shutil.which("jpegtran")


def jpegtran_available() -> bool:
    return _jpegtran_cmd() is not None


def lossless_rotate(src_path: str, dst_path: str, cw_degrees: int) -> bool:
    """Rotate by a 90° multiple without re-encoding. Returns False if jpegtran
    is missing or refuses (-perfect fails on non-iMCU-aligned edges)."""
    cw_degrees %= 360
    if cw_degrees == 0:
        shutil.copyfile(src_path, dst_path)
        return True
    cmd = _jpegtran_cmd()
    if cmd is None:
        return False
    result = subprocess.run(
        [cmd, "-rot", str(cw_degrees), "-perfect", "-copy", "all",
         "-outfile", dst_path, src_path],
        capture_output=True,
    )
    return result.returncode == 0
