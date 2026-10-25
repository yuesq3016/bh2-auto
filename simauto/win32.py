"""Thin ctypes layer over the Win32 APIs used by simauto.

Everything here is deliberately low level: no third-party packages, no
framework.  Higher modules (``scanner``, ``inputs``, ``window``) build on top.

Two things are set up once, lazily:

* DPI awareness (per-monitor v2).  After this, all window rectangles and
  client sizes we read are in *physical* pixels, which is what GDI capture and
  SendInput both expect.  Without it a 150 % scaled display silently gives
  half-size screenshots unless you divide by the scale factor everywhere.
* The ``BITMAPINFOHEADER`` / ``INPUT`` structures and the API prototypes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------

_DPI_AWARENESS_CONTEXTS = {
    "unaware": 0,           # DPI_AWARENESS_CONTEXT_UNAWARE
    "system": -1,           # SYSTEM_AWARE
    "per_monitor": -2,      # PER_MONITOR_AWARE
    "per_monitor_v2": -4,   # PER_MONITOR_AWARE_V2
}

_dpi_done = False


def enable_dpi_awareness(mode: str = "per_monitor_v2") -> bool:
    """Make this process DPI aware.  Returns True if the call succeeded.

    Safe to call repeatedly; later calls with a different mode are ignored
    because Windows only honours the first successful one.
    """
    global _dpi_done
    if _dpi_done:
        return True
    try:
        ctx = _DPI_AWARENESS_CONTEXTS[mode]
        # SetProcessDpiAwarenessContext needs ctypes.c_void_p(negative) to
        # survive the int -> HANDLE conversion.
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(ctx)):
            _dpi_done = True
            return True
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            _dpi_done = True
            return True
    except (AttributeError, OSError):
        pass
    return False


def get_dpi_for_window(hwnd: int) -> int:
    """Effective DPI of the monitor a window is on (96 == 100 %)."""
    try:
        return int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
    except (AttributeError, OSError):
        return 96


def SetLastErrorSafe(value: int = 0) -> None:
    """Reset the thread's last-error so a following failure is unambiguous."""
    ctypes.set_last_error(value)


# --------------------------------------------------------------------------
# Basic window helpers
# --------------------------------------------------------------------------

def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(wintypes.HWND(hwnd)))


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))


def is_iconic(hwnd: int) -> bool:
    """True when the window is minimised.

    A minimised window is usually -32000,-32000 and GDI capture returns junk,
    so callers should warn instead of silently matching nothing.
    """
    return bool(user32.IsIconic(wintypes.HWND(hwnd)))


def get_window_text(hwnd: int) -> str:
    """Window title.  Note the generous buffer: some Qt windows report a very
    large length and a short buffer would truncate (or, worse, overflow)."""
    try:
        length = int(user32.GetWindowTextLengthW(wintypes.HWND(hwnd)))
    except OSError:
        return ""
    size = min(max(length + 1, 512), 8192)
    buf = ctypes.create_unicode_buffer(size)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, size)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def get_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) in screen coordinates."""
    r = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (r.left, r.top, r.right, r.bottom)


# DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS) reports the *visible*
# frame, which is what you need for screen capture: GetWindowRect includes the
# invisible resize border on Windows 10/11.
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def get_frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame (left, top, right, bottom); falls back to GetWindowRect."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        return get_window_rect(hwnd)
    rect = wintypes.RECT()
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect), ctypes.sizeof(rect),
        )
    except (AttributeError, OSError):
        return get_window_rect(hwnd)
    if hr != 0 or rect.right <= rect.left or rect.bottom <= rect.top:
        return get_window_rect(hwnd)
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_client_origin_on_screen(hwnd: int) -> tuple[int, int]:
    """Screen position of the client area's (0, 0) corner."""
    return client_to_screen(hwnd, 0, 0)


def grab_desktop_region(x: int, y: int, w: int, h: int):
    """Copy a rectangle of the *desktop* into an (H, W, 3) uint8 RGB array.

    Used as ground truth for "did I really capture the window, or did I capture
    whatever happens to be sitting on top of it?".  numpy is imported lazily so
    this module stays importable without it.
    """
    import numpy as np

    hdc_screen = user32.GetDC(user32.GetDesktopWindow())
    if not hdc_screen:
        raise OSError("GetDC(desktop) failed")
    mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, int(w), int(h))
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not gdi32.BitBlt(mem, 0, 0, int(w), int(h), hdc_screen, int(x), int(y),
                            SRCCOPY | CAPTUREBLT):
            raise OSError("BitBlt from the desktop failed")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = int(w)
        bmi.bmiHeader.biHeight = -int(h)   # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = np.empty((int(h), int(w) * 4), dtype=np.uint8)
        got = gdi32.GetDIBits(mem, bmp, 0, int(h),
                              buf.ctypes.data_as(ctypes.c_void_p),
                              ctypes.byref(bmi), DIB_RGB_COLORS)
        if got == 0:
            raise OSError("GetDIBits copied no scanlines")
        return np.ascontiguousarray(buf.reshape(int(h), int(w), 4)[:, :, 2::-1])
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(user32.GetDesktopWindow(), hdc_screen)


