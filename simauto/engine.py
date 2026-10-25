"""The executor: turns a MAA-style task description into clicks.

Design rules, all of them borrowed from MAA after seeing how a real,
long-lived automation config behaves in practice:

1. **Tasks are data, not code.**  A task is a dict: what to recognise, where
   to recognise it, what to do when it appears.  Adding a stage means adding
   JSON, not touching Python.
2. **Everything is a poll, not a state machine.**  Each tick we ask "does any
   of my triggers match *right now*?"  Loading screens, popups and network
   lag then need no special cases.
3. **Small ROIs, reference-resolution coordinates.**  The config is written
   against 1280x720 (the emulator default) and scaled to whatever the window
   actually is, so the same config survives a resolution change.
4. **Debounce so one button press is one click.**  A trigger fires on the
   rising edge of a match, plus an optional cooldown, so a button that stays
   on screen for 400 ms is not clicked six times.
5. **Feed back on failure.**  When a required template does not match, dump
   the frame and the scores into a session folder — a black screenshot or a
   0.2 score tells you the problem instantly.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .imaging import ImageError, crop_roi, load_image, resize_rgb, save_image
from .inputs import InputController, random_delay
from .maa_backend import (
    BACKGROUND_SCREENCAP_LABELS, MaaBackend, MaaBackendUnavailable, maa_available,
)
from .matcher import MatchResult, run_detector
from .scanner import PROBE_MODES, WindowCapture, probe_window, resolve_window
from . import win32 as w32

__all__ = ["Config", "Engine", "EngineError", "FrameSource", "load_config"]


class EngineError(RuntimeError):
    pass


class _DeviceStandIn:
    """Placeholder window info for backends that do not use a window (ADB).

    Keeps ``FrameSource.window`` non-None so every caller can still log
    something meaningful, without pretending a real window was found.
    """

    hwnd = 0
    title = "(ADB device: no window involved)"
    class_name = ""
    pid = 0
    rect = (0, 0, 0, 0)
    client_size = (0, 0)
    visible = True
    iconic = False
    process_name = ""

    def describe(self) -> str:
        return self.title


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def _load_structured(path: str) -> dict:
    """Read JSON or YAML.  YAML needs PyYAML; JSON always works."""
    # utf-8-sig, not utf-8: Notepad (and PowerShell's Set-Content) prepend a BOM
    # which json.loads rejects outright.  Reading it away costs nothing and
    # stops a hand-edited config from failing with a confusing parse error.
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise EngineError(
                "this config is YAML but PyYAML is not installed. Either "
                "`pip install pyyaml`, or convert the config to JSON."
            ) from exc
        data = yaml.safe_load(text)
    else:
        # Tolerate comments in JSON configs: strip a trailing // comment per
        # line before parsing.  Cheap, and it keeps configs readable.
        cleaned = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
        data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise EngineError(f"{path}: top level must be a mapping/object")
    return data


class Config:
    """A parsed task file plus the paths needed to resolve templates."""

    def __init__(self, data: dict, path: str | None = None):
        self.data = data
        self.path = os.path.abspath(path) if path else None
        self.dir = os.path.dirname(self.path) if self.path else os.getcwd()
        self.name = str(data.get("name") or (os.path.basename(self.path) if self.path else "unnamed"))
        self.window_spec = dict(data.get("window") or {})
        self.capture_cfg = dict(data.get("capture") or {})
        self.input_cfg = dict(data.get("input") or {})
        self.defaults = dict(data.get("defaults") or {})
        self.recognizers = dict(data.get("recognizers") or {})
        self.tasks = dict(data.get("tasks") or {})
        self.loop_cfg = dict(data.get("loop") or {})
        self.reference = self._parse_reference(data.get("reference_resolution") or [1280, 720])
        self.template_cache: dict[str, Any] = {}
        self.roi_cache: dict[str, Any] = {}
        self._validate()

    # -- reference resolution / ROI scaling --------------------------------

    @staticmethod
    def _parse_reference(value) -> tuple[int, int]:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return (int(value[0]), int(value[1]))
        raise EngineError(f"reference_resolution must be [w, h], got {value!r}")

    def resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(self.dir, path))

    def scale_roi(self, roi, client_size: tuple[int, int]):
        """Map a reference-resolution ROI onto the live client area.

        ROIs are given as [x, y, w, h] in reference coordinates.  We scale by
        the axis ratio and clamp, so a slightly different window size still
        lands on the same UI element.
        """
        if not roi:
            return None
        key = (tuple(int(v) for v in roi), tuple(client_size))
        if key in self.roi_cache:
            return self.roi_cache[key]
        rw, rh = self.reference
        cw, ch = client_size
        sx, sy = cw / rw, ch / rh
        x, y, w, h = (float(v) for v in roi)
        out = (
            max(0, int(round(x * sx))),
            max(0, int(round(y * sy))),
            max(1, int(round(w * sx))),
            max(1, int(round(h * sy))),
        )
        if len(self.roi_cache) < 512:
            self.roi_cache[key] = out
        return out

    def scale_point(self, x: float, y: float, client_size: tuple[int, int]) -> tuple[int, int]:
        """Map a reference-resolution point onto the live client area."""
        rw, rh = self.reference
        cw, ch = client_size
        return (int(round(x * cw / rw)), int(round(y * ch / rh)))

    # -- validation --------------------------------------------------------

    def _validate(self) -> None:
        # A config needs *either* tasks (event/loop mode) or a top-level steps
        # list (once mode).  Demanding `tasks` made a perfectly valid scripted
        # config fail validation.
        if not self.tasks and not self.data.get("steps"):
            raise EngineError(
                "config has no 'tasks' section and no top-level 'steps' list; "
                "give it one of the two"
            )
        for name, task in self.tasks.items():
            if not isinstance(task, dict):
                raise EngineError(f"task {name!r} must be an object")
            for trig in task.get("triggers") or []:
                target = trig.get("recognizer") or trig.get("any") or trig.get("all")
                if not target:
                    raise EngineError(f"task {name!r}: trigger needs 'recognizer', 'any' or 'all'")
            for action in task.get("actions") or []:
                if "action" not in action:
                    raise EngineError(f"task {name!r}: every action needs an 'action' key")
        for name, spec in self.recognizers.items():
            method = str(spec.get("method", "template")).lower()
            if method == "template" and not (spec.get("template") or spec.get("templates")):
                raise EngineError(f"recognizer {name!r}: template method needs 'template'")
            if method in ("color", "bar") and not (spec.get("bands") or spec.get("color_scales")):
                raise EngineError(f"recognizer {name!r}: {method} needs 'bands' or 'color_scales'")

    # -- templates ---------------------------------------------------------

    def template(self, path: str) -> np.ndarray:
        """Load (and cache) a template image."""
        key = self.resolve_path(path)
        if key not in self.template_cache:
            self.template_cache[key] = load_image(key)
        return self.template_cache[key]

    def recognizer(self, name: str) -> dict:
        if name in self.roi_cache and isinstance(self.roi_cache[name], dict):
            return self.roi_cache[name]
        spec = self.recognizers.get(name)
        if spec is None:
            raise EngineError(f"unknown recognizer {name!r}")
        out = dict(spec)
        out.setdefault("name", name)
        # MAA-style default: a recognizer named X with no template uses X.png.
        if str(out.get("method", "template")).lower() == "template" and not (
            out.get("template") or out.get("templates")
        ):
            out["template"] = f"templates/{name}.png"
        return out


def load_config(path: str) -> Config:
    return Config(_load_structured(path), path)


# --------------------------------------------------------------------------
# Frame source: capture + reference-resolution scaling
# --------------------------------------------------------------------------

class FrameSource:
    """Owns frame capture and the reference-resolution conversion.

    Two interchangeable backends:

    ``gdi``     the built-in :class:`~simauto.scanner.WindowCapture`
    ``maafw``   MaaFramework's Win32Controller (``pip install maafw``), whose
                FramePool/PrintWindow capture works on GPU-rendered emulator
                windows where plain GDI returns black

    Frames are kept at their native size and only ROIs are scaled, because
    resizing a 1080p frame 60 times a second costs more than the matching.

    ROI mapping always uses the size of the frame we actually got, not the
    window's nominal client size: if a backend hands back a scaled screenshot
    (MaaFramework can, depending on its screenshot options), scaling against
    the captured frame keeps coordinates correct instead of silently matching
    the wrong patch of screen.
    """

    def __init__(
        self,
        config: Config,
        hwnd: int | None = None,
        mode: str | None = None,
        *,
        backend: str | None = None,
        capture: Any = None,
        input_controller: Any = None,
        window: Any = None,
        logger=None,
    ):
        self.config = config
        # An injected window (or capture) skips window discovery, which is what
        # makes replaying a recorded frame or testing without an emulator work.
        self.log = logger or (lambda msg: None)
        self.backend_name = str(
            backend if backend is not None
            else config.capture_cfg.get("backend") or config.data.get("backend") or "auto"
        ).lower()
        # An ADB backend does not need a window at all: it talks to the device.
        needs_window = capture is None and self.backend_name not in ("adb", "android")
        if window is not None:
            self.window = window
        elif needs_window:
            try:
                self.window = resolve_window(config.window_spec)
            except LookupError:
                # No window spec and no window found: fall through to the device
                # stand-in rather than refusing to start, because `--backend adb`
                # is a perfectly good reason not to have a window at all.
                self.window = _DeviceStandIn()
        else:
            self.window = _DeviceStandIn()
        self.mode = mode or str(config.capture_cfg.get("method") or "printwindow_fullcontent")

        self.capture_input = input_controller
        self.capture = capture if capture is not None else self._open_capture()
        self.frames = 0
        size = getattr(self.capture, "client_size", None)
        if (not size or not size[0]) and self.window.hwnd:
            size = w32.get_client_size(self.window.hwnd)
        if not size or not size[0]:
            raise EngineError(
                f"backend {self.backend_name!r} reported no frame size; "
                f"is the device connected?"
            )
        self._client_size = (int(size[0]), int(size[1]))
        self._scale_noted = False
        self._backend_noted = False

    @property
    def is_maa(self) -> bool:
        return getattr(self.capture, "name", "") in ("maafw", "adb")

    def _open_capture(self):
        if self.backend_name in ("auto", ""):
            if maa_available():
                try:
                    self.log("capture backend: maafw (MaaFramework Win32Controller)")
                    return self._open_maa()
                except (MaaBackendUnavailable, ValueError) as exc:
                    self.log(f"maafw backend unusable ({exc}); falling back to GDI")
            return self._open_gdi()
        if self.backend_name in ("gdi", "builtin", "win32"):
            return self._open_gdi()
        if self.backend_name in ("maafw", "maa", "maaframework"):
            self.log("capture backend: maafw (MaaFramework Win32Controller)")
            return self._open_maa()
        if self.backend_name in ("adb", "android"):
            self.log("capture backend: adb (device framebuffer, works in the background)")
            return self._open_adb()
        raise EngineError(
            f"unknown capture.backend {self.backend_name!r}; "
            f"expected auto, maafw, gdi or adb"
        )

    def _open_adb(self):
        from .adb_backend import AdbBackend, AdbBackendUnavailable

        cfg = self.config.capture_cfg
        try:
            return AdbBackend(
                adb_path=cfg.get("adb_path") or self.config.data.get("adb_path"),
                address=cfg.get("address") or cfg.get("adb_address"),
                # NOTE: capture.screencap means a *maafw* method.  ADB has its
                # own namespace, so it gets its own key to avoid a config that
                # works with one backend failing cryptically with the other.
                screencap_methods=cfg.get("adb_screencap"),
                input_methods=cfg.get("adb_input") or cfg.get("input_method"),
                config=cfg.get("adb_config"),
            )
        except AdbBackendUnavailable as exc:
            raise EngineError(
                f"ADB backend unavailable: {exc}\n"
                f"hint: the emulator must be running; MuMu 12 listens on "
                f"127.0.0.1:16384 and this machine's adb is at "
                f"D:\\MuMuPlayer\\nx_device\\<ver>\\shell\\adb.exe"
            ) from exc

    def _open_gdi(self):
        if not self.window.hwnd:
            raise EngineError(
                "the GDI backend needs a window, but none was resolved. Point it at "
                "one with window.title / window.hwnd, or use `--backend adb` which "
                "needs no window at all."
            )
        mode = self.mode
        if mode == "auto":
            mode = self._pick_mode()
        if mode not in PROBE_MODES:
            raise EngineError(
                f"unknown capture method {mode!r}; expected auto or one of {PROBE_MODES}"
            )
        self.mode = mode
        return WindowCapture(self.window.hwnd, mode=mode).open()

    def _open_maa(self):
        cfg = self.config.capture_cfg
        return MaaBackend(
            self.window.hwnd,
            screencap=cfg.get("screencap") or cfg.get("screencap_method"),
            mouse_method=cfg.get("mouse") or cfg.get("mouse_method"),
            keyboard_method=cfg.get("keyboard") or cfg.get("keyboard_method"),
            use_raw_size=bool(cfg.get("use_raw_size", True)),
        )

    def _pick_mode(self) -> str:
        if not self.window.hwnd:
            raise EngineError(
                "this backend needs a window handle; use backend=adb for device capture"
            )
        for entry in probe_window(self.window.hwnd):
            if entry.get("likely_usable"):
                return entry["mode"]
        raise EngineError(
            f"no GDI backend produced a usable frame for hwnd 0x{self.window.hwnd:X}. "
            f"Run `simauto probe` to inspect, install maafw (`pip install maafw`) so "
            f"FramePool/PrintWindow capture is available, or switch the emulator to its "
            f"compatibility (software) renderer."
        )

    @property
    def client_size(self) -> tuple[int, int]:
        return self._client_size

    def grab(self) -> np.ndarray:
        frame = self.capture.grab()
        size = (int(frame.shape[1]), int(frame.shape[0]))
        if size != self._client_size:
            if not self._scale_noted and self.is_maa:
                self.log(
                    f"note: screencap is {size[0]}x{size[1]} but the window client area "
                    f"is {self._client_size[0]}x{self._client_size[1]}; ROIs are mapped "
                    f"onto the captured frame"
                )
                self._scale_noted = True
            self._client_size = size
        if self.is_maa and not self._backend_noted:
            # Say out loud which method is actually in use.  A requested
            # background method (FramePool/PrintWindow) can silently be replaced
            # by a screen read, and the user then believes the emulator may be
            # covered - which it may not.
            self._backend_noted = True
            backend = getattr(self.capture, "name", "maafw")
            if backend == "adb":
                # ADB reads the framebuffer inside Android, so covering the
                # window is fine - no warning here.
                self.log(
                    f"adb capture: {getattr(self.capture, 'screencap_label', ('?',))[0]}"
                    f" screencap, {getattr(self.capture, 'input_label', ('?',))[0]} input"
                    f" - works while the emulator window is covered"
                )
            else:
                label = getattr(self.capture, "screencap_label", "?")
                self.log(f"maafw capture method in use: {label}")
                if label not in BACKGROUND_SCREENCAP_LABELS:
                    self.log(
                        "  !! screen-reading method: keep the emulator window visible "
                        "and on top. No background backend (FramePool/PrintWindow) was "
                        "available for this window - run `simauto verify` for the truth."
                    )
        self.frames += 1
        return frame

    def roi(self, roi):
        return self.config.scale_roi(roi, self._client_size)

    def point(self, x: float, y: float) -> tuple[int, int]:
        return self.config.scale_point(x, y, self._client_size)

    def close(self) -> None:
        self.capture.close()

    def __enter__(self) -> "FrameSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _orient(frame: np.ndarray, rotation: int) -> np.ndarray:
    """Apply the emulator's orientation so configs can assume landscape."""
    rotation = int(rotation) % 360
    if rotation == 0:
        return frame
    if rotation == 90:
        return np.ascontiguousarray(np.rot90(frame, k=-1))
    if rotation == 180:
        return np.ascontiguousarray(np.rot90(frame, k=2))
    if rotation == 270:
        return np.ascontiguousarray(np.rot90(frame, k=1))
    raise EngineError(f"rotation must be one of 0/90/180/270, got {rotation}")


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------

