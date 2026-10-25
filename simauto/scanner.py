"""Find the emulator window and grab frames from it with GDI.

Why GDI and not ADB?  ADB screencap is the most reliable path on Android
emulators, but it needs a working adb and a device the game does not detect as
hooked.  GDI needs nothing: it is the same window surface Windows itself
composites, and it keeps the whole program to plain Python.

The honest caveat: GDI can only copy what the target window has drawn.  Some
GPU-composited emulator windows answer PrintWindow with a black or stale image.
``probe_window`` captures every candidate handle with every backend and reports
which combination actually produced pixels, so you find out in one command
instead of guessing.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass

import numpy as np

from . import win32 as w32
from .imaging import ImageError, save_image

__all__ = [
    "WindowInfo", "list_windows", "find_windows", "find_window",
    "resolve_window", "WindowCapture", "probe_window", "PROBE_MODES",
]

# The GDI strategies, tried in this order by the probe.
PROBE_MODES = (
    "printwindow_fullcontent",
    "bitblt_client",
    "screendc",
    "printwindow_plain",
)

_MODE_NOTES = {
    "printwindow_fullcontent": "PrintWindow(PW_RENDERFULLCONTENT) on the window DC",
    "bitblt_client": "BitBlt from the window DC (whatever is composited)",
    "screendc": "BitBlt from the desktop DC at the window's screen position",
    "printwindow_plain": "PrintWindow(0) — the classic call, often black on D3D",
}

# Modes that read the screen rather than the window: they need the window
# unobstructed (nothing on top of it).
SCREEN_BASED_MODES = ("screendc",)


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    visible: bool
    iconic: bool

    @property
    def width(self) -> int:
        return self.client_size[0]

    @property
    def height(self) -> int:
        return self.client_size[1]

    @property
    def process_name(self) -> str:
        return _process_name(self.pid)

    def describe(self) -> str:
        flags = []
        if not self.visible:
            flags.append("hidden")
        if self.iconic:
            flags.append("minimised")
        suffix = f" [{','.join(flags)}]" if flags else ""
        return (
            f"hwnd=0x{self.hwnd:08X} pid={self.pid} "
            f"{self.client_size[0]}x{self.client_size[1]} "
            f"class={self.class_name!r} title={self.title!r}{suffix}"
        )


def _process_name(pid: int) -> str:
    """Process image name for a pid, best effort (stdlib only)."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _info(hwnd: int) -> WindowInfo:
    try:
        rect = w32.get_window_rect(hwnd)
    except OSError:
        rect = (0, 0, 0, 0)
    try:
        client = w32.get_client_size(hwnd)
    except OSError:
        client = (0, 0)
    return WindowInfo(
        hwnd=int(hwnd),
        title=w32.get_window_text(hwnd),
        class_name=w32.get_class_name(hwnd),
        pid=w32.get_process_id(hwnd),
        rect=rect,
        client_size=client,
        visible=w32.is_visible(hwnd),
        iconic=w32.is_iconic(hwnd),
    )


def list_windows(
    *,
    visible_only: bool = True,
    min_size: int = 0,
    titles_only: bool = False,
) -> list[WindowInfo]:
    """Enumerate candidate windows (top level), largest client area first."""
    out: list[WindowInfo] = []
    for hwnd in w32.enum_windows():
        info = _info(hwnd)
        if visible_only and (not info.visible or info.iconic):
            continue
        if titles_only and not info.title:
            continue
        if info.client_size[0] < min_size or info.client_size[1] < min_size:
            continue
        out.append(info)
    out.sort(key=lambda i: i.client_size[0] * i.client_size[1], reverse=True)
    return out


def find_windows(
    pattern: str | None = None,
    *,
    class_pattern: str | None = None,
    regex: bool = False,
    visible_only: bool = True,
    titles_only: bool = False,
) -> list[WindowInfo]:
    """Filter windows by title and/or class substring (or regex).

    Both patterns are matched with ``re.search`` semantics when ``regex`` is
    set, so ``regex=False`` means a plain case-insensitive substring test.
    """
    import re

    def _match(value: str, pat: str | None) -> bool:
        if pat is None:
            return True
        if regex:
            return re.search(pat, value, re.IGNORECASE) is not None
        return pat.lower() in value.lower()

    hits = []
    for info in list_windows(visible_only=visible_only, titles_only=titles_only):
        if _match(info.title, pattern) and _match(info.class_name, class_pattern):
            hits.append(info)
    return hits