def get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the client area, in client coordinates.

    ``left``/``top`` are normally 0; they can be non-zero for some windows.
    """
    r = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (r.left, r.top, r.right, r.bottom)


def get_client_size(hwnd: int) -> tuple[int, int]:
    left, top, right, bottom = get_client_rect(hwnd)
    return (right - left, bottom - top)


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    """Map a point inside the client area to absolute screen pixels."""
    pt = wintypes.POINT(int(x), int(y))
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (pt.x, pt.y)


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = wintypes.POINT(int(x), int(y))
    if not user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (pt.x, pt.y)


def get_foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_from_point(x: int, y: int) -> int:
    """Topmost window (not root) containing a screen point."""
    try:
        return int(user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)
    except OSError:
        return 0


def set_foreground_window(hwnd: int) -> bool:
    """Best-effort activation.

    Windows refuses SetForegroundWindow from a process that is not already
    foreground, so this may legitimately fail; callers treat it as a hint.
    """
    return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))


# --------------------------------------------------------------------------
# Enumeration callbacks
# --------------------------------------------------------------------------

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def enum_windows() -> list[int]:
    """All top-level windows, in Z order."""
    out: list[int] = []

    def _cb(hwnd, _lparam):
        out.append(int(hwnd))
        return True

    if not user32.EnumWindows(WNDENUMPROC(_cb), 0):
        err = ctypes.get_last_error()
        if err:
            raise ctypes.WinError(err)
    return out


def enum_child_windows(hwnd: int) -> list[int]:
    out: list[int] = []

    def _cb(child, _lparam):
        out.append(int(child))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), WNDENUMPROC(_cb), 0)
    return out


def get_root_window(hwnd: int) -> int:
    return int(user32.GetAncestor(wintypes.HWND(hwnd), 2) or 0)  # GA_ROOT


# --------------------------------------------------------------------------
# GDI structures
# --------------------------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


DIB_RGB_COLORS = 0
BI_RGB = 0
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000

# PrintWindow flags
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

# RedrawWindow flags (nudge a window into repainting before PrintWindow)
RDW_INVALIDATE = 0x0001
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100

# ---------------------------------------------------------------------------
# Prototypes.
#
# Every handle parameter MUST have an argtype.  Without one ctypes assumes a
# 32-bit int and a 64-bit HDC/HWND raises "int too long to convert" — the
# single most common way a hand-rolled ctypes GDI layer fails.  Declaring
# restype as c_void_p keeps 64-bit handles intact on the way back too.
# ---------------------------------------------------------------------------

_HWND = wintypes.HWND
# NOTE: wintypes.HDC is *not* pointer-sized in this environment (it resolves to
# a 32-bit c_int), so declaring it as an argtype silently truncates a 64-bit
# GDI handle and every call fails with a nonsense error.  Use c_void_p for all
# GDI handles.
_HDC = ctypes.c_void_p
_HBITMAP = ctypes.c_void_p
_HGDIOBJ = ctypes.c_void_p
_HMENU = ctypes.c_void_p

user32.GetDC.argtypes = [_HWND]
user32.GetDC.restype = _HDC
user32.GetWindowDC.argtypes = [_HWND]
user32.GetWindowDC.restype = _HDC
user32.ReleaseDC.argtypes = [_HWND, _HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetDesktopWindow.restype = _HWND
user32.PrintWindow.argtypes = [_HWND, _HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.RedrawWindow.argtypes = [_HWND, ctypes.c_void_p, _HMENU, wintypes.UINT]
user32.RedrawWindow.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = _HWND
user32.GetClientRect.argtypes = [_HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [_HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [_HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [_HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [_HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [_HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [_HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [_HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = [_HWND]
user32.IsWindowVisible.argtypes = [_HWND]
user32.IsIconic.argtypes = [_HWND]
user32.GetForegroundWindow.restype = _HWND
user32.SetForegroundWindow.argtypes = [_HWND]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = _HWND
user32.GetAncestor.argtypes = [_HWND, wintypes.UINT]
user32.GetAncestor.restype = _HWND
user32.ShowWindow.argtypes = [_HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    _HWND, _HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [_HDC]
gdi32.CreateCompatibleDC.restype = _HDC
gdi32.DeleteDC.argtypes = [_HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateCompatibleBitmap.argtypes = [_HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = _HBITMAP
gdi32.SelectObject.argtypes = [_HDC, _HGDIOBJ]
gdi32.SelectObject.restype = _HGDIOBJ
gdi32.DeleteObject.argtypes = [_HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    _HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    _HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    _HDC, _HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDeviceCaps.argtypes = [_HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int


# --------------------------------------------------------------------------
# Window messages
# --------------------------------------------------------------------------

WM_CLOSE = 0x0010
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_ACTIVATE = 0x0006
WM_NCHITTEST = 0x0084
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM


def make_lparam(x: int, y: int) -> int:
    """Pack client-relative coordinates into a mouse message LPARAM."""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


# --------------------------------------------------------------------------
# SendInput (hardware-level synthetic input)
# --------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short


def send_inputs(inputs: list[INPUT]) -> int:
    """Push an array of INPUT events.  Returns how many were accepted."""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = int(user32.SendInput(n, arr, ctypes.sizeof(INPUT)))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def key_name_to_vk(name: str) -> int:
    """Map a friendly key name to a virtual-key code.

    Accepts single characters ("a", "1"), virtual key names ("VK_SPACE",
    "F1", "LEFT") and a few aliases.
    """
    n = name.strip()
    if not n:
        raise ValueError("empty key name")
    upper = n.upper()
    if upper.startswith("VK_"):
        upper = upper[3:]
    aliases = {
        "ESC": "ESCAPE",
        "RETURN": "RETURN",
        "ENTER": "RETURN",
        "SPACE": "SPACE",
        "SPACEBAR": "SPACE",
        "CTRL": "CONTROL",
        "ALT": "MENU",
        "DEL": "DELETE",
        "INS": "INSERT",
        "PGUP": "PRIOR",
        "PGDN": "NEXT",
        "UP": "UP",
        "DOWN": "DOWN",
        "LEFT": "LEFT",
        "RIGHT": "RIGHT",
        "TAB": "TAB",
        "SHIFT": "SHIFT",
        "BACKSPACE": "BACK",
        "BACK": "BACK",
    }
    upper = aliases.get(upper, upper)
    vk = getattr(_VK, upper, None)
    if vk is not None:
        return int(vk)
    if len(n) == 1:
        code = int(user32.VkKeyScanW(n[0])) & 0xFF
        if code:
            return code
        return ord(n.upper())
    raise ValueError(f"unknown key name: {name!r}")


class _VK:
    """The subset of virtual-key codes worth naming."""
    BACK = 0x08
    TAB = 0x09
    RETURN = 0x0D
    SHIFT = 0x10
    CONTROL = 0x11
    MENU = 0x12
    PAUSE = 0x13
    CAPITAL = 0x14
    ESCAPE = 0x1B
    SPACE = 0x20
    PRIOR = 0x21
    NEXT = 0x22
    END = 0x23
    HOME = 0x24
    LEFT = 0x25
    UP = 0x26
    RIGHT = 0x27
    DOWN = 0x28
    SNAPSHOT = 0x2C
    INSERT = 0x2D
    DELETE = 0x2E
    OEM_1 = 0xBA
    OEM_PLUS = 0xBB
    OEM_COMMA = 0xBC
    OEM_MINUS = 0xBD
    OEM_PERIOD = 0xBE
    OEM_2 = 0xBF
    OEM_3 = 0xC0
    OEM_4 = 0xDB
    OEM_5 = 0xDC
    OEM_6 = 0xDD
    OEM_7 = 0xDE
    OEM_8 = 0xDF
    NUMPAD0 = 0x60
    NUMPAD1 = 0x61
    NUMPAD2 = 0x62
    NUMPAD3 = 0x63
    NUMPAD4 = 0x64
    NUMPAD5 = 0x65
    NUMPAD6 = 0x66
    NUMPAD7 = 0x67
    NUMPAD8 = 0x68
    NUMPAD9 = 0x69
    MULTIPLY = 0x6A
    ADD = 0x6B
    SUBTRACT = 0x6D
    DECIMAL = 0x6E
    DIVIDE = 0x6F
    F1 = 0x70
    F2 = 0x71
    F3 = 0x72
    F4 = 0x73
    F5 = 0x74
    F6 = 0x75
    F7 = 0x76
    F8 = 0x77
    F9 = 0x78
    F10 = 0x79
    F11 = 0x7A
    F12 = 0x7B


_VK = _VK()