class Recorder:
    """Writes frames to a session folder for debugging."""

    def __init__(self, root: str, enabled: bool = True, every: float = 0.0):
        self.root = os.path.abspath(root)
        self.enabled = enabled
        self.every = float(every)
        self.session = time.strftime(os.path.join(self.root, "%Y%m%d-%H%M%S"))
        self._last_auto = 0.0
        self._index = 0
        self.files: list[str] = []

    def frame(self, frame: np.ndarray, tag: str) -> str | None:
        if not self.enabled:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)[:60] or "frame"
        self._index += 1
        path = os.path.join(self.session, f"{self._index:05d}_{safe}.png")
        save_image(path, frame)
        self.files.append(path)
        return path

    def maybe_periodic(self, frame: np.ndarray, now: float, tag: str = "tick") -> str | None:
        if not self.enabled or self.every <= 0:
            return None
        if now - self._last_auto >= self.every:
            self._last_auto = now
            return self.frame(frame, tag)
        return None


# --------------------------------------------------------------------------
# Action helpers
# --------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any, variables: dict[str, Any]) -> Any:
    """Substitute ``${var}`` inside strings, recursively for containers."""
    if isinstance(value, str):
        def sub(m):
            key = m.group(1)
            if key not in variables:
                raise EngineError(f"undefined variable {key!r}")
            return str(variables[key])
        return _VAR_RE.sub(sub, value)
    if isinstance(value, list):
        return [_interpolate(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v, variables) for k, v in value.items()}
    return value


