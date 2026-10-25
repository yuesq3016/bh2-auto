"""Input injection: drive the emulator with clicks, keys and drags.

Three methods, because no single one works everywhere:

``sendinput``
    Real hardware-level synthetic events (``SendInput``).  Works with every
    emulator, but it moves the system cursor and needs the emulator to be the
    foreground window — so you cannot use the mouse while the bot runs.

``post``
    Posts ``WM_LBUTTONDOWN``/``WM_KEYDOWN`` to the window and returns
    immediately.  The window does *not* need focus and the cursor does not
    move, so the PC stays usable.  Many emulators honour this; some ignore it.

``send``
    Same idea with ``SendMessage`` (synchronous, blocks until the target
    processes the message).  Occasionally succeeds where ``post`` fails.

Choice is per config (``input.method``), and the CLI can fall back
automatically if a click does not produce the expected screen change.
"""

from __future__ import annotations

import ctypes
import random
import time
from dataclasses import dataclass

from . import win32 as w32

__all__ = ["InputController", "METHODS", "random_delay"]

METHODS = ("sendinput", "post", "send")

# Absolute mouse coordinates for SendInput are normalised to 0..65535 across
# the virtual desktop.
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def random_delay(base: float, jitter: float = 0.0) -> float:
    """Human-ish delay.  MAA randomises its click delay for the same reason."""
    if jitter <= 0:
        return base
    return max(0.0, base + random.uniform(-jitter, jitter))


@dataclass
class ClickResult:
    ok: bool
    method: str
    screen: tuple[int, int]
    client: tuple[int, int]
    detail: str = ""


