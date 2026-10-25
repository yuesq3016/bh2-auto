"""Image IO and small array helpers.

PNG _decoding_ uses Pillow when it is importable (it usually is, and it is the
only sane way to read the screenshots users hand us).  PNG _encoding_ and BMP
read/write are implemented here with zlib/struct so that saving a debug frame
never depends on anything outside the standard library.
"""

from __future__ import annotations

import os
import re
import struct
import zlib

import numpy as np

try:  # optional, strongly recommended
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover - exercised only on bare interpreters
    _PILImage = None

HAVE_PIL = _PILImage is not None


class ImageError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# PNG encoding (stdlib only)
# --------------------------------------------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(rgb: np.ndarray, level: int = 6) -> bytes:
    """Encode an (H, W, 3) uint8 array as PNG bytes."""
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ImageError(f"encode_png expects (H, W, 3), got {arr.shape}")
    h, w = arr.shape[:2]
    # Filter type 0 (None) in front of every scanline.
    raw = np.empty((h, w * 3 + 1), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = arr.reshape(h, w * 3)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit truecolour RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw.tobytes(), level))
        + _png_chunk(b"IEND", b"")
    )


def save_png(path: str | os.PathLike[str], rgb: np.ndarray) -> str:
    path = os.fspath(path)
    _ensure_parent(path)
    with open(path, "wb") as fh:
        fh.write(encode_png(rgb))
    return path


# --------------------------------------------------------------------------
# BMP (stdlib only, both directions)
# --------------------------------------------------------------------------