def _eval_condition(cond, variables: dict[str, Any]) -> bool:
    """Evaluate a small condition language.

    Supported forms::

        true / false
        "${flag}"                     -> truthiness of the variable
        "!${flag}"
        ">= 0.6"                      -> compare the last match score
        "score >= 0.6" / "fill < 0.3" / "pixels > 500"
        ["${a}", ">", "3"]            -> binary comparison
    """
    if cond is None or cond is True:
        return True
    if cond is False:
        return False
    if isinstance(cond, (int, float)):
        return bool(cond)
    if isinstance(cond, list):
        if len(cond) == 3:
            left, op, right = (_interpolate(v, variables) for v in cond)
            return _compare(left, op, right)
        if len(cond) == 2:
            left, op = (_interpolate(v, variables) for v in cond)
            return _compare(left, op, 0)
        raise EngineError(f"bad condition {cond!r}")
    if isinstance(cond, str):
        text = _interpolate(cond, variables).strip()
        if text.lower() in ("true", "yes", "1"):
            return True
        if text.lower() in ("false", "no", "0", ""):
            return False
        negate = text.startswith("!")
        if negate:
            text = text[1:].strip()
        m = re.match(r"^(?:([A-Za-z_][A-Za-z0-9_]*)\s*)?(==|!=|>=|<=|>|<)\s*(.+)$", text)
        if m:
            key, op, rhs = m.group(1), m.group(2), m.group(3).strip()
            left = _lookup(key, variables) if key else variables.get("_score", 0)
            result = _compare(left, op, _to_number(rhs))
        else:
            result = bool(_lookup(text, variables))
        return (not result) if negate else result
    raise EngineError(f"bad condition {cond!r}")


def _lookup(key: str, variables: dict[str, Any]) -> Any:
    if key in variables:
        return variables[key]
    if key in ("score",):
        return variables.get("_score", 0.0)
    if key in ("hit",):
        return variables.get("_hit", False)
    raise EngineError(f"undefined variable {key!r} in condition")


def _to_number(text: str) -> Any:
    try:
        return float(text)
    except ValueError:
        return text


def _compare(left: Any, op: str, right: Any) -> bool:
    try:
        if isinstance(right, str) and isinstance(left, (int, float)):
            right = _to_number(right)
        elif isinstance(left, str) and isinstance(right, (int, float)):
            left = _to_number(left)
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
    except TypeError:
        return False
    raise EngineError(f"unknown operator {op!r}")


# --------------------------------------------------------------------------
# Trigger / action dataclasses
# --------------------------------------------------------------------------

@dataclass
class Trigger:
    """A condition on the current frame that makes a task fire."""

    name: str
    mode: str = "all"                 # recognizer | any | all
    recognizers: list[str] = field(default_factory=list)
    cooldown: float = 0.0
    min_interval: float = 0.0
    require_all: bool = False
    # Recognisers that must NOT match for this trigger to fire.  Needed because
    # UI elements *stack*: the main-menu nav bar is visible behind the daily
    # popups, so "nav bar is on screen" alone cannot mean "the popups are gone".
    absent: list[str] = field(default_factory=list)
    _last_fire: float = -1e9

    def ready(self, now: float) -> bool:
        gap = max(self.cooldown, self.min_interval)
        return (now - self._last_fire) >= gap

    def mark(self, now: float) -> None:
        self._last_fire = now

    def describe(self) -> str:
        base = f"{self.mode}({', '.join(self.recognizers)})"
        if self.absent:
            base += f" and not({', '.join(self.absent)})"
        return base


def _build_trigger(task_name: str, spec: dict) -> Trigger:
    absent = spec.get("absent") or spec.get("without") or []
    if isinstance(absent, str):
        absent = [absent]
    absent = [str(a) for a in absent]

    def _mk(mode: str, recs, require_all: bool = False) -> Trigger:
        return Trigger(
            task_name, mode, recs,
            float(spec.get("cooldown", 0.0)), float(spec.get("min_interval", 0.0)),
            require_all=require_all, absent=absent,
        )

    if "recognizer" in spec:
        return _mk("recognizer", [str(spec["recognizer"])])
    if "any" in spec:
        recs = spec["any"]
        return _mk("any", [str(r) for r in (recs if isinstance(recs, list) else [recs])])
    if "all" in spec:
        recs = spec["all"]
        return _mk("all", [str(r) for r in (recs if isinstance(recs, list) else [recs])],
                   require_all=True)
    # A trigger made only of `absent` conditions is legal: "none of these are on
    # screen" is a real state to react to.
    if absent and spec.get("when_absent"):
        return _mk("absent-only", [])
    raise EngineError(f"task {task_name!r}: trigger needs recognizer/any/all")