def find_window(pattern: str, **kwargs) -> WindowInfo | None:
    """The single best (largest) window matching ``pattern``."""
    hits = find_windows(pattern, **kwargs)
    return hits[0] if hits else None


def resolve_window(spec: dict | None) -> WindowInfo:
    """Turn a config ``window:`` block into a concrete window.

    ``spec`` keys: ``title``, ``class``, ``regex``, ``hwnd``, ``index``.
    Raises ``LookupError`` with a helpful message when nothing matches.
    """
    spec = dict(spec or {})
    hwnd = spec.get("hwnd")
    if hwnd:
        hwnd = int(hwnd, 0) if isinstance(hwnd, str) else int(hwnd)
        if not w32.is_window(hwnd):
            raise LookupError(f"hwnd 0x{hwnd:X} is not a valid window")
        return _info(hwnd)

    title = spec.get("title")
    klass = spec.get("class") or spec.get("class_name")
    if not title and not klass:
        raise LookupError("window spec needs one of: hwnd, title, class")
    visible_only = bool(spec.get("visible_only", True))
    hits = find_windows(
        title,
        class_pattern=klass,
        regex=bool(spec.get("regex", False)),
        visible_only=visible_only,
    )
    if not hits and visible_only:
        # Emulators keep their render window around while pseudo-minimised, in
        # which case the window exists but is flagged hidden.  Silently finding
        # nothing there is a confusing dead end ("no window matched" while the
        # emulator is plainly running), so fall back to hidden windows and say
        # so in the error if that is what happened.
        hidden = find_windows(
            title,
            class_pattern=klass,
            regex=bool(spec.get("regex", False)),
            visible_only=False,
        )
        if hidden:
            raise LookupError(
                f"window(s) matching title={title!r} class={klass!r} exist but are "
                f"HIDDEN (pseudo-minimised / sent to tray): "
                + "; ".join(f"0x{i.hwnd:08X} {i.client_size[0]}x{i.client_size[1]}"
                            for i in hidden[:3])
                + ". Restore the emulator window and retry, or pass hwnd=<handle> "
                  "explicitly if your capture method works on hidden windows."
            )
    if not hits:
        raise LookupError(
            f"no window matched title={title!r} class={klass!r}; "
            f"run `simauto list --all` to see what is open"
        )
    index = int(spec.get("index", 0))
    if index >= len(hits):
        raise LookupError(f"index {index} out of range ({len(hits)} matches)")
    return hits[index]


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