def save_bmp(path: str | os.PathLike[str], rgb: np.ndarray) -> str:
    """Write an uncompressed 24-bit BMP.

    BMP pads every row up to a 4-byte boundary *in bytes*, so the padding is
    ``(-3 * width) % 4`` bytes per row — not pixels.  Adding a pixel-shaped pad
    array instead shifts each row by three bytes and yields the classic
    "horribly slanted" image on reload.  The padded buffer is therefore built
    as raw bytes with an explicit stride.
    """
    path = os.fspath(path)
    _ensure_parent(path)
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = arr.shape[:2]
    stride = (w * 3 + 3) & ~3
    rows = np.zeros((h, stride), dtype=np.uint8)
    rows[:, :w * 3] = arr[:, :, ::-1].reshape(h, w * 3)  # BGR, padded per row
    # A positive-height BMP stores rows bottom-up.
    body = rows[::-1].tobytes()
    size = 14 + 40 + len(body)
    header = b"BM" + struct.pack("<IHHI", size, 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    with open(path, "wb") as fh:
        fh.write(header + info + body)
    return path


def load_bmp(path: str | os.PathLike[str]) -> np.ndarray:
    with open(os.fspath(path), "rb") as fh:
        data = fh.read()
    if data[:2] != b"BM":
        raise ImageError(f"not a BMP file: {path}")
    data_off = struct.unpack_from("<I", data, 10)[0]
    hdr_size = struct.unpack_from("<I", data, 14)[0]
    if hdr_size < 40:
        raise ImageError("unsupported BMP header (BITMAPCOREHEADER)")
    width, height, planes, bpp, compression = struct.unpack_from("<iiHHI", data, 18)
    if compression != 0:
        raise ImageError(f"unsupported BMP compression: {compression}")
    if bpp not in (24, 32):
        raise ImageError(f"unsupported BMP bit depth: {bpp}")
    top_down = height < 0
    h = abs(height)
    stride = ((width * bpp // 8) + 3) & ~3
    buf = np.frombuffer(data, dtype=np.uint8, count=stride * h, offset=data_off)
    buf = buf.reshape(h, stride)[:, : width * (bpp // 8)]
    buf = buf.reshape(h, width, bpp // 8)
    if not top_down:
        buf = buf[::-1]
    # Both 24bpp and 32bpp BMPs store channels as B, G, R (then A): take the
    # first three reversed, which also drops the pad byte for 32bpp.  Getting
    # this backwards produces a red/blue-swapped image, which is exactly the
    # kind of bug that silently ruins colour matching.
    rgb = buf[:, :, 2::-1] if bpp == 32 else buf[:, :, ::-1]
    return np.ascontiguousarray(rgb)


# --------------------------------------------------------------------------
# Generic loading
# --------------------------------------------------------------------------

_EXT_READERS = {
    ".bmp": None,
    ".png": None,
    ".jpg": None,
    ".jpeg": None,
    ".webp": None,
}


def load_image(path: str | os.PathLike[str]) -> np.ndarray:
    """Load an image file as an (H, W, 3) uint8 RGB array.

    Orientation/EXIF is intentionally ignored: templates are cropped from raw
    screen grabs, which never carry EXIF.
    """
    path = os.fspath(path)
    if not os.path.exists(path):
        raise ImageError(f"image not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".bmp":
        return load_bmp(path)
    if _PILImage is None:
        raise ImageError(
            f"cannot read {ext} without Pillow; install Pillow or use .bmp/.png "
            f"(PNG/PIL decoding unavailable)"
        )
    with _PILImage.open(path) as im:
        im = im.convert("RGB")
        return np.asarray(im, dtype=np.uint8)


def save_image(path: str | os.PathLike[str], rgb: np.ndarray) -> str:
    """Save RGB array, choosing the encoder from the file extension."""
    ext = os.path.splitext(os.fspath(path))[1].lower()
    if ext in (".png",):
        return save_png(path, rgb)
    if ext == ".bmp":
        return save_bmp(path, rgb)
    if _PILImage is None:
        raise ImageError(f"cannot write {ext} without Pillow; use .png or .bmp")
    _ensure_parent(path)
    _PILImage.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8), "RGB").save(path)
    return os.fspath(path)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


# --------------------------------------------------------------------------
# Array helpers
# --------------------------------------------------------------------------

def to_gray(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 luma as float32 (colour weights matter for red-heavy game UI)."""
    arr = np.asarray(rgb, dtype=np.float32)
    return arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114


def crop_roi(arr: np.ndarray, roi: tuple[int, int, int, int] | None) -> np.ndarray:
    """Crop (x, y, w, h), clamped to the image bounds."""
    if roi is None:
        return arr
    h, w = arr.shape[:2]
    x, y, rw, rh = (int(v) for v in roi)
    x0 = max(0, min(x, w - 1))
    y0 = max(0, min(y, h - 1))
    x1 = max(x0 + 1, min(x + max(1, rw), w))
    y1 = max(y0 + 1, min(y + max(1, rh), h))
    return arr[y0:y1, x0:x1]


def resize_rgb(arr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Resize to (width, height).  Uses Pillow when present (much better)."""
    w, h = int(size[0]), int(size[1])
    if w <= 0 or h <= 0:
        raise ImageError(f"bad resize target: {size}")
    if _PILImage is not None:
        im = _PILImage.fromarray(np.ascontiguousarray(arr, dtype=np.uint8), "RGB")
        return np.asarray(im.resize((w, h), _PILImage.Resampling.BILINEAR), dtype=np.uint8)
    return _resize_nearest(arr, w, h)


def _resize_nearest(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    src_h, src_w = arr.shape[:2]
    ys = np.clip((np.arange(h) * src_h / h).astype(np.int64), 0, src_h - 1)
    xs = np.clip((np.arange(w) * src_w / w).astype(np.int64), 0, src_w - 1)
    return arr[ys][:, xs]


def parse_scale_list(text: str) -> list[float]:
    """Parse "1.0" / "0.9,1.0,1.1" / "0.9:1.1:0.05" into a list of floats."""
    text = (text or "").strip()
    if not text:
        return [1.0]
    if ":" in text:
        parts = [p for p in re.split(r"[:]", text) if p.strip()]
        if len(parts) not in (2, 3):
            raise ValueError(f"bad scale range: {text!r}")
        lo, hi = float(parts[0]), float(parts[1])
        step = float(parts[2]) if len(parts) == 3 else 0.05
        if step <= 0 or hi < lo:
            raise ValueError(f"bad scale range: {text!r}")
        out = []
        v = lo
        while v <= hi + 1e-9:
            out.append(round(v, 4))
            v += step
        return out or [1.0]
    return [float(p) for p in re.split(r"[,\s]+", text) if p]


def bbox_union(boxes) -> tuple[int, int, int, int] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)