@dataclass
class Tick:
    """What happened during one poll — handed to actions."""

    frame: np.ndarray
    now: float
    results: dict[str, MatchResult] = field(default_factory=dict)
    last: MatchResult | None = None
    task: str = ""

    def score(self, name: str | None = None) -> float:
        if name:
            res = self.results.get(name)
            return float(res.score) if res else 0.0
        return float(self.last.score) if self.last else 0.0


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class Engine:
    """Runs a :class:`Config` until it finishes, fails or is stopped."""

    def __init__(
        self,
        config: Config,
        *,
        input_method: str | None = None,
        capture_method: str | None = None,
        backend: str | None = None,
        frame_source: FrameSource | None = None,
        input_controller: Any = None,
        debug: bool = False,
        record: bool = False,
        record_every: float = 0.0,
        dry_run: bool = False,
        max_ticks: int | None = None,
        log=None,
    ):
        self.config = config
        self.log = log or (lambda msg: print(msg, flush=True))
        self.debug = debug
        self.dry_run = dry_run

        # Injected for tests (and for replaying recorded frames): an engine that
        # can only run against a live window is an engine you cannot test.
        #
        # Track ownership so run() closes only what it created.  A caller that
        # passes a shared, already-open backend (the GUI runs a whole task list
        # through one AdbBackend) must not have it torn down mid-sequence --
        # doing so deletes the backend's controller and every later task fails
        # with AttributeError.
        self._owns_source = frame_source is None
        self.source = frame_source or FrameSource(
            config, mode=capture_method, backend=backend, logger=self.log
        )
        # The capture backend may own input too (MaaFramework pairs them, and
        # its PostMessage path needs no focus and does not move the cursor).
        if input_controller is not None:
            self.input = input_controller
            self._owns_input = False
        elif getattr(self.source.capture, "name", "") in ("maafw", "adb"):
            # Both MaaFramework-backed controllers own their input as well, and
            # pairing them avoids mixing an ADB screencap with a Windows click.
            self.input = self.source.capture
            self._owns_input = self._owns_source
        else:
            method = input_method or str(config.input_cfg.get("method") or "sendinput")
            self.input = InputController(
                self.source.window.hwnd,
                method=method,
                focus_before_sendinput=bool(config.input_cfg.get("focus", True)),
                click_delay=float(config.input_cfg.get("click_delay", 0.04)),
            )
            self._owns_input = True

        self.recorder = Recorder(
            config.resolve_path(str(config.data.get("session_dir", "sessions"))),
            enabled=record or debug,
            every=record_every,
        )
        self.max_ticks = max_ticks
        self.variables: dict[str, Any] = dict(config.data.get("variables") or {})
        self.ticks = 0
        self.started = time.time()
        self._stop_reason: str | None = None
        self._task_counts: dict[str, int] = {}
        self._rotation = int(config.capture_cfg.get("rotation", 0) or 0)
        self.check_every = float(config.defaults.get("check_every", 0.05))
        self.click_jitter = float(config.defaults.get("click_jitter", 2))
        self.miss_limit = int(config.defaults.get("max_consecutive_misses", 30))
        self._misses = 0
        self._frame: np.ndarray | None = None

    # -- frame -------------------------------------------------------------

    def frame(self, force: bool = False) -> np.ndarray:
        if self._frame is None or force:
            raw = self.source.grab()
            self._frame = _orient(raw, self._rotation) if self._rotation else raw
        return self._frame

    # -- recognition -------------------------------------------------------

    def recognize(self, name: str, frame: np.ndarray | None = None,
                  index: int = 0) -> MatchResult:
        frame = self.frame() if frame is None else frame
        spec = dict(self.config.recognizer(name))
        spec["roi"] = self.source.roi(spec.get("roi"))
        if index:
            spec["index"] = index
        if str(spec.get("method", "template")).lower() == "template":
            tpl = spec.get("template")
            if tpl is None:
                tpl = spec.get("templates")
            if isinstance(tpl, list):
                spec["template"] = [self.config.template(str(t)) for t in tpl]
            elif tpl is not None:
                spec["template"] = self.config.template(str(tpl))
        try:
            return run_detector(frame, spec)
        except (ImageError, ValueError, OSError) as exc:
            self.log(f"  ! recognizer {name}: {exc}")
            return MatchResult(False, name=name, method=str(spec.get("method", "template")),
                               metrics={"error": str(exc)})

    def evaluate(self, trigger: Trigger) -> tuple[bool, Tick]:
        """Check one trigger against a *single* fresh frame."""
        frame = self.frame(force=True)
        tick = Tick(frame=frame, now=time.time())
        for name in trigger.recognizers:
            tick.results[name] = self.recognize(name, frame)
        if trigger.mode == "all":
            ok = all(r.hit for r in tick.results.values())
            order = trigger.recognizers
        elif trigger.mode == "any":
            ok = any(r.hit for r in tick.results.values())
            order = trigger.recognizers
        else:
            order = trigger.recognizers[:1]
            ok = bool(tick.results and next(iter(tick.results.values())).hit)
        tick.last = tick.results.get(order[0]) if order else None
        # Prefer the strongest hit for actions that use the match location.
        hits = [r for r in tick.results.values() if r.hit]
        if hits:
            tick.last = max(hits, key=lambda r: r.score)
        if ok and trigger.absent:
            for nm in trigger.absent:
                blocker = self.recognize(nm, frame)
                tick.results[f"absent:{nm}"] = blocker
                if blocker.hit:
                    ok = False
        if self.debug:
            summary = " ".join(r.describe() for r in tick.results.values())
            self.log(f"  {trigger.describe()} -> {'HIT' if ok else 'miss'} | {summary}")
        return ok, tick

    # -- actions -----------------------------------------------------------

    def run_actions(self, actions: list[dict], tick: Tick, depth: int = 0) -> None:
        if depth > 8:
            raise EngineError("action nesting too deep (recursive task?)")
        for raw_action in actions or []:
            action = _interpolate(raw_action, self.variables)
            kind = str(action.get("action", "")).lower()
            if kind == "click":
                self._act_click(action, tick)
            elif kind == "key":
                self._act_key(action)
            elif kind == "swipe":
                self._act_swipe(action, tick)
            elif kind == "wait":
                self._act_wait(action)
            elif kind == "click_verified":
                self._act_click_verified(action, tick, depth)
            elif kind == "wait_for":
                self._act_wait_for(action, tick, depth)
            elif kind == "if":
                cond = _eval_condition(action.get("condition"), self._vars_with(tick))
                branch = action.get("then") if cond else action.get("else")
                self.run_actions(branch or [], tick, depth + 1)
            elif kind == "repeat":
                self._act_repeat(action, tick, depth)
            elif kind == "repeat_until":
                self._act_repeat_until(action, tick, depth)
            elif kind == "click_text_until_gone":
                self._act_click_text_until_gone(action, tick, depth)
            elif kind == "dispatch_expeditions":
                self._act_dispatch_expeditions(action, tick, depth)
            elif kind == "resonance_refresh":
                self._act_resonance_refresh(action, tick, depth)
            elif kind == "equip_select":
                self._act_equip_select(action, tick, depth)
            elif kind == "screenshot":
                # An explicit screenshot action is a request, not incidental
                # debug output, so force the recorder on rather than respecting
                # --record (which used to make this log "saved None").
                self.recorder.enabled = True
                path = self.recorder.frame(tick.frame, str(action.get("tag", "shot")))
                self.log(f"  saved {path}")
            elif kind == "log":
                self.log(f"  {action.get('message', '')}")
            elif kind == "add_jitter":
                self.click_jitter = float(action.get("radius", self.click_jitter))
            elif kind == "set":
                self.variables[str(action["name"])] = action.get("value")
            elif kind == "wait_for_result":
                self._act_wait_for_result(action, depth)
            elif kind == "stop":
                self._stop_reason = str(action.get("reason", "stop action"))
            elif kind == "abort":
                raise EngineError(str(action.get("reason", "aborted by config")))
            else:
                raise EngineError(f"unknown action {kind!r}")
            if self._stop_reason:
                return

    def _vars_with(self, tick: Tick) -> dict[str, Any]:
        """Variables plus the current match, so conditions can use score/fill."""
        out = dict(self.variables)
        out["_score"] = tick.score()
        out["_hit"] = bool(tick.last and tick.last.hit)
        if tick.last:
            out.update({f"_{k}": v for k, v in tick.last.metrics.items()})
        return out

    def _resolve_point(self, action: dict, tick: Tick) -> tuple[int, int]:
        """Where to click: explicit point, a named recognizer, or the match.

        ``index`` picks the Nth distinct match instead of the best one, which is
        how a list of identical controls (several "claim reward" buttons) gets
        drained in order: index 0, then 1, and so on.
        """
        index = int(action.get("index", 0) or 0)
        if "point" in action:
            x, y = action["point"]
            return self.source.point(float(x), float(y))
        if "target" in action:
            name = str(action["target"])
            res = tick.results.get(name)
            if res is None or index or not res.hit:
                res = self.recognize(name, tick.frame, index=index)
            if not res.hit or res.loc is None:
                raise EngineError(
                    f"click target {name!r} did not match at index {index} "
                    f"(score {res.score:.3f})"
                )
            return (int(res.loc[0]), int(res.loc[1]))
        if tick.last and tick.last.loc:
            return (int(tick.last.loc[0]), int(tick.last.loc[1]))
        raise EngineError("click action has no point, target or matched location")

    def _act_click(self, action: dict, tick: Tick) -> None:
        x, y = self._resolve_point(action, tick)
        jitter = float(action.get("jitter", self.click_jitter) or 0)
        if jitter:
            # Randomise inside a small disc: identical pixel-perfect input
            # forever is the easiest pattern to fingerprint.
            angle = random.uniform(0.0, 2.0 * math.pi)
            radius = jitter * math.sqrt(random.random())
            x = int(round(x + radius * math.cos(angle)))
            y = int(round(y + radius * math.sin(angle)))
        count = int(action.get("count", 1))
        hold = action.get("hold")
        hold = float(hold) if hold is not None else None
        if self.dry_run:
            self.log(f"  [dry-run] click ({x}, {y}) count={count}")
            return
        result = self.input.click(x, y, count=count, hold=hold)
        if self.debug or not result.ok:
            self.log(f"  click ({x}, {y}) -> "
                     f"{'ok' if result.ok else 'FAILED: ' + result.detail}")
        if not result.ok:
            raise EngineError(f"input failed: {result.detail}")

    def _act_key(self, action: dict) -> None:
        key = action.get("key")
        keys = action.get("keys") or ([key] if key else [])
        if isinstance(keys, str):
            keys = [keys]
        hold = float(action.get("hold", 0.05))
        times = int(action.get("times", 1))
        if self.dry_run:
            self.log(f"  [dry-run] key {keys} x{times}")
            return
        for k in keys:
            ok = self.input.key(str(k), hold=hold, times=times)
            if not ok:
                raise EngineError(f"key {k!r} failed: {self.input.last_error}")

    def _act_swipe(self, action: dict, tick: Tick) -> None:
        x0, y0 = self._resolve_endpoint(action, "from", tick)
        x1, y1 = self._resolve_endpoint(action, "to", tick)
        duration = float(action.get("duration", 0.3))
        if self.dry_run:
            self.log(f"  [dry-run] swipe ({x0},{y0}) -> ({x1},{y1}) in {duration}s")
            return
        self.input.drag(x0, y0, x1, y1, duration=duration,
                        steps=int(action.get("steps", 12)))

    def _resolve_endpoint(self, action: dict, key: str, tick: Tick) -> tuple[int, int]:
        value = action.get(key)
        if isinstance(value, str):
            res = tick.results.get(value) or self.recognize(value, tick.frame)
            if not res.hit or res.loc is None:
                raise EngineError(f"swipe endpoint {value!r} did not match")
            return (int(res.loc[0]), int(res.loc[1]))
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return self.source.point(float(value[0]), float(value[1]))
        raise EngineError(f"swipe needs '{key}' as [x, y] or a recognizer name")

    def _act_wait(self, action: dict) -> None:
        base = float(action.get("seconds", action.get("base", 0.0)))
        jitter = float(action.get("jitter", 0.0))
        delay = random_delay(base, jitter)
        if self.dry_run:
            return
        time.sleep(max(0.0, delay))

    def _act_click_verified(self, action: dict, tick: Tick, depth: int) -> None:
        """Click, then require some recogniser to appear (or disappear).

        This is the answer to a real failure mode: a click can silently do
        nothing (wrong point, a modal in the way, the game ignoring synthetic
        input) and the old ``click`` action reported success anyway, so the
        script cheerfully kept clicking a screen that never changed.  Verifying
        turns that into a loud, early failure with the frame saved.

        Fields: ``point``/``target``, ``expect`` (recogniser that must appear),
        ``expect_gone`` (recogniser that must vanish), ``timeout``,
        ``interval``, ``retries`` (default 1 = do not re-click), ``settle``
        (delay before polling starts).
        """
        expect = action.get("expect")
        gone = action.get("expect_gone")
        if not expect and not gone:
            raise EngineError("click_verified needs 'expect' and/or 'expect_gone'")
        timeout = float(action.get("timeout", 12.0))
        interval = float(action.get("interval", 0.5))
        retries = max(1, int(action.get("retries", 1)))
        settle = float(action.get("settle", 0.0))
        wait_first = float(action.get("wait_before", 0.0))

        if self.dry_run:
            x, y = self._resolve_point(action, tick)
            self.log(f"  [dry-run] click_verified ({x}, {y}) "
                     f"expect={expect} gone={gone} retries={retries}")
            return

        last_seen: dict[str, float] = {}
        for attempt in range(1, retries + 1):
            if wait_first:
                time.sleep(wait_first)
            x, y = self._resolve_point(action, tick)
            jitter = float(action.get("jitter", self.click_jitter) or 0)
            if jitter:
                angle = random.uniform(0.0, 2.0 * math.pi)
                radius = jitter * math.sqrt(random.random())
                x = int(round(x + radius * math.cos(angle)))
                y = int(round(y + radius * math.sin(angle)))
            result = self.input.click(x, y, count=int(action.get("count", 1)))
            if not result.ok:
                raise EngineError(f"input failed: {result.detail}")
            self.log(f"  click_verified ({x}, {y}) attempt {attempt}/{retries}")
            if settle:
                time.sleep(settle)

            deadline = time.time() + timeout
            while time.time() < deadline:
                frame = self.frame(force=True)
                ok = True
                if expect:
                    name = str(expect) if not isinstance(expect, list) else None
                    names = [str(n) for n in (expect if isinstance(expect, list) else [expect])]
                    for n in names:
                        r = self.recognize(n, frame)
                        last_seen[n] = r.score
                        if not r.hit:
                            ok = False
                if gone:
                    names = [str(n) for n in (gone if isinstance(gone, list) else [gone])]
                    for n in names:
                        r = self.recognize(n, frame)
                        last_seen[f"!{n}"] = r.score
                        if r.hit:
                            ok = False
                if ok:
                    self.log(f"  verified after attempt {attempt}")
                    return
                time.sleep(interval)
            self.log(f"  not verified after attempt {attempt} "
                     f"(scores: {', '.join(f'{k}={v:.3f}' for k, v in last_seen.items())})")

        path = self.recorder.frame(self.frame(), "click_verified_failed")
        raise EngineError(
            f"click at ({x}, {y}) did not produce the expected change after "
            f"{retries} attempt(s) and {timeout}s each. Last scores: "
            + ", ".join(f"{k}={v:.3f}" for k, v in last_seen.items())
            + (f". Frame saved to {path}" if path else "")
        )

    def _act_wait_for(self, action: dict, tick: Tick, depth: int) -> None:
        """Poll until a recognizer matches (or timeout), then run steps."""
        name = str(action.get("recognizer") or action.get("target") or "")
        if not name:
            raise EngineError("wait_for needs 'recognizer'")
        timeout = float(action.get("timeout", 10.0))
        interval = float(action.get("interval", 0.25))
        deadline = time.time() + timeout
        while True:
            frame = self.frame(force=True)
            res = self.recognize(name, frame)
            if res.hit:
                sub = Tick(frame=frame, now=time.time(), results={name: res}, last=res)
                self.run_actions(action.get("then") or [], sub, depth + 1)
                return
            if time.time() >= deadline:
                if action.get("optional"):
                    sub = Tick(frame=frame, now=time.time(), results={name: res}, last=res)
                    self.run_actions(action.get("else") or [], sub, depth + 1)
                    return
                path = self.recorder.frame(frame, f"wait_for_{name}_timeout")
                raise EngineError(
                    f"wait_for {name!r} timed out after {timeout}s "
                    f"(best score {res.score:.3f}"
                    + (f", frame saved to {path}" if path else "")
                    + ")"
                )
            time.sleep(max(0.02, interval))

    def _act_wait_for_result(self, action: dict, depth: int) -> None:
        """Wait for *any* trigger to fire — the 'run until something happens' loop."""
        timeout = float(action.get("timeout", 60.0))
        deadline = time.time() + timeout
        triggers = [_build_trigger(f"inline{i}", spec)
                    for i, spec in enumerate(action.get("triggers") or [])]
        if not triggers:
            raise EngineError("wait_for_result needs 'triggers'")
        while time.time() < deadline:
            frame = self.frame(force=True)
            now = time.time()
            for trig in triggers:
                tick = Tick(frame=frame, now=now)
                for nm in trig.recognizers:
                    tick.results[nm] = self.recognize(nm, frame)
                hits = [r for r in tick.results.values() if r.hit]
                if hits:
                    tick.last = max(hits, key=lambda r: r.score)
                    on = action.get("on") or {}
                    handler = (on.get(trig.describe()) or on.get(trig.recognizers[0])
                               or on.get("any"))
                    self.run_actions(handler or [], tick, depth + 1)
                    return
            time.sleep(self.check_every)
        if not action.get("optional"):
            frame = self.frame()
            path = self.recorder.frame(frame, "wait_for_result_timeout")
            raise EngineError(
                f"wait_for_result timed out after {timeout}s"
                + (f" (frame saved to {path})" if path else "")
            )

    def _act_click_text_until_gone(self, action: dict, tick: Tick, depth: int = 0) -> None:
        """Drain a list of identical text controls, verifying progress each round.

        Written for the familiar-expedition screen.  Everything about this shape
        of UI was measured on the real thing:

        * every finished entry shows the same label (``点击领取奖励``);
        * the completion marker (``探索完成``) is what says "there is still
          something to collect";
        * clicking opens a reward window, and that window is *translucent* - the
          labels behind it still OCR fine, so the label alone cannot be used as
          the loop condition or it would re-click forever;
        * claiming makes the list scroll itself, so no scrolling is needed and
          the next claimable entry always ends up at the same place.

        Hence the decision order each round:
          1. reward window visible  -> close it, loop again
          2. claim label visible    -> click the topmost one (opens the window)
          3. completion marker left -> stuck: fail loudly with a saved frame
          4. nothing left           -> done

        Fields: ``text``, ``done_when``/``stop_when`` (marker), ``popup_text``,
        ``dismiss`` ([x,y]) or ``dismiss_text``, ``roi``, ``max_rounds``,
        ``settle``, ``confidence``.
        """
        label = str(action.get("text") or "")
        if not label:
            raise EngineError("click_text_until_gone needs 'text'")
        marker = action.get("stop_when") or action.get("done_when")
        marker = str(marker) if marker else None
        popup = action.get("popup_text")
        popup = str(popup) if popup else None
        dismiss = action.get("dismiss")
        dismiss = [float(v) for v in dismiss] if dismiss else None
        dismiss_text = action.get("dismiss_text")
        roi = action.get("roi")
        roi = tuple(int(v) for v in roi) if roi else None
        search_roi = list(roi) if roi else None
        max_rounds = int(action.get("max_rounds", 40))
        settle = float(action.get("settle", 1.5))
        conf = float(action.get("confidence", 0.75))
        # A single idle-looking frame is NOT proof of completion: the game
        # repaints the list between claims, and OCR on that transient frame sees
        # neither the label nor the marker.  That produced a false "done" while
        # two finished entries were still sitting on screen.  Require the clean
        # state to hold across several consecutive reads.
        confirmations = max(1, int(action.get("confirm_clean", 3)))

        def _texts(frame) -> list[tuple[str, float, tuple[int, int, int, int]]]:
            from .matcher import ocr_strings
            return ocr_strings(frame, roi)

        def _pick(hits, needle):
            found = [h for h in hits if needle in h[0] and h[1] >= conf]
            if not found:
                return None
            # 从上往下:按 y 坐标升序选最上面的那个(用户要求,不要按置信度随机选)
            found.sort(key=lambda h: h[2][1])
            return found[0]

        def _click_box(box, what: str) -> None:
            cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
            if self.dry_run:
                self.log(f"  [dry-run] would click {what} at ({cx}, {cy})")
                return
            res = self.input.click(cx, cy)
            if not res.ok:
                raise EngineError(f"input failed: {res.detail}")
            self.log(f"  clicked {what} at ({cx}, {cy})")

        claims = 0
        clean_streak = 0
        for round_no in range(1, max_rounds + 1):
            if round_no > 1 or action.get("settle_first"):
                time.sleep(settle)
            frame = self.frame(force=True)
            hits = _texts(frame)

            if popup:
                win = _pick(hits, popup)
                if win is not None:
                    clean_streak = 0
                    self.log(f"  round {round_no}: reward window is open, closing it")
                    if dismiss_text:
                        box = _pick(hits, str(dismiss_text))
                        if box is not None:
                            _click_box(box[2], f"'{dismiss_text}'")
                        else:
                            _click_box((int(dismiss[0]), int(dismiss[1]), 1, 1)
                                       if dismiss else (300, 650, 1, 1), "dismiss point")
                    else:
                        pt = dismiss or [300, 650]
                        _click_box((int(pt[0]), int(pt[1]), 1, 1), "dismiss point")
                    if self.dry_run:
                        return
                    continue

            claim = _pick(hits, label)
            if claim is not None:
                clean_streak = 0
                claims += 1
                self.log(f"  round {round_no}: claiming (#{claims}) "
                         f"conf={claim[1]:.3f} box={claim[2]}")
                _click_box(claim[2], f"'{label}'")
                if self.dry_run:
                    return
                continue

            if marker:
                left = [h for h in hits if marker in h[0] and h[1] >= conf]
                if left:
                    clean_streak = 0
                    path = self.recorder.frame(frame, "text_drain_stuck")
                    raise EngineError(
                        f"round {round_no}: '{label}' is gone but {len(left)} "
                        f"'{marker}' marker(s) remain at "
                        f"{[h[2] for h in left][:4]}; the click may be landing wrong"
                        + (f" (frame saved to {path})" if path else "")
                    )

            clean_streak += 1
            self.log(f"  round {round_no}: looks clean "
                     f"({clean_streak}/{confirmations} confirmations)")
            if self.dry_run:
                return
            if clean_streak >= confirmations:
                self.log(f"  done: {claims} claim(s), no '{label}' left"
                         + (f" and no '{marker}'" if marker else "")
                         + f" (confirmed {clean_streak}x)")
                return

        self.log(f"  hit max_rounds={max_rounds} after {claims} claim(s)")

    def _act_dispatch_expeditions(self, action: dict, tick: Tick, depth: int = 0) -> None:
        """Send out every expedition slot that is waiting for a servant.

        This is the familiar-expedition loop MAAHG2 documents, made explicit and
        verified at each step.  Measured behaviour on the real screen:

        1. a slot with no servant shows ``点击选择使魔``;
        2. tapping it opens the servant picker, whose success rate starts at
           ``0.00%``;
        3. ``一键派遣`` auto-fills the team;
        4. ``出发`` dispatches, and the list scrolls itself; the next pending slot
           takes its place.

        The loop therefore repeats "click the topmost 点击选择使魔 -> 一键派遣 ->
        出发" until no pending slot is visible.

        An optional success-rate gate used to sit between steps 3 and 4.  It was
        removed on request: ``一键派遣`` is trusted to fill the team, and waiting
        for the rate text to update cost a round trip every attempt.  Set
        ``rate_text`` if you want it back.
        """
        pick = str(action.get("text", "点击选择使魔"))
        quick = action.get("quick_text") or ["一键派遣", "键派遣", "派遣"]
        quick = [str(q) for q in (quick if isinstance(quick, list) else [quick])]
        depart = str(action.get("depart_text", "出发"))
        # Optional: when set, the rate must be visible before departing.  Off by
        # default so dispatch is one click after 一键派遣.
        rate_needle = action.get("rate_text")
        rate_needle = str(rate_needle) if rate_needle else None
        roi = action.get("roi")
        roi = tuple(int(v) for v in roi) if roi else None
        rate_roi = action.get("rate_roi")
        rate_roi = tuple(int(v) for v in rate_roi) if rate_roi else None
        max_rounds = int(action.get("max_rounds", 10))
        conf = float(action.get("confidence", 0.75))
        settle = float(action.get("settle", 2.0))

        def _hits(frame, region=None):
            from .matcher import ocr_strings
            return ocr_strings(frame, region if region is not None else roi)

        def _best(hits, needles):
            found = []
            for h in hits:
                if h[1] < conf:
                    continue
                for n in needles:
                    if n in h[0]:
                        found.append(h)
                        break
            if not found:
                return None
            # 从上往下:按 y 坐标升序选最上面的那个(用户要求,不要按置信度/长度随机)
            found.sort(key=lambda h: h[2][1])
            return found[0]

        def _click(hit, what: str) -> bool:
            cx, cy = hit[2][0] + hit[2][2] // 2, hit[2][1] + hit[2][3] // 2
            if self.dry_run:
                self.log(f"  [dry-run] would click {what} at ({cx}, {cy})")
                return False
            res = self.input.click(cx, cy)
            if not res.ok:
                raise EngineError(f"input failed: {res.detail}")
            self.log(f"  clicked {what} at ({cx}, {cy})")
            return True

        sent = 0
        for round_no in range(1, max_rounds + 1):
            if round_no > 1:
                time.sleep(settle)
            frame = self.frame(force=True)
            entry = _best(_hits(frame), [pick])
            if entry is None:
                self.log(f"  no '{pick}' visible; {sent} expedition(s) dispatched")
                return
            self.log(f"  round {round_no}: pending slot at {entry[2]}")
            if not _click(entry, f"'{pick}'"):
                return
            time.sleep(settle)

            picker = self.frame(force=True)
            q = _best(_hits(picker), quick)
            if q is None:
                seen = [t for t, s, _ in _hits(picker) if s >= conf][:10]
                path = self.recorder.frame(picker, "dispatch_no_quick")
                raise EngineError(
                    f"the servant picker opened but none of {quick} was found "
                    f"(saw {seen})" + (f" (frame saved to {path})" if path else "")
                )
            self.log(f"  auto-selecting servants via {q[0]!r}")
            _click(q, f"{q[0]!r}")
            time.sleep(settle)
            check = self.frame(force=True)

            if rate_needle:
                rate = _best(_hits(check, rate_roi), [rate_needle])
                if rate is None:
                    seen = [t for t, s, _ in _hits(check, rate_roi) if s >= 0.4]
                    self.log(f"  !! success rate is not {rate_needle}% (saw {seen}); "
                             f"not departing")
                    return
                self.log(f"  success rate ok ({rate[0]})")

            d = _best(_hits(check), [depart])
            if d is None:
                path = self.recorder.frame(check, "dispatch_no_depart")
                raise EngineError(
                    f"team filled but {depart!r} not found"
                    + (f" (frame saved to {path})" if path else "")
                )
            _click(d, f"'{depart}'")
            sent += 1
            time.sleep(settle)

        self.log(f"  hit max_rounds={max_rounds}; dispatched {sent}")

    def _act_equip_select(self, action: dict, tick: Tick, depth: int = 0) -> None:
        """Switch to a saved equipment loadout before a stage.

        Everything is configurable, because the group and suit names are the
        player's own:

          ``open_point``   [x, y] of the pistol icon on the stage detail screen
          ``group``        e.g. ``C自定义组``  (a group *button*)
          ``suit``         e.g. ``C01-自定义套装`` (an entry inside that group)
          ``apply_text``   default ``选择方案``
          ``verify_text``  default the group name - used to confirm the screen
                           actually changed instead of assuming the tap landed

        Measured behaviour this works around (it cost several failed attempts):

        * tapping a group toggles its sub-list open/closed - it is not a plain
          "select".  So the flow checks whether any suit entry is already
          visible and only taps the group when the list is collapsed; otherwise
          the tap would close the very list it needs.
        * the suit entries are only in the tree once the group is open, and they
          sit to the *right* of the group column (x around 560), so a hit to the
          right of the group is preferred when both match.
        """
        # Settings come from equipment.json (editable) unless the config passes
        # them inline, which keeps the loadout out of the automation logic.
        from .resonance import load_equipment_settings
        settings = dict(load_equipment_settings(action.get("equipment_file")))
        settings.update({k: v for k, v in action.items()
                         if k in ("open_point", "group", "suit", "apply_text",
                                  "switch_point", "list_min_y",
                                  "settle", "confidence", "list_marker")})

        open_pt = settings.get("open_point")
        if not open_pt:
            raise EngineError(
                "equip_select needs 'open_point' (the equipment icon) - set it in "
                "equipment.json or in the config step"
            )
        open_pt = [int(open_pt[0]), int(open_pt[1])]
        group = str(settings.get("group") or "").strip()
        suit = str(settings.get("suit") or "").strip()
        apply_text = str(settings.get("apply_text") or "选择方案")
        settle = float(settings.get("settle", 2.5))
        conf = float(settings.get("confidence", 0.85))
        list_marker = str(settings.get("list_marker") or "自定义套装")
        self.log(f"  loadout settings: group={group!r} suit={suit!r} "
                 f"apply={apply_text!r} icon={open_pt}")

        def _texts(frame):
            from .matcher import ocr_strings
            return ocr_strings(frame)

        def _pick(hits, needle, *, prefer_right=False):
            found = [h for h in hits if needle and needle in h[0] and h[1] >= conf]
            if not found:
                return None
            if prefer_right:
                # Suit entries live in the right-hand column; the group button
                # can contain the same substring.
                found.sort(key=lambda h: h[2][0], reverse=True)
            return found[0]

        def _click(hit, what):
            cx, cy = hit[2][0] + hit[2][2] // 2, hit[2][1] + hit[2][3] // 2
            if self.dry_run:
                self.log(f"  [dry-run] would click {what} at ({cx}, {cy})")
                return False
            res = self.input.click(cx, cy)
            if not res.ok:
                raise EngineError(f"input failed: {res.detail}")
            self.log(f"  clicked {what} at ({cx}, {cy})")
            return True

        if not self.dry_run:
            res = self.input.click(open_pt[0], open_pt[1])
            if not res.ok:
                raise EngineError(f"input failed opening the equipment screen: "
                                  f"{res.detail}")
            self.log(f"  opened the equipment screen at ({open_pt[0]}, {open_pt[1]})")
            time.sleep(settle)
        else:
            self.log(f"  [dry-run] would open the equipment screen at {open_pt}")

        # The screen's own 切换 button must be pressed first: it reveals the
        # group/suit tree.  Without this the currently equipped suit's *title*
        # at the top of the screen gets mistaken for a list entry - exactly the
        # wrong-click this flow hit before the switch step was added.
        sw = settings.get("switch_point") or [459, 204]
        sw = [int(sw[0]), int(sw[1])]
        if not self.dry_run:
            res = self.input.click(sw[0], sw[1])
            if not res.ok:
                raise EngineError(f"input failed opening the loadout list: {res.detail}")
            self.log(f"  clicked the 切换 button at ({sw[0]}, {sw[1]})")
            time.sleep(settle)
        else:
            self.log(f"  [dry-run] would click 切换 at {sw}")

        # The group is a toggle; only tap it when no suit entry is on screen.
        for attempt in range(1, 4):
            frame = self.frame(force=True)
            hits = _texts(frame)
            if not self.dry_run and _pick(hits, list_marker) is None and group:
                g = _pick(hits, group)
                if g is None:
                    seen = [t for t, s, _ in hits if s >= conf][:12]
                    path = self.recorder.frame(frame, "equip_no_group")
                    raise EngineError(
                        f"neither '{list_marker}' nor '{group}' is on screen "
                        f"(saw {seen})" + (f" (frame saved to {path})" if path else "")
                    )
                self.log(f"  list is collapsed; tapping '{group}' to expand it")
                _click(g, f"'{group}'")
                time.sleep(settle)
                continue
            break

        if suit:
            frame = self.frame(force=True)
            # Inside the opened list the entries sit in the LEFT column (the
            # group tree), unlike the title at the top of the screen, so the
            # match must be constrained to the panel area below the header.
            s = _pick(_texts(frame), suit)
            if s is not None and s[2][1] < int(settings.get("list_min_y", 150)):
                s = None
            if s is None:
                path = self.recorder.frame(frame, "equip_no_suit")
                seen = [t for t, sc, _ in _texts(frame) if sc >= conf][:14]
                raise EngineError(
                    f"suit '{suit}' not found in the opened list "
                    f"(saw {seen})" + (f" (frame saved to {path})" if path else "")
                )
            _click(s, f"'{suit}'")
            if self.dry_run:
                return
            time.sleep(settle)

        frame = self.frame(force=True)
        a = _pick(_texts(frame), apply_text)
        if a is None:
            path = self.recorder.frame(frame, "equip_no_apply")
            raise EngineError(
                f"'{apply_text}' not found" + (f" (frame saved to {path})" if path else "")
            )
        _click(a, f"'{apply_text}'")
        if self.dry_run:
            return
        time.sleep(settle)
        self.log(f"  loadout '{suit or group}' applied")

    def _act_resonance_refresh(self, action: dict, tick: Tick, depth: int = 0) -> None:
        """Refresh the resonance shop until a wanted item shows up, then buy it.

        ``watchlist`` is the list of equipment names you are hunting for.  The
        loop, each round:

        1. read all six slots (name + 已拥有/未拥有 + the 兑换 button position);
        2. if a wanted item is present **and not owned**, click its 兑换 and
           report it - the mechanism repeats until the refresh budget runs out,
           so it keeps hunting after a successful buy;
        3. otherwise click refresh and verify ``剩余次数`` dropped by one.

        Verification matters here because a refresh that silently fails would
        make the loop spin forever on the same six items, and a purchase that
        fails would be re-attempted endlessly.  Both are checked against what the
        game itself reports.  Set ``dry_run`` per the engine; the action honours
        the global ``--dry-run`` too.
        """
        from .resonance import (
            load_watchlist, read_currency, read_refresh_state, read_slots,
            watchlist_hits, watchlist_path,
        )

        # Battle list: an explicit "watchlist" in the config wins, otherwise the
        # editable watchlist.txt is read.  The txt file is the intended workflow
        # (edit it in Notepad between runs), so it is consulted whenever the
        # config does not hard-code names.
        watch = [str(w) for w in (action.get("watchlist") or []) if str(w).strip()]
        watch_file = action.get("watchlist_file")
        if not watch:
            watch = load_watchlist(watch_file)
            self.log(f"  watchlist: {len(watch)} name(s) from "
                     f"{watchlist_path(watch_file)}")
        else:
            self.log(f"  watchlist: {len(watch)} name(s) from the config")
        if not watch:
            raise EngineError(
                "no watchlist items. Add equipment names (one per line) to "
                f"{watchlist_path(watch_file)} - the file is plain text and is "
                f"re-read on every run."
            )
        refresh_xy = action.get("refresh_point")
        if not refresh_xy:
            raise EngineError("resonance_refresh needs 'refresh_point': [x, y]")
        refresh_xy = [int(refresh_xy[0]), int(refresh_xy[1])]
        max_refresh = int(action.get("max_refresh", 3))
        buy = bool(action.get("buy", True))
        settle = float(action.get("settle", 2.5))
        # Currency guard: an exchange costs a fixed amount, so checking the
        # balance first avoids a half-completed purchase or a stream of failed
        # clicks when the wallet is empty.
        currency_roi = action.get("currency_roi")
        currency_roi = tuple(int(v) for v in currency_roi) if currency_roi else None
        currency_min = int(action.get("currency_min", 0))
        engine = self._ocr_engine()

        def _currency(frame) -> int | None:
            return read_currency(frame, currency_roi, engine) if currency_roi else None

        bought: list[str] = []
        refreshes = 0

        for round_no in range(1, max_refresh + 1):
            frame = self.frame(force=True)
            state = read_refresh_state(frame, engine)
            slots = read_slots(frame, engine)
            mode = (f"free left={state['remaining']}" if state["remaining"] is not None
                    else f"cost={state['cost'] or '?'}")
            self.log(f"  round {round_no}: {len([s for s in slots if s.name])} slot(s), "
                     f"{mode}, {state['countdown']}")
            for s in slots:
                if s.name:
                    self.log(f"      {s.describe()}")

            wanted = [s for s in watchlist_hits(slots, watch) if not s.owned]
            if wanted and buy:
                target = wanted[0]
                self.log(f"  >>> TARGET FOUND: {target.name} (未拥有) at "
                         f"{target.exchange_point}")
                if self.dry_run:
                    self.log("  [dry-run] not buying")
                    return
                have = _currency(frame)
                if currency_roi and have is not None and have < currency_min:
                    self.log(f"  !! balance {have} < {currency_min}; not buying "
                             f"{target.name}")
                elif not target.exchange_point:
                    self.log("  !! no 兑换 button position for that slot; skipping buy")
                else:
                    if have is not None:
                        self.log(f"  balance {have} (>= {currency_min}): buying")
                    res = self.input.click(target.exchange_point[0],
                                           target.exchange_point[1])
                    if not res.ok:
                        raise EngineError(f"input failed: {res.detail}")
                    self.log(f"  clicked 兑换 for {target.name}")
                    time.sleep(settle)
                    self._dismiss_dialogs(settle)
                    bought.append(target.name)
                    self.recorder.frame(self.frame(force=True), f"bought_{target.name}")
                    # Buying does not consume a refresh, so do not count a round.
                    continue

            owned_match = [s for s in watchlist_hits(slots, watch) if s.owned]
            if owned_match:
                self.log(f"  (watchlist item present but already owned: "
                         f"{[s.name for s in owned_match]})")

            if refreshes >= max_refresh:
                self.log(f"  refresh budget reached ({max_refresh})")
                break
            if self.dry_run:
                self.log(f"  [dry-run] would click refresh at {refresh_xy}")
                return
            before = state["remaining"]
            res = self.input.click(refresh_xy[0], refresh_xy[1])
            if not res.ok:
                raise EngineError(f"input failed: {res.detail}")
            refreshes += 1
            self.log(f"  clicked refresh at {refresh_xy} (#{refreshes})")
            time.sleep(settle)
            self._dismiss_dialogs(settle)

            after_state = read_refresh_state(self.frame(force=True), engine)
            after = after_state["remaining"]
            if before is not None and after is not None and after >= before:
                path = self.recorder.frame(self.frame(), "refresh_not_consumed")
                raise EngineError(
                    f"refresh did not consume a use ({before} -> {after}); the "
                    f"button may have moved or the dialog blocked it"
                    + (f" (frame saved to {path})" if path else "")
                )
            self.log(f"  refresh verified ({before} -> {after})")

        self.log(f"  done: {refreshes} refresh(es), bought {len(bought)}: "
                 f"{bought if bought else 'nothing'}")

    def _ocr_engine(self):
        from .ocr import OcrEngine
        if getattr(self, "_ocr", None) is None:
            self._ocr = OcrEngine.shared()
        return self._ocr

    def _dismiss_dialogs(self, settle: float = 1.5, rounds: int = 3) -> int:
        """Click 确定/已知/etc. until nothing dialog-ish is left.

        Purchases and refreshes can raise confirmation and result dialogs; a
        stale dialog on screen makes every later OCR read the dialog's text
        instead of the shop's, which silently breaks the loop.
        """
        from .matcher import ocr_strings
        labels = ("确定", "确认", "知道了", "知道了。", "关闭")
        dismissed = 0
        for _ in range(rounds):
            frame = self.frame(force=True)
            try:
                hits = ocr_strings(frame)
            except Exception:  # noqa: BLE001
                return dismissed
            found = None
            for text, score, box in hits:
                if score < 0.85:
                    continue
                if any(text.strip() == lb for lb in labels):
                    found = (text.strip(), box)
                    break
            if found is None:
                return dismissed
            label, box = found
            cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
            self.log(f"  dismissing dialog via '{label}' at ({cx}, {cy})")
            self.input.click(cx, cy)
            dismissed += 1
            time.sleep(settle)
        return dismissed

    def _act_repeat_until(self, action: dict, tick: Tick, depth: int) -> None:
        """Repeat a step list until a recogniser stops matching.

        Built for lists with several identical controls (the familiar-expedition
        screen has one "点击领取奖励" per finished entry, and template matching
        only ever returns the topmost one).  Clicking the same recogniser over
        and over drains the list without hard-coding how many entries there are.

        Fields: ``until`` / ``while`` (recogniser that must still match for
        another round), ``steps``, ``max_rounds``, ``interval``, ``min_delay``,
        ``optional`` (do not fail when the first check already fails).
        """
        name = action.get("until") or action.get("while")
        if not name:
            raise EngineError("repeat_until needs 'until' (the recogniser to drain)")
        name = str(name)
        steps = action.get("steps") or []
        max_rounds = int(action.get("max_rounds", 20))
        interval = float(action.get("interval", 1.0))
        min_delay = float(action.get("min_delay", 0.0))
        optional = bool(action.get("optional", False))

        rounds = 0
        while rounds < max_rounds:
            frame = self.frame(force=True)
            res = self.recognize(name, frame)
            if not res.hit:
                if rounds == 0 and not optional:
                    raise EngineError(
                        f"repeat_until: {name!r} did not match at all "
                        f"(score {res.score:.3f}); nothing to drain"
                    )
                self.log(f"  repeat_until {name}: done after {rounds} round(s)")
                return
            rounds += 1
            sub = Tick(frame=frame, now=time.time(),
                       results={name: res}, last=res, task=tick.task)
            sub.results.update({k: v for k, v in tick.results.items() if k not in sub.results})
            self.log(f"  repeat_until {name}: round {rounds}/{max_rounds} "
                     f"(score {res.score:.3f})")
            self.run_actions(steps, sub, depth + 1)
            if self._stop_reason:
                return
            if min_delay:
                time.sleep(min_delay)
            if interval:
                time.sleep(interval)
        self.log(f"  repeat_until {name}: hit max_rounds={max_rounds}, stopping")

    def _act_repeat(self, action: dict, tick: Tick, depth: int) -> None:
        times = int(action.get("times", 1))
        interval = float(action.get("interval", 0.0))
        until = action.get("until")
        body = action.get("steps") or []
        for i in range(max(0, times)):
            sub_tick = Tick(frame=self.frame(force=True), now=time.time(),
                            results=dict(tick.results), last=tick.last)
            self.run_actions(body, sub_tick, depth + 1)
            if self._stop_reason:
                return
            if until:
                frame = self.frame(force=True)
                names = until.get("any") or until.get("recognizer") or []
                if isinstance(names, str):
                    names = [names]
                if any(self.recognize(str(n), frame).hit for n in names):
                    return
            if interval:
                time.sleep(interval)

    # -- tasks -------------------------------------------------------------

    def task(self, name: str) -> dict:
        try:
            return self.config.tasks[name]
        except KeyError as exc:
            raise EngineError(f"unknown task {name!r}") from exc

    def run_task(self, name: str) -> None:
        """Run a single task once: check triggers, then run the actions."""
        task = self.task(name)
        triggers = [_build_trigger(name, spec) for spec in (task.get("triggers") or [])]
        if not triggers:
            # No trigger: unconditional task (a script, not a reaction).
            frame = self.frame(force=True)
            tick = Tick(frame=frame, now=time.time(), task=name)
            self.log(f"[task] {name} (unconditional)")
            self.run_actions(task.get("actions") or [], tick)
            return
        for trig in triggers:
            ok, tick = self.evaluate(trig)
            if ok:
                tick.task = name
                trig.mark(tick.now)
                self._task_counts[name] = self._task_counts.get(name, 0) + 1
                self.log(f"[task] {name} <- {trig.describe()}")
                self.run_actions(task.get("actions") or [], tick)
                self._misses = 0
                return
        self._misses += 1
        if self._misses >= self.miss_limit:
            frame = self.frame()
            path = self.recorder.frame(frame, f"stuck_before_{name}")
            raise EngineError(
                f"{self._misses} consecutive ticks without {name!r} matching; "
                f"check templates/ROI/threshold"
                + (f" (frame saved to {path})" if path else "")
            )

    def run(self) -> str:
        """Run the configured mode: ``once`` | ``loop`` | ``events``."""
        mode = str(self.config.data.get("mode", "events")).lower()
        try:
            if mode == "once":
                self._run_once()
            elif mode == "loop":
                self._run_loop()
            else:
                self._run_events()
        finally:
            # 只关闭**自己创建**的资源。
            # 调用方经常把已经打开的 backend 传进来复用(例如 GUI 的全流程
            # 里多个任务共用一个 AdbBackend),此时 Engine 关掉它会让后端
            # 的 _controller 被删除,后续任务全部 AttributeError —— 实测踩过。
            if getattr(self, "_owns_input", True):
                self.input.close()
            if getattr(self, "_owns_source", True):
                self.source.close()
        return self._stop_reason or "finished"

    def _run_once(self) -> None:
        steps = self.config.data.get("steps")
        if not steps:
            raise EngineError("mode=once needs a top-level 'steps' list")
        frame = self.frame(force=True)
        tick = Tick(frame=frame, now=time.time(), task="once")
        self.run_actions(steps, tick)

    def _run_loop(self) -> None:
        sequence = self.config.loop_cfg.get("sequence") or []
        if not sequence:
            raise EngineError("mode=loop needs loop.sequence")
        rounds = int(self.config.loop_cfg.get("rounds", 0))  # 0 = forever
        while True:
            for name in sequence:
                self.run_task(str(name))
                if self._stop_reason:
                    return
            rounds -= 1
            if rounds == 0:
                return
            delay = self.config.loop_cfg.get("between_rounds", 0)
            if delay:
                time.sleep(float(delay))

    def _run_events(self) -> None:
        """Poll every task in priority order; first matching task wins."""
        order = self.config.data.get("priority") or list(self.config.tasks)
        unknown = [n for n in order if n not in self.config.tasks]
        if unknown:
            raise EngineError(f"priority lists unknown tasks: {unknown}")
        triggers = {n: [_build_trigger(n, s) for s in (self.task(n).get("triggers") or [])]
                    for n in order}
        if not any(triggers.values()):
            raise EngineError("mode=events needs at least one task with triggers")
        idle = float(self.config.loop_cfg.get("idle_sleep", 0.03))
        while True:
            self.ticks += 1
            if self.max_ticks and self.ticks > self.max_ticks:
                self._stop_reason = f"max_ticks={self.max_ticks} reached"
                return
            if self.config.data.get("max_seconds"):
                if time.time() - self.started > float(self.config.data["max_seconds"]):
                    self._stop_reason = "max_seconds reached"
                    return
            frame = self.frame(force=True)
            now = time.time()
            self.recorder.maybe_periodic(frame, now, "tick")
            fired = False
            for name in order:
                for trig in triggers[name]:
                    if not trig.ready(now):
                        continue
                    tick = Tick(frame=frame, now=now, task=name)
                    for nm in trig.recognizers:
                        tick.results[nm] = self.recognize(nm, frame)
                    hits = [r for r in tick.results.values() if r.hit]
                    if trig.mode == "all":
                        ok = len(hits) == len(tick.results) and bool(hits)
                    elif trig.mode == "absent-only":
                        ok = not tick.results
                    else:
                        ok = bool(hits)
                    if ok and trig.absent:
                        # Stacked UI: a nav bar visible behind a modal must not
                        # count as "the modal is gone".
                        for nm in trig.absent:
                            blocker = self.recognize(nm, frame)
                            tick.results[f"absent:{nm}"] = blocker
                            if blocker.hit:
                                ok = False
                    if not ok:
                        continue
                    if hits:
                        tick.last = max(hits, key=lambda r: r.score)
                    elif tick.results:
                        tick.last = max(tick.results.values(), key=lambda r: r.score)
                    trig.mark(now)
                    fired = True
                    self._misses = 0
                    self._task_counts[name] = self._task_counts.get(name, 0) + 1
                    self.log(f"[{self.ticks}] {name} <- {tick.last.describe()}")
                    self.run_actions(self.task(name).get("actions") or [], tick)
                    if self._stop_reason:
                        return
                    break
                if fired:
                    break
            self._misses = 0 if fired else self._misses + 1
            if self._misses >= self.miss_limit:
                path = self.recorder.frame(frame, "stuck_idle")
                self.log(f"!! nothing matched for {self._misses} ticks"
                         + (f"; frame saved to {path}" if path else ""))
                self._stop_reason = "idle (no trigger matched)"
                return
            time.sleep(max(0.0, idle))

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict:
        capture = self.source.capture
        return {
            "ticks": self.ticks,
            "clicks": self.input.clicks,
            "captures": self.source.frames,
            "capture_ms": round(float(getattr(capture, "last_capture_ms", 0.0)), 2),
            "backend": getattr(capture, "name", "gdi"),
            "capture_method": getattr(capture, "mode", None) or (
                "maafw:" + str(getattr(capture, "screencap_mask", "")) if self.source.is_maa else None
            ),
            "tasks": dict(self._task_counts),
            "elapsed": round(time.time() - self.started, 1),
            "window": self.source.window.describe(),
            "input_method": getattr(self.input, "name", None) or self.input.method,
            "session_dir": self.recorder.session if self.recorder.enabled else None,
        }