class InputController:
    """Clicks/keys target either a window handle (post/send) or the cursor."""

    def __init__(
        self,
        hwnd: int,
        *,
        method: str = "sendinput",
        focus_before_sendinput: bool = True,
        click_delay: float = 0.04,
        park_cursor: bool = True,
    ):
        if method not in METHODS:
            raise ValueError(f"unknown input method {method!r}; expected one of {METHODS}")
        self.hwnd = int(hwnd)
        self.method = method
        self.focus_before_sendinput = focus_before_sendinput
        self.click_delay = click_delay
        self.park_cursor = park_cursor
        self.clicks = 0
        self.last_error: str | None = None
        self._cursor_park: tuple[int, int] | None = None

    # -- geometry ----------------------------------------------------------

    def client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        return w32.client_to_screen(self.hwnd, x, y)

    # -- mouse -------------------------------------------------------------

    def click(
        self,
        client_x: int,
        client_y: int,
        *,
        button: str = "left",
        count: int = 1,
        hold: float | None = None,
    ) -> ClickResult:
        """Click at a point given in *client* coordinates of the target window."""
        x, y = int(round(client_x)), int(round(client_y))
        screen = self.client_to_screen(x, y)
        if self.method == "sendinput":
            ok, detail = self._click_sendinput(screen, button=button, count=count, hold=hold)
        elif self.method == "post":
            ok, detail = self._click_message(x, y, button=button, count=count, sync=False)
        else:
            ok, detail = self._click_message(x, y, button=button, count=count, sync=True)
        self.clicks += 1 if ok else 0
        if not ok:
            self.last_error = detail
        return ClickResult(ok, self.method, screen, (x, y), detail)

    def _click_sendinput(
        self, screen: tuple[int, int], *, button: str, count: int, hold: float | None
    ) -> tuple[bool, str]:
        if self.focus_before_sendinput and w32.get_foreground_window() != self.hwnd:
            w32.set_foreground_window(self.hwnd)
            time.sleep(0.06)
        down, up = (
            (w32.MOUSEEVENTF_LEFTDOWN, w32.MOUSEEVENTF_LEFTUP)
            if button == "left"
            else (w32.MOUSEEVENTF_RIGHTDOWN, w32.MOUSEEVENTF_RIGHTUP)
        )
        try:
            if not self.park_cursor and self._cursor_park is None:
                # Remember where the user had the cursor so we can give it back.
                pt = w32.wintypes.POINT()
                w32.user32.GetCursorPos(ctypes.byref(pt))
                self._cursor_park = (pt.x, pt.y)
            w32.user32.SetCursorPos(int(screen[0]), int(screen[1]))
            time.sleep(0.01)
            events = []
            if down == w32.MOUSEEVENTF_LEFTDOWN:
                events.append((w32.MOUSEEVENTF_MOVE | w32.MOUSEEVENTF_ABSOLUTE, 0))
            for _ in range(max(1, count)):
                events.append((down, 0))
                events.append((up, 0))
            ok = self._send_mouse(events)
            if hold:
                # Press, wait, release — for charge/hold attacks.
                self._send_mouse([(down, 0)])
                time.sleep(hold)
                self._send_mouse([(up, 0)])
            time.sleep(self.click_delay)
            return (True, "sendinput") if ok else (False, "SendInput rejected")
        except OSError as exc:
            return False, f"sendinput failed: {exc}"

    def _send_mouse(self, events: list[tuple[int, int]]) -> bool:
        """``events`` is a list of (dwFlags, mouseData)."""
        vx = w32.user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = w32.user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        vw = max(1, w32.user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
        vh = max(1, w32.user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        inputs = []
        for flags, data in events:
            ip = w32.INPUT()
            ip.type = w32.INPUT_MOUSE
            ip.mi = w32.MOUSEINPUT()
            ip.mi.dwFlags = flags | (w32.MOUSEEVENTF_ABSOLUTE | w32.MOUSEEVENTF_VIRTUALDESK)
            ip.mi.mouseData = data
            if flags & w32.MOUSEEVENTF_MOVE:
                pt = w32.wintypes.POINT()
                w32.user32.GetCursorPos(ctypes.byref(pt))
                ip.mi.dx = int((pt.x - vx) * 65535 / vw)
                ip.mi.dy = int((pt.y - vy) * 65535 / vh)
            inputs.append(ip)
        if not inputs:
            return True
        w32.send_inputs(inputs)
        return True

    def _click_message(self, x: int, y: int, *, button: str, count: int, sync: bool) -> tuple[bool, str]:
        msg_down = w32.WM_LBUTTONDOWN if button == "left" else w32.WM_RBUTTONDOWN
        msg_up = w32.WM_LBUTTONUP if button == "left" else w32.WM_RBUTTONUP
        mk = w32.MK_LBUTTON if button == "left" else w32.MK_RBUTTON
        post = w32.user32.SendMessageW if sync else w32.user32.PostMessageW
        hwnd = w32.wintypes.HWND(self.hwnd)
        lp = w32.make_lparam(x, y)
        try:
            # A move first: many emulators only hit-test a button after
            # tracking the cursor inside the render surface.
            post(hwnd, w32.WM_MOUSEMOVE, 0, lp)
            for _ in range(max(1, count)):
                post(hwnd, msg_down, mk, lp)
                time.sleep(0.02)
                post(hwnd, msg_up, 0, lp)
                time.sleep(self.click_delay)
            return True, f"{'send' if sync else 'post'}Message WM_LBUTTON*"
        except OSError as exc:
            return False, f"message click failed: {exc}"

    def click_screen(self, screen_x: int, screen_y: int, **kwargs) -> ClickResult:
        client = w32.screen_to_client(self.hwnd, int(screen_x), int(screen_y))
        return self.click(client[0], client[1], **kwargs)

    def drag(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        *,
        duration: float = 0.3,
        steps: int = 12,
        button: str = "left",
    ) -> ClickResult:
        """Press at (x0, y0), move to (x1, y1), release.  Client coordinates."""
        steps = max(2, int(steps))
        if self.method == "sendinput":
            screen0 = self.client_to_screen(x0, y0)
            screen1 = self.client_to_screen(x1, y1)
            try:
                w32.user32.SetCursorPos(*screen0)
                self._send_mouse([(w32.MOUSEEVENTF_LEFTDOWN, 0)])
                for i in range(1, steps + 1):
                    t = i / steps
                    sx = int(screen0[0] + (screen1[0] - screen0[0]) * t)
                    sy = int(screen0[1] + (screen1[1] - screen0[1]) * t)
                    w32.user32.SetCursorPos(sx, sy)
                    time.sleep(max(0.0, duration / steps))
                self._send_mouse([(w32.MOUSEEVENTF_LEFTUP, 0)])
                return ClickResult(True, "sendinput", screen0, (int(x0), int(y0)), "drag")
            except OSError as exc:
                return ClickResult(False, "sendinput", screen0, (int(x0), int(y0)), str(exc))

        hwnd = w32.wintypes.HWND(self.hwnd)
        post = w32.user32.SendMessageW if self.method == "send" else w32.user32.PostMessageW
        down = w32.WM_LBUTTONDOWN if button == "left" else w32.WM_RBUTTONDOWN
        up = w32.WM_LBUTTONUP if button == "left" else w32.WM_RBUTTONUP
        mk = w32.MK_LBUTTON if button == "left" else w32.MK_RBUTTON
        post(hwnd, down, mk, w32.make_lparam(x0, y0))
        for i in range(1, steps + 1):
            t = i / steps
            mx = int(x0 + (x1 - x0) * t)
            my = int(y0 + (y1 - y0) * t)
            post(hwnd, w32.WM_MOUSEMOVE, mk, w32.make_lparam(mx, my))
            time.sleep(max(0.0, duration / steps))
        post(hwnd, up, 0, w32.make_lparam(x1, y1))
        return ClickResult(True, self.method, self.client_to_screen(x0, y0),
                           (int(x0), int(y0)), "drag")

    def mouse_wheel(self, delta: int = -120) -> bool:
        if self.method == "sendinput":
            return self._send_mouse([(w32.MOUSEEVENTF_WHEEL, delta & 0xFFFFFFFF)])
        # WM_MOUSEWHEEL carries *screen* coordinates in its lParam.
        pt = w32.wintypes.POINT(0, 0)
        w32.user32.GetCursorPos(ctypes.byref(pt))
        lparam = w32.make_lparam(pt.x, pt.y)
        return bool(w32.user32.PostMessageW(
            w32.wintypes.HWND(self.hwnd), w32.WM_MOUSEWHEEL,
            (int(delta) & 0xFFFF) << 16, lparam,
        ))

    # -- keyboard ----------------------------------------------------------

    def key(self, name: str, *, hold: float = 0.05, times: int = 1) -> bool:
        """Press and release a key ``times`` times."""
        try:
            vk = w32.key_name_to_vk(name)
        except ValueError as exc:
            self.last_error = str(exc)
            return False
        if self.method == "sendinput":
            if self.focus_before_sendinput and w32.get_foreground_window() != self.hwnd:
                w32.set_foreground_window(self.hwnd)
                time.sleep(0.05)
            events = []
            for _ in range(max(1, times)):
                events.append((vk, False))
                events.append((vk, True))
            self._send_keys(events, hold=hold)
            return True

        hwnd = w32.wintypes.HWND(self.hwnd)
        post = w32.user32.SendMessageW if self.method == "send" else w32.user32.PostMessageW
        scan = int(w32.user32.MapVirtualKeyW(vk, 0))
        lp = 1 | (scan << 16)
        for _ in range(max(1, times)):
            post(hwnd, w32.WM_KEYDOWN, vk, lp)
            time.sleep(hold)
            post(hwnd, w32.WM_KEYUP, vk, lp | (1 << 30) | (1 << 31))
            time.sleep(self.click_delay)
        return True

    def key_down(self, name: str) -> bool:
        vk = w32.key_name_to_vk(name)
        if self.method == "sendinput":
            self._send_keys([(vk, False)], hold=0.0)
            return True
        scan = int(w32.user32.MapVirtualKeyW(vk, 0))
        return bool(w32.user32.PostMessageW(
            w32.wintypes.HWND(self.hwnd), w32.WM_KEYDOWN, vk, 1 | (scan << 16)
        ))

    def key_up(self, name: str) -> bool:
        vk = w32.key_name_to_vk(name)
        if self.method == "sendinput":
            self._send_keys([(vk, True)], hold=0.0)
            return True
        scan = int(w32.user32.MapVirtualKeyW(vk, 0))
        return bool(w32.user32.PostMessageW(
            w32.wintypes.HWND(self.hwnd), w32.WM_KEYUP, vk, 1 | (scan << 16)
        ))

    def _send_keys(self, events: list[tuple[int, bool]], *, hold: float) -> None:
        inputs = []
        for vk, is_up in events:
            ip = w32.INPUT()
            ip.type = w32.INPUT_KEYBOARD
            ip.ki = w32.KEYBDINPUT()
            ip.ki.wVk = vk
            ip.ki.wScan = int(w32.user32.MapVirtualKeyW(vk, 0))
            ip.ki.dwFlags = w32.KEYEVENTF_KEYUP if is_up else 0
            inputs.append(ip)
            if not is_up and hold > 0:
                # Keep down/up as separate SendInput batches so the hold duration
                # is a real gap the game can observe.
                w32.send_inputs(inputs)
                inputs = []
                time.sleep(hold)
        if inputs:
            w32.send_inputs(inputs)

    # -- housekeeping ------------------------------------------------------

    def wheel(self, delta: int = -120) -> bool:
        """Scroll wheel by ``delta`` (120 == one notch, negative scrolls down)."""
        return self.mouse_wheel(delta)

    def restore_cursor(self) -> None:
        """Return the cursor to where the user left it (sendinput mode only)."""
        if self._cursor_park:
            try:
                w32.user32.SetCursorPos(*self._cursor_park)
            except OSError:
                pass

    def close(self) -> None:
        self.restore_cursor()
