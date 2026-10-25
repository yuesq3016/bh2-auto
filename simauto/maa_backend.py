"""Optional MaaFramework backend.

MaaFramework (LGPL-3.0) ships a Win32 controller that attaches to a desktop
window by handle and does its own screencap/input, including methods that work
on GPU-rendered emulator windows where plain GDI returns black:

    GDI, FramePool (Win10 1903+, background), DXGI_DesktopDup,
    DXGI_DesktopDup_Window, PrintWindow (background), ScreenDC

Its screencap argument is a **bitmask**: you pass every method you are willing
to accept and MaaFramework probes them, then uses the fastest one that works.
That is exactly the problem this project hit with MuMu 12, so when ``maafw`` is
importable we prefer it and fall back to the built-in GDI backend otherwise.

Install the binding with::

    python -m pip install maafw        # import name is `maa`

Nothing in this module is imported at package import time; a missing ``maa``
package is reported as :class:`MaaBackendUnavailable` instead of crashing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import win32 as w32
from .imaging import ImageError

__all__ = [
    "MaaBackendUnavailable", "MaaBackend", "maa_available",
    "SCREENCAP_METHODS", "INPUT_METHODS", "DEFAULT_SCREENCAP", "DEFAULT_INPUT",
    "resolve_screencap_method", "resolve_input_method",
]

# Human-readable names -> MaaWin32ScreencapMethodEnum member names, so configs
# stay readable while the attribute lookup stays exact.
SCREENCAP_METHODS: dict[str, str] = {
    "gdi": "GDI",
    "framepool": "FramePool",
    "frame_pool": "FramePool",
    "dxgi_desktopdup": "DXGI_DesktopDup",
    "desktopdup": "DXGI_DesktopDup",
    "dxgi_desktopdup_window": "DXGI_DesktopDup_Window",
    "desktopdup_window": "DXGI_DesktopDup_Window",
    "printwindow": "PrintWindow",
    "print_window": "PrintWindow",
    "screendc": "ScreenDC",
    "screen_dc": "ScreenDC",
    "foreground": "Foreground",
    "background": "Background",
    "all": "All",
}

INPUT_METHODS: dict[str, str] = {
    "seize": "Seize",
    "sendmessage": "SendMessage",
    "send_message": "SendMessage",
    "postmessage": "PostMessage",
    "post_message": "PostMessage",
    "legacyevent": "LegacyEvent",
    "sendmessagewithcursorpos": "SendMessageWithCursorPos",
    "postmessagewithcursorpos": "PostMessageWithCursorPos",
    "sendmessagewithwindowpos": "SendMessageWithWindowPos",
    "postmessagewithwindowpos": "PostMessageWithWindowPos",
    "interception": "Interception",
    "anchoredtouch": "AnchoredTouch",
    "anchored_touch": "AnchoredTouch",
}

# FramePool + PrintWindow are the two backends that work on a background
# window, which is what a bot wants; the foreground-only methods are added as
# fallbacks because MaaFramework picks the fastest *working* one anyway.
DEFAULT_SCREENCAP = "background,framepool,printwindow,gdi,screendc"
DEFAULT_INPUT = "postmessage"

# Labels whose method keeps working while the window is covered.  Anything else
# reads the screen and therefore needs the window visible and unobstructed.
BACKGROUND_SCREENCAP_LABELS = frozenset({"Background", "FramePool", "PrintWindow"})


class MaaBackendUnavailable(RuntimeError):
    """``maafw`` is not installed / could not be imported."""


def _import_maa():
    try:
        from maa.controller import (  # type: ignore
            Win32Controller,
            MaaWin32ScreencapMethodEnum,
            MaaWin32InputMethodEnum,
        )
        from maa.toolkit import Toolkit  # type: ignore
        from maa.tasker import Tasker as _Tasker  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise MaaBackendUnavailable(
            "the 'maafw' package is not installed in this Python environment "
            "(pip install maafw, or run tools/setup-maafw.ps1). "
            "Falling back to the built-in GDI backend."
        ) from exc
    # Applied once, on first successful import: the binding logs to stderr on
    # its own and would otherwise drown the CLI output.
    global _maa_env_inited
    if not _maa_env_inited:
        _maa_env_inited = True
        init_maa_environment()
    return Win32Controller, MaaWin32ScreencapMethodEnum, MaaWin32InputMethodEnum, Toolkit


def init_maa_environment(option_dir: str = "./.maa", level: str = "Off") -> bool:
    """Initialise MaaFramework's global options and silence its logging.

    MaaFramework writes one line per *failed capture attempt* to stderr, and the
    fallback chain deliberately attempts several methods before one works, so
    the program's own output is buried under dozens of identical lines.  Those
    failures are already reported usefully by the chain itself (``tried
    Background: RuntimeError`` ...), so the default here is ``Off``; pass
    ``level="Error"`` if you want the framework's own view as well.

    ``option_dir`` also decides where the framework drops its own files
    (``maa_option.json`` and a debug log), so it points at ``.maa/`` instead of
    scattering ``config/`` and ``debug/`` into the project root.

    Returns True when the option was applied.  Never raises: this is cosmetic.
    """
    try:
        from maa.toolkit import Toolkit  # type: ignore
    except ImportError:
        return False

    try:
        os.makedirs(option_dir, exist_ok=True)
    except OSError:
        option_dir = "./"

    try:
        Toolkit.init_option(option_dir)
    except Exception:  # noqa: BLE001
        pass

    # The supported entry point is Tasker.set_stdout_level (a static method on
    # the Tasker class).  Calling MaaSetGlobalOption directly is deprecated and
    # gets ignored, which is how the per-capture spam survives.
    try:
        from maa.tasker import Tasker  # type: ignore
        from maa.define import LoggingLevelEnum  # type: ignore
        return bool(
            Tasker.set_stdout_level(getattr(LoggingLevelEnum, level, LoggingLevelEnum.Off))
        )
    except Exception:  # noqa: BLE001
        pass

    # Fall back to the raw call for bindings that do not expose the helper.
    try:
        import ctypes
        from maa.define import MaaGlobalOptionEnum, LoggingLevelEnum  # type: ignore
        from maa.library import Library  # type: ignore
        value = ctypes.c_int32(int(getattr(LoggingLevelEnum, level, LoggingLevelEnum.Off)))
        return bool(
            Library.framework().MaaSetGlobalOption(
                int(MaaGlobalOptionEnum.StdoutLevel),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
        )
    except Exception:  # noqa: BLE001
        return False


_maa_env_inited = False


def maa_available() -> bool:
    try:
        _import_maa()
        return True
    except MaaBackendUnavailable:
        return False


def parse_screencap_tokens(spec) -> list[str]:
    """Split a screencap spec into MaaFramework enum member names.

    Validation happens against the local name table, so a typo is reported as
    a ValueError even when ``maafw`` is not installed (which is when a config
    typo is most annoying to debug).
    """
    if spec is None or str(spec).strip() == "":
        spec = DEFAULT_SCREENCAP
    members: list[str] = []
    for token in str(spec).replace("|", ",").split(","):
        key = token.strip().lower().replace("-", "_").replace(" ", "")
        if not key:
            continue
        member = SCREENCAP_METHODS.get(key)
        if member is None:
            # Allow the exact enum member name (e.g. "FramePool").
            if key in {v.lower() for v in SCREENCAP_METHODS.values()}:
                member = next(v for v in SCREENCAP_METHODS.values() if v.lower() == key)
            else:
                raise ValueError(
                    f"unknown screencap method {token.strip()!r}; known: "
                    f"{', '.join(sorted(set(SCREENCAP_METHODS)))}"
                )
        if member not in members:
            members.append(member)
    return members or ["Background"]


def parse_input_token(spec) -> str:
    key = str(spec if spec is not None else DEFAULT_INPUT)
    key = key.strip().lower().replace("-", "_").replace(" ", "")
    member = INPUT_METHODS.get(key)
    if member is None:
        if key in {v.lower() for v in INPUT_METHODS.values()}:
            member = next(v for v in INPUT_METHODS.values() if v.lower() == key)
        else:
            raise ValueError(
                f"unknown input method {spec!r}; known: "
                f"{', '.join(sorted(set(INPUT_METHODS)))}"
            )
    return member


def resolve_screencap_method(spec) -> int:
    """Turn a config value into the screencap bitmask.

    Accepts an int (used verbatim), or a name / comma or ``|`` separated list
    such as ``"background"``, ``"framepool,printwindow"``, ``"FramePool|GDI"``.
    """
    if isinstance(spec, int):
        return int(spec)
    members = parse_screencap_tokens(spec)
    _, ScreencapEnum, _, _ = _import_maa()
    total = 0
    for member in members:
        total |= int(getattr(ScreencapEnum, member))
    return total or int(ScreencapEnum.Background)


# Order tried when the configured mask cannot be satisfied.  Screen-based
# methods come before GDI on purpose: GDI "succeeds" on a GPU-composited window
# and hands back an all-black frame, which is the worst failure mode because it
# looks like success.  Background methods still come first because they are the
# only ones that keep working when the window is covered.
_FALLBACK_ORDER = (
    "Background",            # FramePool | PrintWindow - the only background pair
    "FramePool",
    "PrintWindow",
    "ScreenDC",              # reads the screen; always works, needs the window visible
    "Foreground",            # DXGI_DesktopDup_Window | ScreenDC
    "DXGI_DesktopDup_Window",
    "GDI",
)


def screencap_fallback_chain(spec) -> list[tuple[str, int]]:
    """Ordered (name, mask) candidates starting with the configured one.

    MaaFramework refuses to attach at all when *no* method in the mask works,
    so a config that insists on ``background`` fails outright on a window that
    only supports screen reading.  Trying the alternatives in turn turns a hard
    failure into a (logged) downgrade.
    """
    _, ScreencapEnum, _, _ = _import_maa()
    first = parse_screencap_tokens(spec)
    chain: list[tuple[str, int]] = []
    for name in list(first) + [n for n in _FALLBACK_ORDER if n not in first]:
        try:
            mask = int(getattr(ScreencapEnum, name))
        except AttributeError:
            continue
        if not any(mask == existing for _, existing in chain):
            chain.append((name, mask))
    return chain


def resolve_input_method(spec) -> int:
    """Turn a config value into one input method (MaaFramework takes no OR here)."""
    if isinstance(spec, int):
        return int(spec)
    member = parse_input_token(spec)
    _, _, InputEnum, _ = _import_maa()
    return int(getattr(InputEnum, member))


def describe_screencap_mask(mask: int) -> str:
    try:
        _, ScreencapEnum, _, _ = _import_maa()
    except MaaBackendUnavailable:
        return f"0x{mask:x}"
    names = [
        name for name in ("GDI", "FramePool", "DXGI_DesktopDup",
                          "DXGI_DesktopDup_Window", "PrintWindow", "ScreenDC")
        if mask & int(getattr(ScreencapEnum, name))
    ]
    return "|".join(names) or f"0x{mask:x}"


@dataclass
class JobFailure:
    what: str
    status: Any

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.what} failed (status={self.status})"


class MaaBackend:
    """Screencap + input through MaaFramework's Win32Controller.

    Implements the same surface as the built-in backend
    (:class:`simauto.scanner.WindowCapture` + :class:`simauto.inputs.InputController`)
    but behind one object, because MaaFramework pairs the two.
    """

    name = "maafw"

    def __init__(
        self,
        hwnd: int,
        *,
        screencap: Any = None,
        mouse_method: Any = None,
        keyboard_method: Any = None,
        use_raw_size: bool = True,
        absolute: bool = True,
        connect_timeout: float = 15.0,
        strict: bool = False,
    ):
        win32_cls, ScreencapEnum, InputEnum, _ = _import_maa()
        self.hwnd = int(hwnd)
        self.screencap_mask = resolve_screencap_method(screencap)
        # strict=True measures *this* mask and nothing else.  Without it the
        # fallback chain silently substitutes a method that works, which is the
        # right behaviour for running a bot but the wrong one for a diagnostic
        # that claims "FramePool does not work here".
        self.strict = bool(strict)
        self.mouse_method = resolve_input_method(
            mouse_method if mouse_method is not None else DEFAULT_INPUT
        )
        self.keyboard_method = resolve_input_method(
            keyboard_method if keyboard_method is not None else DEFAULT_INPUT
        )
        self.clicks = 0
        self.frames = 0
        self.last_capture_ms = 0.0
        self.last_error: str | None = None
        self.absolute = bool(absolute)
        self.attempts: list[str] = []
        self._controller = None

        failures: list[str] = []
        chain = [(f"mask:0x{self.screencap_mask:x}", self.screencap_mask)] if self.strict \
            else screencap_fallback_chain(
                screencap if screencap is not None else DEFAULT_SCREENCAP
            )
        for label, mask in chain:
            try:
                self._controller = self._construct(win32_cls, mask)
                self._connect(connect_timeout)
                if use_raw_size:
                    # Keep 1:1 pixel mapping so reference-resolution ROIs stay valid.
                    try:
                        self._controller.set_screenshot_use_raw_size(True)
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = f"set_screenshot_use_raw_size not applied: {exc}"
                # MaaFramework reports success from post_connection even when no
                # screencap method works (the failure only surfaces on the first
                # frame), so prove it now instead of at the first tick.  GDI in
                # particular succeeds on a DirectX window and returns all-black,
                # so the frame's *content* is what gets checked, not the call.
                probe = self._raw_screencap()
                if not self._frame_has_content(probe):
                    raise ImageError(
                        f"returned an all-black {probe.shape[1]}x{probe.shape[0]} frame"
                    )
                self.screencap_mask = mask
                self.screencap_label = label
                self.probe_shape = (int(probe.shape[1]), int(probe.shape[0]))
                self.attempts.append(f"{label}: ok {self.probe_shape[0]}x{self.probe_shape[1]}")
                return
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                failures.append(f"{label}: {str(exc).splitlines()[0][:110]}")
                self.attempts.append(f"{label}: {type(exc).__name__}")
                self._drop_controller()

        extra = (
            "\nThis was a strict single-method test, so no fallback was attempted."
            if self.strict else
            "\nMost common causes: the target runs elevated (run this as admin too), "
            "the window is protected (FramePool/WGC returns access denied and "
            "PrintWindow returns error 5 - e.g. a browser window), or the session "
            "is not an interactive desktop session."
        )
        raise MaaBackendUnavailable(
            "MaaFramework could not capture hwnd 0x{:X}. Tried:\n  {}".format(
                self.hwnd, "\n  ".join(failures)
            ) + extra
        )

    @staticmethod
    def _frame_has_content(frame: np.ndarray, min_visible_ratio: float = 0.02) -> bool:
        """Cheap 'is this a picture rather than a black rectangle?' test.

        A dark game loading screen could trip this, but a black frame is far
        more often a broken capture than a real one, and accepting it means
        matching templates against nothing forever.
        """
        try:
            grey = frame.astype(np.float32).mean(axis=2)
        except (ValueError, TypeError):
            return False
        return float((grey >= 12).mean()) >= min_visible_ratio

    def _construct(self, win32_cls, mask: int):
        # The first positional parameter is spelled ``hWnd`` (capital W) in the
        # released binding, while the docs' demo1.py uses ``hwnd=``.  Passing it
        # positionally works for both spellings, so do that.
        try:
            return win32_cls(
                self.hwnd,
                screencap_method=mask,
                mouse_method=self.mouse_method,
                keyboard_method=self.keyboard_method,
            )
        except TypeError:
            return win32_cls(
                hwnd=self.hwnd,
                screencap_method=mask,
                mouse_method=self.mouse_method,
                keyboard_method=self.keyboard_method,
            )

    def _connect(self, timeout: float) -> None:
        started = time.time()
        job = self._controller.post_connection()
        while not job.done:
            if time.time() - started > timeout:
                raise TimeoutError(
                    f"controller did not connect within {timeout}s"
                )
            time.sleep(0.02)
        self.connection_status = str(job.status)

    def _drop_controller(self) -> None:
        """Release a controller that did not work, without raising from __del__."""
        ctrl, self._controller = self._controller, None
        if ctrl is None:
            return
        try:
            ctrl.post_inactive().wait()
        except Exception:  # noqa: BLE001
            pass
        try:
            del ctrl
        except Exception:  # noqa: BLE001
            pass

    # -- capture -----------------------------------------------------------

    def _raw_screencap(self) -> np.ndarray:
        """One screencap as (H, W, 3) RGB, or raise."""
        job = self._controller.post_screencap()
        job.wait()
        image = job.get()
        if image is None:
            raise ImageError("MaaFramework screencap returned no image")
        arr = np.asarray(image)
        if arr.ndim != 3 or arr.shape[2] < 3:
            raise ImageError(f"unexpected screencap shape {arr.shape}")
        # MaaFramework hands back BGRA; drop alpha and reverse to RGB.
        return np.ascontiguousarray(arr[:, :, 2::-1])

    def grab(self) -> np.ndarray:
        t0 = time.perf_counter()
        frame = self._raw_screencap()
        self.frames += 1
        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
        return frame

    @property
    def client_size(self) -> tuple[int, int]:
        ctrl = self._controller
        res = getattr(ctrl, "resolution", (0, 0)) if ctrl is not None else (0, 0)
        if res and res[0] > 0:
            return (int(res[0]), int(res[1]))
        return w32.get_client_size(self.hwnd)

    # -- input -------------------------------------------------------------

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1,
              hold: float | None = None) -> Any:
        from .inputs import ClickResult
        contact = {"left": 0, "right": 1, "middle": 2}.get(button, 0)
        screen = w32.client_to_screen(self.hwnd, int(x), int(y))
        try:
            for _ in range(max(1, int(count))):
                if hold:
                    self._controller.post_touch_down(int(x), int(y), contact).wait()
                    time.sleep(hold)
                    self._controller.post_touch_up(contact).wait()
                else:
                    self._controller.post_click(int(x), int(y), contact).wait()
            self.clicks += 1
            return ClickResult(True, "maafw", screen, (int(x), int(y)), "maafw click")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maafw click failed: {exc}"
            return ClickResult(False, "maafw", screen, (int(x), int(y)), self.last_error)

    def drag(self, x0: int, y0: int, x1: int, y1: int, *, duration: float = 0.3,
             steps: int = 12, button: str = "left") -> Any:
        from .inputs import ClickResult
        contact = {"left": 0, "right": 1, "middle": 2}.get(button, 0)
        screen = w32.client_to_screen(self.hwnd, int(x0), int(y0))
        try:
            self._controller.post_swipe(
                int(x0), int(y0), int(x1), int(y1), int(max(1, duration * 1000)), contact
            ).wait()
            return ClickResult(True, "maafw", screen, (int(x0), int(y0)), "maafw swipe")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maafw swipe failed: {exc}"
            return ClickResult(False, "maafw", screen, (int(x0), int(y0)), self.last_error)

    def key(self, name: str, *, hold: float = 0.05, times: int = 1) -> bool:
        try:
            vk = w32.key_name_to_vk(name)
        except ValueError as exc:
            self.last_error = str(exc)
            return False
        try:
            for _ in range(max(1, int(times))):
                if hold and hold > 0.02:
                    self._controller.post_key_down(vk).wait()
                    time.sleep(hold)
                    self._controller.post_key_up(vk).wait()
                else:
                    self._controller.post_click_key(vk).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maafw key failed: {exc}"
            return False

    def key_down(self, name: str) -> bool:
        try:
            self._controller.post_key_down(w32.key_name_to_vk(name)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maafw key_down failed: {exc}"
            return False

    def key_up(self, name: str) -> bool:
        try:
            self._controller.post_key_up(w32.key_name_to_vk(name)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maafw key_up failed: {exc}"
            return False

    def wheel(self, delta: int = -120) -> bool:
        try:
            self._controller.post_scroll(0, int(delta)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"maafw scroll failed: {exc}"
            return False

    # -- lifecycle / introspection ----------------------------------------

    def info(self) -> dict:
        try:
            return dict(self._controller.info)
        except Exception:  # noqa: BLE001
            return {}

    def close(self) -> None:
        """Return the window to a normal state (MaaFramework un-tops it).

        MaaFramework's ``Controller.__del__`` touches ``_handle`` without a
        guard and raises "Exception ignored" noise at interpreter shutdown, so
        the controller is torn down explicitly here instead of being left to
        the garbage collector.
        """
        self._drop_controller()

    def describe(self) -> str:
        """One-line summary for logs and ``probe`` output."""
        return (
            f"maafw[{getattr(self, 'screencap_label', '?')}] "
            f"{getattr(self, 'probe_shape', ('?', '?'))[0]}x"
            f"{getattr(self, 'probe_shape', ('?', '?'))[1]}"
        )

    def __enter__(self) -> "MaaBackend":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def verify_background_capture(
    hwnd: int,
    candidates=("FramePool", "PrintWindow"),
    reference: str = "ScreenDC",
    *,
    connect_timeout: float = 15.0,
    threshold: float = 2.0,
    rounds: int = 2,
) -> list[dict]:
    """Decide whether a background method really captures the window.

    The question that matters: if something covers the window, does
    ``FramePool``/``PrintWindow`` still return the window, or does it quietly
    return a copy of the desktop?  MaaFramework happily falls back to desktop
    duplication, so "it returned a frame" proves nothing.

    Method, designed to survive a *moving* picture:

    1. Grab a fresh desktop crop of the window's client area (ground truth for
       "what is on screen right now").
    2. Grab the candidate method and the reference screen method.
       For each candidate frame X::

           d_ref   = |X - screen_method_frame|   # same "does it read the screen"
           d_desk  = |X - desktop_crop|

       A desktop-copy implementation is a screen read, so *both* distances are
       small.  A real window capture has an independent source and cannot match
       two independently-timed screenshots, so ``d_ref`` stays large.
    3. Repeat ``rounds`` times and take the *median*, because a live game makes
       any single pairwise comparison noisy.

    Returns one dict per candidate with the measured distances and a verdict.
    """
    from .scanner import _frame_stats

    results: list[dict] = []
    _, ScreencapEnum, _, _ = _import_maa()
    ref_mask = int(getattr(ScreencapEnum, reference))

    def _one(mask: int) -> np.ndarray | None:
        # strict: measure this exact mask, never a substituted fallback.
        try:
            backend = MaaBackend(hwnd, screencap=mask, connect_timeout=connect_timeout,
                                 strict=True)
        except Exception:  # noqa: BLE001
            return None
        try:
            return backend.grab()
        except Exception:  # noqa: BLE001
            return None
        finally:
            backend.close()

    for name in candidates:
        entry: dict = {"method": name, "ok": False}
        try:
            mask = int(getattr(ScreencapEnum, name))
        except AttributeError:
            entry["error"] = f"unknown method {name!r}"
            results.append(entry)
            continue

        d_refs: list[float] = []
        d_desks: list[float] = []
        for _ in range(max(1, rounds)):
            try:
                from .win32 import client_to_screen, get_client_size, grab_desktop_region
                w, h = get_client_size(hwnd)
                sx, sy = client_to_screen(hwnd, 0, 0)
                desk = grab_desktop_region(sx, sy, w, h)
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"could not read the desktop for comparison: {exc}"
                break

            cand = _one(mask)
            ref = _one(ref_mask)
            if cand is None:
                entry["error"] = "candidate method returned no frame"
                break
            if ref is None or ref.shape != cand.shape or desk.shape != cand.shape:
                entry["error"] = "frames have mismatched shapes"
                break
            c = cand.astype(np.int16)
            d_refs.append(float(np.abs(c - ref.astype(np.int16)).mean()))
            d_desks.append(float(np.abs(c - desk.astype(np.int16)).mean()))

        if entry.get("error"):
            results.append(entry)
            continue
        d_ref = float(np.median(d_refs))
        d_desk = float(np.median(d_desks))
        entry.update(
            ok=True,
            diff_vs_screen_method=round(d_ref, 3),
            diff_vs_desktop=round(d_desk, 3),
            rounds=len(d_refs),
        )
        # It is a desktop copy only if it agrees with *both* independent screen
        # reads; agreement with just one is not enough evidence.
        if d_ref < threshold and d_desk < threshold:
            entry["verdict"] = "desktop-copy"
            entry["background_capable"] = False
        else:
            entry["verdict"] = "window-content"
            entry["background_capable"] = True
        results.append(entry)
    return results


def probe_screencap_methods(hwnd: int, methods: dict[str, str] | None = None,
                            connect_timeout: float = 10.0) -> list[dict]:
    """Try each MaaFramework screencap method *on its own* and report frame stats.

    ``strict=True`` matters here and was a real bug: without it the fallback
    chain substitutes a method that works, so the entry labelled "framepool"
    could actually have been captured by ScreenDC and the report silently lied
    about which method is available.  Knowing which method truly works is the
    whole point of the probe - it decides whether the bot can run with the
    emulator covered.
    """
    from .scanner import _frame_stats  # reuse the same statistic

    if methods is None:
        methods = {k: v for k, v in SCREENCAP_METHODS.items()
                   if k in ("gdi", "framepool", "dxgi_desktopdup_window",
                            "printwindow", "screendc")}
    results = []
    for label, member in methods.items():
        entry: dict[str, Any] = {"method": label, "member": member, "ok": False}
        backend = None
        try:
            backend = MaaBackend(hwnd, screencap=member,
                                 connect_timeout=connect_timeout, strict=True)
            t0 = time.perf_counter()
            frame = backend.grab()
            entry["capture_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            entry["size"] = [int(frame.shape[1]), int(frame.shape[0])]
            entry["stats"] = _frame_stats(frame)
            entry["ok"] = True
            entry["likely_usable"] = entry["stats"]["nonblack_ratio"] > 0.2
        except MaaBackendUnavailable as exc:
            entry["error"] = str(exc).splitlines()[-1][:120] or "unavailable"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            if backend is not None:
                backend.close()
        results.append(entry)
    return results
