"""ADB backend — capture and control the *device*, not the window.

Why this exists: window capture on Windows is fundamentally fragile.  On this
machine MuMu 12 cannot be captured in the background at all (FramePool returns
access denied, PrintWindow returns error 5), so the only window-based option is
reading the screen, which forces the emulator to stay visible and uncovered.

Talking to the emulator over ADB removes that whole class of problem:

* ``adb exec-out screencap`` reads the framebuffer from inside Android, so the
  window may be minimised, covered, or on another virtual desktop;
* the frame contains *only* the game, with no title bar, no emulator toolbar and
  no desktop showing through;
* input goes through minitouch/maatouch (or ``input tap``), which lands on the
  Android input stack rather than the Windows message queue, so it needs no
  window focus.

The price is latency: measured on MuMu 12 / this machine, a screencap costs
~450 ms (RawByNetcat) against ~33 ms reading the window, i.e. about **2 fps**
instead of ~30 fps, and a click costs ~51 ms.  That makes ADB the right choice
for reliability and the wrong one for anything twitchy, which is exactly why
both backends exist and ``capture.backend`` selects between them.

Everything here goes through MaaFramework's ``AdbController`` (LGPL-3.0), which
brings device discovery, the fastest-screencap speed test and the minitouch /
maatouch binaries.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .imaging import ImageError
from .maa_backend import (
    DEFAULT_INPUT, INPUT_METHODS, MaaBackendUnavailable, _import_maa,
    init_maa_environment, parse_input_token,
)

__all__ = [
    "AdbBackend", "AdbBackendUnavailable", "SCREENCAP_METHODS_ADB", "INPUT_METHODS_ADB",
    "default_adb_path", "list_adb_devices", "connect_adb_device", "GAME_PACKAGE",
]

# 崩坏学园2 官服的包名。**官服是 Original,混服是另一个包名** —— 写错会启动到
# 别的客户端。实测用 `adb shell pm list packages` 在本机 MuMu 12 上确认。
GAME_PACKAGE = "com.miHoYo.HSoDv2Original"

# MaaAdbScreencapMethodEnum members, in the order that makes sense to try.
# RawByNetcat measured fastest on MuMu 12 (454 ms vs 976 ms for Encode), but it
# is marked low-compatibility, so it is only a *preference* on top of the
# framework's own speed test.
SCREENCAP_METHODS_ADB: dict[str, str] = {
    "default": "Default",
    "all": "All",
    "encode": "Encode",
    "encodetofileandpull": "EncodeToFileAndPull",
    "rawwithgzip": "RawWithGzip",
    "rawbynetcat": "RawByNetcat",
    "minicapdirect": "MinicapDirect",
    "minicapstream": "MinicapStream",
    "emulatorextras": "EmulatorExtras",
}

# MaaAdbInputMethodEnum members.  These are a *single* choice (no OR), and the
# fast ones need an agent binary pushed to the device.
#
# NOTE: the released binding spells them ``AdbShell``, ``MinitouchAndAdbKey``,
# ``Maatouch`` (lower-case t), ``EmulatorExtras`` - not the ``MaaTouch`` used in
# the docs.  Verified against the installed MaaAdbInputMethodEnum.
INPUT_METHODS_ADB: dict[str, str] = {
    "default": "Default",
    "adb": "AdbShell",
    "adbshell": "AdbShell",
    "maatouch": "Maatouch",
    "maatouchfast": "Maatouch",
    "minitouch": "MinitouchAndAdbKey",
    "minitouchandadbkey": "MinitouchAndAdbKey",
    "emulatorextras": "EmulatorExtras",
    "all": "All",
}

# Quick Connect addresses for the common emulators, tried when the caller does
# not name one.  MuMu 12 defaults to 16384; 7555 is what MuMu's own
# "adb connect" helper uses and points at the same guest.
KNOWN_ADB_ADDRESSES = (
    "127.0.0.1:16384",   # MuMu 12
    "127.0.0.1:7555",    # MuMu / MEmu
    "127.0.0.1:62001",   # Nox
    "127.0.0.1:21503",   # MEmu
    "127.0.0.1:5555",    # LDPlayer / BlueStacks
    "127.0.0.1:5556",
    "emulator-5554",
)

# Where emulators keep their own adb.exe, so the user does not have to install
# platform-tools.  MuMu ships one; so do the others.
_ADB_SEARCH_GLOBS = (
    r"D:\MuMuPlayer\nx_device\*\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"D:\MuMuPlayer\shell\adb.exe",
    r"C:\Program Files\Nox\bin\adb.exe",
    r"D:\LDPlayer\LDPlayer9\adb.exe",
    r"C:\LDPlayer\LDPlayer9\adb.exe",
    r"C:\Program Files (x86)\Nox\bin\adb.exe",
)


class AdbBackendUnavailable(MaaBackendUnavailable):
    """No usable adb.exe or no reachable device."""


def default_adb_path() -> str | None:
    """Locate an adb.exe: PATH first, then the known emulator installs."""
    found = shutil.which("adb")
    if found:
        return found
    import glob
    for pattern in _ADB_SEARCH_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]        # highest version directory wins
    return None


@dataclass
class AdbDevice:
    adb_path: str
    address: str
    model: str = ""
    state: str = ""

    def describe(self) -> str:
        return f"{self.address} ({self.model or 'unknown'}, {self.state or '?'})"


def list_adb_devices(adb_path: str | None = None, timeout: float = 20.0) -> list[AdbDevice]:
    """``adb devices -l`` parsed into :class:`AdbDevice` records.

    Deliberately shells out instead of using MaaToolkit: the toolkit's
    ``find_adb_devices`` only reports devices it can match to a known emulator
    profile and returned nothing here even with a device connected, while
    ``adb devices`` always tells the truth.
    """
    adb = adb_path or default_adb_path()
    if not adb or not os.path.exists(adb if os.path.isabs(adb) else adb):
        return []
    try:
        import subprocess
        proc = subprocess.run(
            [adb, "devices", "-l"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[AdbDevice] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        address, state = parts[0], parts[1]
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1]
        devices.append(AdbDevice(adb, address, model, state))
    return devices


def connect_adb_device(
    adb_path: str | None = None,
    address: str | None = None,
    *,
    try_known_ports: bool = True,
    timeout: float = 20.0,
) -> AdbDevice:
    """Return a usable device, connecting to a known port if necessary."""
    adb = adb_path or default_adb_path()
    if not adb:
        raise AdbBackendUnavailable(
            "no adb.exe found. MuMu ships one at "
            r"D:\MuMuPlayer\nx_device\<ver>\shell\adb.exe; pass capture.adb_path "
            "or install Android platform-tools."
        )

    if address:
        devices = list_adb_devices(adb, timeout)
        live = [d for d in devices if d.address == address and d.state == "device"]
        if live:
            return live[0]
        try:
            import subprocess
            subprocess.run([adb, "connect", address], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdbBackendUnavailable(f"adb connect {address} failed: {exc}") from exc
        devices = list_adb_devices(adb, timeout)
        live = [d for d in devices if d.address == address and d.state == "device"]
        if live:
            return live[0]
        raise AdbBackendUnavailable(
            f"device {address} did not come up; `adb devices -l` shows: "
            + ", ".join(d.describe() for d in devices) or "(nothing)"
        )

    devices = list_adb_devices(adb, timeout)
    live = [d for d in devices if d.state == "device"]
    if live:
        return live[0]

    if try_known_ports:
        import subprocess
        for candidate in KNOWN_ADB_ADDRESSES:
            try:
                subprocess.run([adb, "connect", candidate], capture_output=True,
                               text=True, timeout=8, encoding="utf-8", errors="replace")
            except (OSError, subprocess.SubprocessError):
                continue
            live = [d for d in list_adb_devices(adb, timeout) if d.state == "device"]
            if live:
                return live[0]

    raise AdbBackendUnavailable(
        "no ADB device is connected. Start the emulator, then either the emulator's "
        "own adb port will be found automatically or pass capture.address "
        "(e.g. 127.0.0.1:16384 for MuMu 12). `adb devices -l` currently shows: "
        + (", ".join(d.describe() for d in devices) or "(nothing)")
    )


class AdbBackend:
    """Screencap + input through MaaFramework's AdbController.

    Same surface as the other backends, so the engine can swap it in without
    caring where the pixels came from.
    """

    name = "adb"

    def __init__(
        self,
        *,
        adb_path: str | None = None,
        address: str | None = None,
        screencap_methods: Any = None,
        input_methods: Any = None,
        config: dict | None = None,
        connect_timeout: float = 30.0,
        use_raw_size: bool = True,
        **_: Any,
    ):
        from maa.controller import AdbController  # type: ignore

        if not init_maa_environment():
            pass  # cosmetic only

        self.device = connect_adb_device(adb_path, address)
        self.adb_path = self.device.adb_path
        self.address = self.device.address
        self.clicks = 0
        self.frames = 0
        self.last_capture_ms = 0.0
        self.last_error: str | None = None
        self.screencap_label = _resolve_adb_screencap(screencap_methods)
        self.input_label = _resolve_adb_input(input_methods)

        try:
            self._controller = AdbController(
                adb_path=self.adb_path,
                address=self.address,
                screencap_methods=self.screencap_label[1],
                input_methods=self.input_label[1],
                config=config or {},
            )
        except Exception as exc:  # noqa: BLE001
            raise AdbBackendUnavailable(
                f"AdbController could not be created for {self.address}: {exc}"
            ) from exc

        started = time.time()
        job = self._controller.post_connection()
        while not job.done:
            if time.time() - started > connect_timeout:
                raise AdbBackendUnavailable(
                    f"ADB device {self.address} did not connect within "
                    f"{connect_timeout}s"
                )
            time.sleep(0.05)
        if not bool(getattr(self._controller, "connected", True)):
            raise AdbBackendUnavailable(f"ADB device {self.address} is not connected")

        if use_raw_size:
            try:
                self._controller.set_screenshot_use_raw_size(True)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"set_screenshot_use_raw_size not applied: {exc}"

        # Warm up once: the first screencap pays for the agent push and the
        # speed test, and everything after it is much cheaper.
        self._size: tuple[int, int] | None = None
        warm = self.grab()
        self._size = (int(warm.shape[1]), int(warm.shape[0]))

    # -- capture -----------------------------------------------------------

    def grab(self) -> np.ndarray:
        t0 = time.perf_counter()
        job = self._controller.post_screencap()
        job.wait()
        image = job.get()
        if image is None:
            self.last_error = "ADB screencap returned no image"
            raise ImageError(self.last_error)
        arr = np.asarray(image)
        if arr.ndim != 3 or arr.shape[2] < 3:
            raise ImageError(f"unexpected screencap shape {arr.shape}")
        # BGRA from the binding -> RGB
        frame = np.ascontiguousarray(arr[:, :, 2::-1])
        self.frames += 1
        self.last_capture_ms = (time.perf_counter() - t0) * 1000.0
        return frame

    @property
    def client_size(self) -> tuple[int, int]:
        """The device resolution.

        For ADB the "client area" concept does not exist: coordinates are device
        pixels.  Using the resolution of the frame we actually receive keeps ROI
        scaling correct, which matters because the binding may hand back a
        scaled screenshot depending on its screenshot options.
        """
        if self._size:
            return self._size
        res = getattr(self._controller, "resolution", (0, 0)) or (0, 0)
        if res and res[0] > 0:
            return (int(res[0]), int(res[1]))
        return (0, 0)

    @property
    def is_background_capable(self) -> bool:
        """ADB reads the framebuffer inside Android, so yes - always."""
        return True

    # -- input -------------------------------------------------------------

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1,
              hold: float | None = None) -> Any:
        from .inputs import ClickResult
        contact = {"left": 0, "right": 1, "middle": 2}.get(button, 0)
        try:
            for _ in range(max(1, int(count))):
                if hold:
                    self._controller.post_touch_down(int(x), int(y), contact).wait()
                    time.sleep(hold)
                    self._controller.post_touch_up(contact).wait()
                else:
                    self._controller.post_click(int(x), int(y), contact).wait()
            self.clicks += 1
            return ClickResult(True, "adb", (int(x), int(y)), (int(x), int(y)), "adb tap")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb click failed: {exc}"
            return ClickResult(False, "adb", (int(x), int(y)), (int(x), int(y)),
                               self.last_error)

    def touch_down(self, x: int, y: int, contact: int = 0) -> bool:
        """按下一根手指并保持不放(多点按住用 contact 区分)。

        「没有自动套」时的挂机就靠它:contact=0 按住方向、contact=1 按住第一个
        装备键,一直按到结算再 ``touch_up``。ADB 输入方法(maatouch)支持多点。
        """
        try:
            self._controller.post_touch_down(int(x), int(y), int(contact)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb touch_down failed: {exc}"
            return False

    def touch_up(self, contact: int = 0) -> bool:
        """松开某根手指(与 ``touch_down`` 配对)。"""
        try:
            self._controller.post_touch_up(int(contact)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb touch_up failed: {exc}"
            return False

    def drag(self, x0: int, y0: int, x1: int, y1: int, *, duration: float = 0.3,
             steps: int = 12, button: str = "left") -> Any:
        from .inputs import ClickResult
        contact = {"left": 0, "right": 1, "middle": 2}.get(button, 0)
        try:
            self._controller.post_swipe(
                int(x0), int(y0), int(x1), int(y1), int(max(1, duration * 1000)), contact
            ).wait()
            return ClickResult(True, "adb", (int(x0), int(y0)), (int(x0), int(y0)), "adb swipe")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb swipe failed: {exc}"
            return ClickResult(False, "adb", (int(x0), int(y0)), (int(x0), int(y0)),
                               self.last_error)

    def key(self, name: str, *, hold: float = 0.05, times: int = 1) -> bool:
        from .win32 import key_name_to_vk
        try:
            # Android key codes are not Windows virtual-key codes; the binding
            # passes the value straight through as an Android keycode, so map
            # the few names that matter for games.
            code = _android_keycode(name, key_name_to_vk)
        except ValueError as exc:
            self.last_error = str(exc)
            return False
        try:
            for _ in range(max(1, int(times))):
                if hold and hold > 0.05:
                    self._controller.post_key_down(code).wait()
                    time.sleep(hold)
                    self._controller.post_key_up(code).wait()
                else:
                    self._controller.post_click_key(code).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb key failed: {exc}"
            return False

    def key_down(self, name: str) -> bool:
        try:
            self._controller.post_key_down(_android_keycode(name)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb key_down failed: {exc}"
            return False

    def key_up(self, name: str) -> bool:
        try:
            self._controller.post_key_up(_android_keycode(name)).wait()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"adb key_up failed: {exc}"
            return False

    def info(self) -> dict:
        try:
            return dict(self._controller.info)
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ App

    def is_app_running(self, package: str = GAME_PACKAGE) -> bool:
        """游戏当前是否在运行(有前台或后台进程都算)。

        ⚠️ 只靠 ``pidof`` 不够:个别模拟器/ROM 对**后台**进程返回空,于是
        "用户已经手动开好游戏"会被误判成"没启动",流程回头去重启它。
        用户明确要求过:**已经启动的就别动、更不要报错**。所以 pidof
        查不到时再用 ``ps`` 兜底,adb 本身出错时退到前台判断。
        """
        out = self._adb_shell(f"pidof {package}", timeout=10)
        if out is None:
            return self.is_app_foreground(package)
        if out.strip() and "not found" not in out.lower():
            return True
        ps = self._adb_shell(f"ps -A | grep {package}", timeout=10) or ""
        return package in ps

    def is_app_foreground(self, package: str = GAME_PACKAGE) -> bool:
        """游戏是否在前台(用于判断"启动完成")。"""
        out = self._adb_shell("dumpsys activity activities", timeout=15) or ""
        for line in out.splitlines():
            if "topResumedActivity" in line or "mResumedActivity" in line:
                if package in line:
                    return True
        # 兜底:少数 ROM 的 activity dumps 不带上面两个字段,window 里有焦点行
        win = self._adb_shell("dumpsys window", timeout=15) or ""
        return any(("mCurrentFocus" in ln or "mFocusedApp" in ln) and package in ln
                   for ln in win.splitlines())

    def _bring_to_front(self, package: str = GAME_PACKAGE) -> bool:
        """把**已经在运行**的游戏带回前台,不重新启动它。

        ⚠️ 这里绝不能调 ``start_app``:有的模拟器对已在运行的应用执行启动
        会重载进程,把用户手动开着的游戏打断。``monkey`` 的 LAUNCHER 意图
        只是把已有任务切到前台。
        """
        out = self._adb_shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=15)
        return out is not None

    def start_app(self, package: str = GAME_PACKAGE,
                  activity: str | None = None, wait: float = 3.0) -> bool:
        """启动游戏。

        优先用 MaaFramework 的 ``start_app``(它知道该用 monkey 还是 am start);
        失败则退回直接调 ``adb shell am start`` 或 ``monkey``。

        崩坏学园2 官服的包名是 ``com.miHoYo.HSoDv2Original`` —— 注意是
        **Original**,混服是另一个包名,写错会启动到别的客户端。
        """
        started = False
        try:
            fn = getattr(self._controller, "start_app", None)
            if callable(fn):
                fn(package)
                started = True
        except Exception:  # noqa: BLE001
            started = False

        if not started:
            if activity:
                cmd = f"am start -n {package}/{activity}"
            else:
                # monkey 不需要知道 Activity 名,对游戏最稳
                cmd = (f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
            out = self._adb_shell(cmd, timeout=20)
            started = bool(out is not None and "Error" not in (out or ""))
        if not started and self.is_app_running(package):
            # 目标进程其实在跑,只是这次启动动作没成功 —— 用户要求:
            # 已经启动过就不要因为「启动失败」报错
            started = True
        if wait:
            time.sleep(wait)
        return started

    def stop_app(self, package: str = GAME_PACKAGE) -> bool:
        """强制停止游戏(用于"关闭游戏"这类收尾动作)。"""
        try:
            fn = getattr(self._controller, "stop_app", None)
            if callable(fn):
                fn(package)
                return True
        except Exception:  # noqa: BLE001
            pass
        out = self._adb_shell(f"am force-stop {package}", timeout=15)
        return out is not None

    def ensure_game_running(self, package: str = GAME_PACKAGE,
                            log=print, timeout_s: float = 120) -> bool:
        """确保游戏在运行并到前台。

        用户明确要求的约定:**游戏已经在运行时直接算成功** —— 不再去启动它,
        也不因为「已经启动」而报错。

        判断顺序:
          ① 进程在跑?  在 -> 不启动,只等/切前台
          ② 在前台?    是 -> 立刻返回 True
          ③ 不在前台?  在跑就用 ``monkey`` 切前台(绝不重启);真的退出了才启动
        """
        if self.is_app_running(package):
            log("    游戏已在运行(不重复启动)")
        else:
            log(f"    游戏未运行,启动 {package} …")
            if not self.start_app(package):
                log("    !! 启动失败")
                return False

        if self.is_app_foreground(package):
            log("    游戏已在前台")
            return True

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_app_foreground(package):
                log("    游戏已在前台")
                return True
            if self.is_app_running(package):
                # 还在跑,只是没到前台(加载中/被切走):温和切前台,不重启
                self._bring_to_front(package)
            else:
                # 进程真的没了,才重新启动
                self.start_app(package)
            time.sleep(3.0)
        if self.is_app_running(package):
            log(f"    ⚠️ {timeout_s:.0f}s 内没等到游戏到前台,但进程还在 —— "
                f"按「已在运行」处理,不报启动错误")
            return True
        log(f"    !! {timeout_s:.0f}s 内未等到游戏到前台")
        return False

    def _adb_shell(self, cmd: str, timeout: float = 15) -> str | None:
        """直接跑 ``adb -s <addr> shell <cmd>``。

        MaaFramework 的控制器没有暴露通用 shell,而启动/查询 App 状态需要它。
        失败返回 None,调用方据此降级。
        """
        import subprocess
        adb = self.adb_path
        if not adb:
            return None
        try:
            res = subprocess.run(
                [adb, "-s", self.address, "shell", cmd],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0 and not res.stdout:
            return None
        return res.stdout

    def close(self) -> None:
        try:
            del self._controller
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "AdbBackend":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _resolve_adb_screencap(spec: Any) -> tuple[str, int]:
    from maa.define import MaaAdbScreencapMethodEnum as S  # type: ignore
    if isinstance(spec, int):
        return (f"0x{spec & 0xFFFFFFFF:x}", spec)
    if spec is None or str(spec).strip() == "":
        return ("Default", int(S.Default))
    key = str(spec).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    member = SCREENCAP_METHODS_ADB.get(key)
    if member is None:
        match = next((n for n in S.__members__ if n.lower() == key), None)
        if match is None:
            raise ValueError(
                f"unknown ADB screencap method {spec!r}; known: "
                f"{', '.join(sorted(set(SCREENCAP_METHODS_ADB)))}, or one of "
                f"{', '.join(S.__members__)}"
            )
        member = match
    return (member, int(getattr(S, member)))


def _resolve_adb_input(spec: Any) -> tuple[str, int]:
    from maa.define import MaaAdbInputMethodEnum as I  # type: ignore
    if isinstance(spec, int):
        return (f"0x{spec & 0xFFFFFFFF:x}", spec)
    if spec is None or str(spec).strip() == "":
        return ("Default", int(I.Default))
    key = str(spec).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    member = INPUT_METHODS_ADB.get(key)
    if member is None:
        # Accept the exact enum member name too, case-insensitively, so a
        # user copying from the docs is not defeated by MaaTouch/Maatouch.
        match = next((n for n in I.__members__ if n.lower() == key), None)
        if match is None:
            raise ValueError(
                f"unknown ADB input method {spec!r}; known: "
                f"{', '.join(sorted(set(INPUT_METHODS_ADB)))}, or one of "
                f"{', '.join(I.__members__)}"
            )
        member = match
    return (member, int(getattr(I, member)))


# Android key codes for the handful of keys a game bot needs.  Android's
# KEYCODE_* values are their own numbering, NOT Windows VK codes.
_ANDROID_KEYS = {
    "back": 4,
    "home": 3,
    "menu": 82,
    "space": 62,
    "enter": 66,
    "escape": 111,
    "esc": 111,
    "tab": 61,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "a": 29, "b": 30, "c": 31, "d": 32, "e": 33, "f": 34, "g": 35, "h": 36,
    "i": 37, "j": 38, "k": 39, "l": 40, "m": 41, "n": 42, "o": 43, "p": 44,
    "q": 45, "r": 46, "s": 47, "t": 48, "u": 49, "v": 50, "w": 51, "x": 52,
    "y": 53, "z": 54,
    "0": 7, "1": 8, "2": 9, "3": 10, "4": 11,
    "5": 12, "6": 13, "7": 14, "8": 15, "9": 16,
}


def _android_keycode(name: str, vk_lookup=None) -> int:
    key = str(name).strip().lower()
    if key in _ANDROID_KEYS:
        return _ANDROID_KEYS[key]
    if key.startswith("vkey_") and key[5:] in _ANDROID_KEYS:
        return _ANDROID_KEYS[key[5:]]
    raise ValueError(
        f"unknown Android key {name!r}; known: "
        f"{', '.join(sorted(_ANDROID_KEYS))}"
    )