class WindowCapture:
    """Reusable GDI capture session for one window handle.

    Use it as a context manager::

        with WindowCapture(hwnd) as cap:
            frame = cap.grab()          # (H, W, 3) uint8 RGB

    The memory DC + bitmap are created once and reused, which matters at
    10 fps: allocating a 1920x1080 DIB per frame is what makes naive
    scripts stutter.
    """

    def __init__(self, hwnd: int, mode: str = "printwindow_fullcontent"):
        if mode not in PROBE_MODES:
            raise ValueError(f"unknown capture mode {mode!r}; expected one of {PROBE_MODES}")
        self.hwnd = int(hwnd)
        self.mode = mode
        self._hdc_window = None
        self._hmem = None
        self._hbmp = None
        self._old = None
        self._size = (0, 0)
        self._buf = None
        self.last_error: str | None = None
        self.frames = 0
        self.failures = 0
        self.last_capture_ms = 0.0

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "WindowCapture":
        w, h = w32.get_client_size(self.hwnd)
        if w <= 0 or h <= 0:
            raise ImageError(
                f"window 0x{self.hwnd:X} has an empty client area ({w}x{h}); "
                f"is it minimised?"
            )
        # GetWindowDC rather than GetDC: GetWindowDC is granted across process
        # boundaries for windows we do not own, which GetDC is not (it fails
        # with ERROR_ACCESS_DENIED on emulator render windows).
        w32.SetLastErrorSafe(0)
        self._hdc_window = w32.user32.GetWindowDC(self.hwnd)
        if not self._hdc_window:
            err = ctypes.get_last_error()
            raise ImageError(
                f"GetWindowDC failed for hwnd 0x{self.hwnd:X}"
                + (f" (WinError {err})" if err else "")
                + "; run this script at the same privilege level as the emulator"
            )
        self._hmem = w32.gdi32.CreateCompatibleDC(self._hdc_window)
        if not self._hmem:
            raise ctypes.WinError(ctypes.get_last_error())
        self._resize_buffers(w, h)
        return self

    def close(self) -> None:
        try:
            if self._hmem and self._old:
                w32.gdi32.SelectObject(self._hmem, self._old)
            if self._hbmp:
                w32.gdi32.DeleteObject(self._hbmp)
            if self._hmem:
                w32.gdi32.DeleteDC(self._hmem)
            if self._hdc_window:
                w32.user32.ReleaseDC(self.hwnd, self._hdc_window)
        finally:
            self._hdc_window = self._hmem = self._hbmp = self._old = None
            self._buf = None

    def __enter__(self) -> "WindowCapture":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _resize_buffers(self, w: int, h: int) -> None:
        if self._hbmp:
            w32.gdi32.SelectObject(self._hmem, self._old)
            w32.gdi32.DeleteObject(self._hbmp)
            self._old = None
        self._hbmp = w32.gdi32.CreateCompatibleBitmap(self._hdc_window, w, h)
        if not self._hbmp:
            err = ctypes.get_last_error()
            raise ImageError(
                f"CreateCompatibleBitmap failed for {w}x{h}"
                + (f" (WinError {err})" if err else "")
            )
        self._old = w32.gdi32.SelectObject(self._hmem, self._hbmp)
        # GetDIBits with biBitCount=32 writes 4 bytes per pixel, so the buffer
        # must be BGRA-sized even though we only keep 3 channels.
        self._buf = np.empty((h, w * 4), dtype=np.uint8)
        self._size = (w, h)

    # -- grabbing ----------------------------------------------------------

    def grab(self) -> np.ndarray:
        """Capture one frame as an (H, W, 3) uint8 RGB array."""
        t0 = time.perf_counter()
        w, h = w32.get_client_size(self.hwnd)
        if (w, h) != self._size:
            self._resize_buffers(w, h)

        ok = self._blit(w, h)
        if not ok:
            self.failures += 1
            self.last_error = f"{self.mode} returned failure"
            raise ImageError(self.last_error)

        raw = self._read_bits(w, h)
        # _buf is (H, W*4); each pixel is BGRA little-endian, so dropping the
        # alpha byte and reversing the first three gives RGB.
        bgra = raw.reshape(h, w, 4)
        frame = np.ascontiguousarray(bgra[:, :, 2::-1])
        self.frames += 1
        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
        return frame

    def _blit(self, w: int, h: int) -> bool:
        if self.mode == "bitblt_client":
            # SRCCOPY | CAPTUREBLT keeps layered windows in the copy.
            return bool(
                w32.gdi32.BitBlt(
                    self._hmem, 0, 0, w, h,
                    self._hdc_window, 0, 0,
                    w32.SRCCOPY | w32.CAPTUREBLT,
                )
            )
        if self.mode == "screendc":
            return self._blit_screen(w, h)
        if self.mode == "printwindow_fullcontent":
            return bool(
                w32.user32.PrintWindow(self.hwnd, self._hmem, w32.PW_RENDERFULLCONTENT)
            )
        if self.mode == "printwindow_plain":
            return bool(w32.user32.PrintWindow(self.hwnd, self._hmem, 0))
        raise ValueError(self.mode)

    def _blit_screen(self, w: int, h: int) -> bool:
        """Copy the desktop pixels where the window's *client area* sits.

        This is the fallback that always works, because it reads the same
        surface the user sees.  The price: the window must be visible and not
        covered by anything, and it cannot be captured in the background.

        Note the offset: the window DC origin is the window's top-left corner
        (frame included), but our buffer is the client area, so we must start
        from the client origin or the image is shifted by the title bar.
        """
        screen_x, screen_y = w32.client_to_screen(self.hwnd, 0, 0)
        hdc_screen = w32.user32.GetDC(w32.user32.GetDesktopWindow())
        if not hdc_screen:
            return False
        try:
            return bool(
                w32.gdi32.BitBlt(
                    self._hmem, 0, 0, w, h,
                    hdc_screen, int(screen_x), int(screen_y),
                    w32.SRCCOPY | w32.CAPTUREBLT,
                )
            )
        finally:
            w32.user32.ReleaseDC(w32.user32.GetDesktopWindow(), hdc_screen)

    def _read_bits(self, w: int, h: int) -> np.ndarray:
        bmi = w32.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        # Negative height requests a top-down DIB, so row 0 is the top row and
        # we never have to flip the buffer.
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = w32.BI_RGB
        got = w32.gdi32.GetDIBits(
            self._hmem, self._hbmp, 0, h,
            self._buf.ctypes.data_as(ctypes.c_void_p),
            ctypes.byref(bmi), w32.DIB_RGB_COLORS,
        )
        if got == 0:
            # GDI often leaves last-error at 0 here, so do not dress this up as
            # a WinError — the return value is the real signal.
            raise ImageError(
                f"GetDIBits copied no scanlines ({w}x{h}, 32bpp top-down DIB)"
            )
        return self._buf

    # -- convenience -------------------------------------------------------

    def save_frame(self, path: str, frame: np.ndarray | None = None) -> str:
        if frame is None:
            frame = self.grab()
        return save_image(path, frame)

    def grab_roi(self, roi) -> np.ndarray:
        from .imaging import crop_roi
        return crop_roi(self.grab(), roi)


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def _frame_stats(frame: np.ndarray) -> dict:
    """Cheap 'is this a real picture?' statistics."""
    gray = frame.astype(np.float32).mean(axis=2)
    dark = float((gray < 12).mean())
    # Mean absolute difference between neighbouring columns: a black or frozen
    # surface has almost none.
    if frame.shape[1] > 2:
        detail = float(np.abs(np.diff(gray, axis=1)).mean())
    else:
        detail = 0.0
    return {
        "mean": round(float(gray.mean()), 2),
        "stdev": round(float(gray.std()), 2),
        "dark_ratio": round(dark, 4),
        "detail": round(detail, 3),
        "nonblack_ratio": round(float((gray >= 12).mean()), 4),
    }


def probe_window(hwnd: int, out_dir: str | None = None, modes=PROBE_MODES) -> list[dict]:
    """Try every capture backend on ``hwnd`` and report what came back.

    Returns one dict per mode with the frame statistics and, when ``out_dir``
    is given, the path of the saved sample frame.  A mode whose frame is
    essentially uniform black (``nonblack_ratio`` near 0) is marked unusable —
    that is the classic PrintWindow-on-D3D symptom.
    """
    results = []
    for mode in modes:
        entry = {"mode": mode, "note": _MODE_NOTES.get(mode, ""), "ok": False}
        try:
            with WindowCapture(hwnd, mode=mode) as cap:
                t0 = time.perf_counter()
                frame = cap.grab()
                entry["capture_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                entry["size"] = [int(frame.shape[1]), int(frame.shape[0])]
                entry["stats"] = _frame_stats(frame)
                entry["ok"] = True
                if out_dir:
                    path = os.path.join(out_dir, f"probe_{mode}.png")
                    save_image(path, frame)
                    entry["sample"] = path
        except Exception as exc:  # noqa: BLE001 - the point is to report anything
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)

    usable = [r for r in results if r.get("ok") and r["stats"]["nonblack_ratio"] > 0.2]
    for r in results:
        r["likely_usable"] = bool(r in usable)
    if not usable:
        for r in results:
            r.setdefault("warning", "no backend returned a non-black frame")
    return results


def pick_best_mode(hwnd: int) -> str | None:
    """Probe once and return the first backend that produced real pixels."""
    for entry in probe_window(hwnd):
        if entry.get("likely_usable"):
            return entry["mode"]
    return None
