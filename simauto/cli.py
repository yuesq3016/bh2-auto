"""Command line interface.

Every subcommand exists to answer one question during calibration:

``list``     which window handle is my emulator?
``doctor``   is this machine/environment able to run this at all?
``probe``    which capture backend actually returns pixels for that window?
``grab``     what does the bot see right now?
``crop``     this rectangle on screen looks like the attack button — cut it out.
``match``    does that template match reliably, and what threshold is safe?
``run``      run the task file.
``validate`` is my task file sane?
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import numpy as np

from . import __version__
from .engine import Config, Engine, EngineError, FrameSource, load_config
from .imaging import (
    ImageError, crop_roi, load_image, parse_scale_list, save_image, to_gray,
)
from .maa_backend import (
    MaaBackend, MaaBackendUnavailable, describe_screencap_mask, maa_available,
    probe_screencap_methods, verify_background_capture,
)
from .matcher import MatchResult, run_detector, suggest_threshold
from .scanner import PROBE_MODES, WindowCapture, list_windows, probe_window, resolve_window


# --------------------------------------------------------------------------
# Pretty output
# --------------------------------------------------------------------------

def _c(text: str, colour: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    codes = {"red": 31, "green": 32, "yellow": 33, "blue": 34, "grey": 90}
    return f"\033[{codes.get(colour, 0)}m{text}\033[0m"


def _ok(msg: str) -> None:
    print(f"{_c('[ok]', 'green')} {msg}")


def _warn(msg: str) -> None:
    print(f"{_c('[warn]', 'yellow')} {msg}")


def _fail(msg: str) -> None:
    print(f"{_c('[fail]', 'red')} {msg}")


def _info(msg: str) -> None:
    print(f"{_c('[info]', 'blue')} {msg}")


def _window_arg(args) -> dict:
    spec: dict[str, Any] = {}
    if getattr(args, "hwnd", None):
        spec["hwnd"] = args.hwnd
    if getattr(args, "title", None):
        spec["title"] = args.title
    if getattr(args, "class_name", None):
        spec["class"] = args.class_name
    if getattr(args, "regex", False):
        spec["regex"] = True
    if getattr(args, "index", 0):
        spec["index"] = args.index
    return spec


class _DeviceWindowStub:
    """Minimal stand-in for window info when the ADB backend is in use.

    ADB capture has no window, but a lot of reporting code expects something
    whose ``describe()`` can be printed.
    """

    def __init__(self, address: str):
        self.address = address
        self.hwnd = 0
        self.title = f"ADB device {address}"
        self.class_name = ""
        self.pid = 0
        self.rect = (0, 0, 0, 0)
        self.client_size = (0, 0)
        self.visible = True
        self.iconic = False
        self.process_name = "adb"

    def describe(self) -> str:
        return self.title


def _open_capture(args):
    """Open whichever capture backend the user asked for (or the best one).

    Returns ``(window_info, backend)``.  ``maafw``/``adb`` are only attempted
    when they are importable/reachable, so the CLI keeps working with zero
    third-party packages.

    Note that ``adb`` needs no window at all - it talks to the device - so the
    window argument is optional for it.
    """
    # Commands with no window arguments at all (resonance, adb) default to the
    # ADB backend: requiring an explicit --backend adb there is pointless
    # ceremony, and an empty window spec would just fail.
    window_capable = any(hasattr(args, a) for a in ("title", "hwnd", "class_name"))
    backend = (getattr(args, "backend", None) or ("auto" if window_capable else "adb")).lower()

    if backend in ("adb", "android"):
        from .adb_backend import AdbBackend, AdbBackendUnavailable
        try:
            cap = AdbBackend(
                adb_path=getattr(args, "adb_path", None),
                address=getattr(args, "address", None),
                screencap_methods=getattr(args, "screencap", None),
                input_methods=getattr(args, "adb_input", None),
            )
        except (AdbBackendUnavailable, ValueError) as exc:
            _fail(f"ADB backend unavailable: {exc}")
            raise SystemExit(2)
        _info(f"capture backend: adb, device {cap.device.describe()}")
        _info(f"  screencap={cap.screencap_label[0]} input={cap.input_label[0]} "
              f"resolution={cap.client_size[0]}x{cap.client_size[1]}")
        _ok("ADB capture works while the emulator window is covered or minimised "
            "(at the cost of ~0.5 s per frame)")
        return _DeviceWindowStub(cap.device.address), cap

    # Only resolve a window when the chosen backend actually needs one; ADB only
    # talks to the device, and demanding a title there was a needless failure.
    if backend in ("adb", "android"):
        window = None
    else:
        window = resolve_window(_window_arg(args))

    if getattr(args, "focus", False) and window is not None:
        from .win32 import set_foreground_window
        if set_foreground_window(window.hwnd):
            _info("activated the window")
            time.sleep(0.35)  # give the compositor a moment to draw it on top
        else:
            _warn("could not activate the window; screen-reading methods may see "
                  "whatever is on top instead")

    if backend in ("maafw", "maa", "maaframework"):
        if not maa_available():
            _fail("maafw is not installed; run `python -m pip install maafw` "
                  "or `tools\\setup-maafw.ps1` (or use --backend gdi)")
            raise SystemExit(2)
        try:
            cap = MaaBackend(
                window.hwnd,
                screencap=getattr(args, "screencap", None),
                mouse_method=getattr(args, "mouse", None),
            )
        except (MaaBackendUnavailable, ValueError) as exc:
            _fail(str(exc))
            raise SystemExit(2)
        for attempt in getattr(cap, "attempts", []):
            print(f"       tried {attempt}")
        _info(f"capture backend: maafw, selected {cap.screencap_label} "
              f"({cap.probe_shape[0]}x{cap.probe_shape[1]})")
        if cap.screencap_label not in ("Background", "FramePool", "PrintWindow"):
            _warn("the selected method reads the screen, so the emulator window must "
                  "stay visible and unobstructed (background capture was unavailable)")
        return window, cap

    if backend == "auto" and maa_available():
        try:
            cap = MaaBackend(window.hwnd, screencap=getattr(args, "screencap", None),
                             mouse_method=getattr(args, "mouse", None))
            for attempt in getattr(cap, "attempts", []):
                print(f"       tried {attempt}")
            _info(f"capture backend: maafw, selected {cap.screencap_label} "
                  f"({cap.probe_shape[0]}x{cap.probe_shape[1]}); "
                  f"use --backend gdi to force GDI")
            if cap.screencap_label not in ("Background", "FramePool", "PrintWindow"):
                _warn("background capture unavailable for this window; the selected "
                      "method reads the screen, so keep the window visible")
            return window, cap
        except (MaaBackendUnavailable, ValueError) as exc:
            _warn(f"maafw unusable ({exc}); using built-in GDI")
    elif backend == "auto":
        _info("maafw not installed; using built-in GDI (pip install maafw for "
              "FramePool/PrintWindow capture)")

    mode = getattr(args, "method", "auto")
    if mode == "auto":
        chosen = None
        for entry in probe_window(window.hwnd):
            if entry.get("likely_usable"):
                chosen = entry["mode"]
                break
        if not chosen:
            _fail(f"no GDI backend returned a usable frame for {window.describe()}")
            _info("run `probe` to read the per-backend stats, install maafw, or switch "
                  "the emulator to its compatibility/software renderer")
            raise SystemExit(2)
        _info(f"auto-selected GDI capture method: {chosen}")
        mode = chosen
    cap = WindowCapture(window.hwnd, mode=mode).open()
    return window, cap


# --------------------------------------------------------------------------
# find
# --------------------------------------------------------------------------

# Process-name hints for the emulators people actually run on Windows.  Matching
# on the *process* rather than the window title matters: emulator titles are
# localised ("MuMu安卓设备") and change between versions, while the process name
# is stable - and it avoids the long, wrap-prone `--title ...` argument.
_EMULATOR_PROCESS_HINTS = (
    "mumunxdevice", "mumuplayer", "nemuplayer", "mumuglobal",
    "dnplayer", "ldplayer", "ld9box",
    "nox", "nemu", "bluestacks", "hd-player", "bstksvc",
    "memu", "memon", "androws", "gameloop", "aow_exe",
)


def _looks_like_emulator(info) -> str | None:
    proc = info.process_name.lower()
    for hint in _EMULATOR_PROCESS_HINTS:
        if hint in proc:
            return hint
    return None


# Class names that emulators use for the window that actually renders the
# guest screen.  Community-sourced (BlueStacksApp, LDPlayer's subWin/RenderWindow,
# MuMuPlayer/...); version-dependent, so this only *bonuses* candidates rather
# than filtering them.
_RENDER_WINDOW_CLASS_HINTS = (
    "mumuplayer", "nemuplayer", "subwin", "renderwindow", "bluestacksapp",
    "qt5qwindowicon", "qt5156qwindowtoolsavebits", "nemu", "memu", "androws",
)

# Windows that must never be picked: GPU driver helper windows and thumbnail
# stubs.  Probing them "works" (they answer PrintWindow with a stale copy of the
# desktop) but controlling them is meaningless.
_DUMMY_WINDOW_CLASS_HINTS = (
    "nvopenglpbuffer", "mumunativewindow", "mumuthumbnailwindow",
    "tooltips_class32", "shell_traywnd", "progman", "workerw",
)


def _window_score(info, process_hint: str | None) -> tuple[int, list[str]]:
    """Rank a window as a capture target.  Higher is better."""
    score = 0
    why: list[str] = []
    cls = info.class_name.lower()
    title = info.title.lower()

    if process_hint:
        score += 100
        why.append(f"process:{process_hint}")
    if info.visible and not info.iconic:
        score += 50
        why.append("visible")
    else:
        score -= 60
        why.append("hidden")
    if any(h in cls for h in _RENDER_WINDOW_CLASS_HINTS):
        score += 40
        why.append("render-class")
    if any(h in cls for h in _DUMMY_WINDOW_CLASS_HINTS) or "invisible" in title:
        score -= 200
        why.append("driver/dummy")
    # A window at least the size of a phone screen is a plausible target; a
    # 300x200 stub is not.
    if info.client_size[0] >= 480 and info.client_size[1] >= 320:
        score += 10
        why.append("plausible-size")
    else:
        score -= 30
    return score, why


def cmd_find(args) -> int:
    """Locate the emulator, then test capture backends and report what works.

    Exists so the user never has to type a long, terminal-wrapping
    ``--title 'MuMu安卓设备'`` argument: it enumerates windows, ranks the ones
    that look like emulators, and probes the best candidate.
    """
    infos = list_windows(visible_only=False, min_size=200)
    ranked = []
    for info in infos:
        hint = _looks_like_emulator(info)
        if not hint and not args.all:
            continue
        score, why = _window_score(info, hint)
        ranked.append((score, info, hint, why))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked:
        _warn("no emulator-looking window found (process-name heuristics).")
        _info("Is the emulator running? Check with `simauto list --min-size 200`.")
        _info("You can still point at any window yourself: `simauto probe --index 0`.")
        return 1

    usable = [r for r in ranked if r[0] > 0]
    if not usable:
        _warn("the emulator process is running, but every window it owns is hidden "
              "or is a driver helper window.")
        _info("That usually means the emulator is minimised to the tray or closed to "
              "the background. Restore it, then run this again.")
        print()
        for score, info, hint, why in ranked[:6]:
            print(f"  [{score:>5}] {info.describe()}")
            print(f"          {_c(', '.join(why), 'grey')}")
        return 1

    print("candidate windows (best first):")
    for i, (score, info, hint, why) in enumerate(usable[:args.limit]):
        print(f"  [{i}] score={score:<5} {info.describe()}")
        print(f"          {_c(', '.join(why), 'grey')}")
    index = min(args.index, len(usable) - 1)
    pick = usable[index][1]
    print()
    _info(f"probing [{index}] {pick.title!r}")
    return _probe_and_report(pick, backend=args.backend, out_dir=args.out)


def _probe_and_report(window, *, backend: str = "auto", out_dir: str | None = None) -> int:
    """Shared body of probe/find: try backends, print a verdict and a snippet."""
    best = None
    best_backend = None
    out_dir = out_dir or os.path.join("sessions", "probe")

    if backend in ("auto", "maafw", "maa", "maaframework"):
        if maa_available():
            _info("MaaFramework Win32Controller screencap methods:")
            for entry in probe_screencap_methods(window.hwnd):
                head = f"maafw:{entry['method']:<20}"
                if entry.get("error"):
                    print(f"  {head} {_c('ERROR', 'red')} {entry['error']}")
                    continue
                stats = entry["stats"]
                verdict = (_c("usable", "green") if entry.get("likely_usable")
                           else _c("black/blank", "red"))
                print(f"  {head} {verdict:<12} {entry['capture_ms']:>6.1f} ms  "
                      f"size={entry['size'][0]}x{entry['size'][1]}  "
                      f"mean={stats['mean']} detail={stats['detail']} "
                      f"nonblack={stats['nonblack_ratio']}")
                if entry.get("likely_usable") and best is None:
                    best, best_backend = entry["method"], "maafw"
            print()
        elif backend != "auto":
            _fail("maafw is not installed; run `python -m pip install maafw`")
            return 2
        else:
            _info("maafw not installed; skipping MaaFramework methods "
                  "(pip install maafw to test FramePool/PrintWindow)")

    if backend in ("auto", "gdi", "builtin", "win32"):
        _info("built-in GDI backends:")
        for entry in probe_window(window.hwnd, out_dir=out_dir, modes=PROBE_MODES):
            head = f"gdi:{entry['mode']:<22}"
            if entry.get("error"):
                print(f"  {head} {_c('ERROR', 'red')} {entry['error']}")
                continue
            stats = entry["stats"]
            verdict = (_c("usable", "green") if entry.get("likely_usable")
                       else _c("black/blank", "red"))
            print(f"  {head} {verdict:<12} {entry['capture_ms']:>6.1f} ms  "
                  f"size={entry['size'][0]}x{entry['size'][1]}  "
                  f"mean={stats['mean']} detail={stats['detail']} "
                  f"nonblack={stats['nonblack_ratio']}")
            print(f"  {'':30} {_c(entry['note'], 'grey')}")
            if entry.get("likely_usable") and best is None:
                best, best_backend = entry["mode"], "gdi"

    print()
    if best and best_backend == "maafw":
        _ok(f"recommended: capture.backend = \"maafw\", capture.screencap = \"{best}\"")
        if best in ("FramePool", "PrintWindow", "Background"):
            _ok("this is a BACKGROUND method: the window may be covered while running")
        else:
            _warn("this method reads the screen: keep the emulator visible and on top")
        return 0
    if best:
        _ok(f"recommended: capture.backend = \"gdi\", capture.method = \"{best}\"")
        if best == "screendc":
            _warn("screendc reads the screen: keep the emulator visible and on top")
        return 0
    _fail("no backend produced a non-black frame.")
    _info("in order of success rate:")
    _info("  1. python -m pip install maafw, then re-run this (FramePool)")
    _info("  2. emulator settings -> compatibility / software rendering (DirectX -> OpenGL)")
    _info("  3. keep the emulator visible and unobstructed (screendc)")
    _info("  4. try a child render handle with `simauto tree`, then probe that --hwnd")
    return 1


def _cmd_verify(args) -> int:
    """Answer: is background capture actually available for this window?"""
    window = resolve_window(_window_arg(args))
    if not maa_available():
        _fail("maafw is not installed; background verification needs it "
              "(pip install maafw, or run tools\\setup-maafw.ps1)")
        return 2
    _info(f"verifying background capture for {window.describe()}")
    _info("comparing each method against two independent screen reads; a desktop "
          "copy agrees with both, a real window capture with neither")
    results = verify_background_capture(
        window.hwnd, rounds=args.rounds, threshold=args.threshold
    )
    print()
    verdicts = []
    for entry in results:
        if entry.get("error"):
            print(f"  {entry['method']:<14} {_c('ERROR', 'red')} {entry['error']}")
            continue
        capable = entry.get("background_capable")
        mark = _c("BACKGROUND OK", "green") if capable else _c("desktop copy", "yellow")
        print(f"  {entry['method']:<14} {mark:<14} "
              f"diff_vs_screen={entry['diff_vs_screen_method']:>7.2f}  "
              f"diff_vs_desktop={entry['diff_vs_desktop']:>7.2f}  "
              f"({entry['rounds']} rounds)")
        verdicts.append(capable)
    print()
    if any(verdicts):
        _ok("background capture WORKS - the window may be covered while running")
        good = [e["method"] for e in results if e.get("background_capable")]
        _info(f'config: {{"capture": {{"backend": "maafw", "screencap": "{good[0].lower()}"}}}}')
        return 0
    if verdicts:
        _warn("every method returned a copy of the desktop: background capture is NOT "
              "available for this window right now")
        _info("most likely the window is minimised or covered - restore it, keep it "
              "visible, and re-run this")
        _info("this is also what happens when the emulator is pseudo-minimised")
        return 1
    _fail("could not verify: no method returned a frame")
    return 1


# --------------------------------------------------------------------------
# adb
# --------------------------------------------------------------------------

def cmd_adb(args) -> int:
    """List ADB devices and measure the ADB capture path."""
    from .adb_backend import (
        AdbBackend, AdbBackendUnavailable, default_adb_path, list_adb_devices,
    )

    adb = args.adb_path or default_adb_path()
    if not adb:
        _fail("no adb.exe found. MuMu ships one at "
              r"D:\MuMuPlayer\nx_device\<ver>\shell\adb.exe")
        _info("pass --adb-path, or install Android platform-tools")
        return 2
    _info(f"adb: {adb}")
    devices = list_adb_devices(adb)
    if not devices:
        _fail("no devices. Start the emulator, then re-run (`adb connect` is tried "
              "automatically for the known emulator ports)")
    else:
        _ok(f"{len(devices)} device(s):")
        for d in devices:
            print(f"     {d.describe()}")
    if args.list:
        return 0 if devices else 1

    try:
        backend = AdbBackend(adb_path=adb, address=args.address,
                             screencap_methods=args.screencap)
    except (AdbBackendUnavailable, ValueError) as exc:
        _fail(str(exc))
        return 2

    try:
        _ok(f"connected: {backend.device.describe()}")
        print(f"     screencap = {backend.screencap_label[0]}"
              f"   input = {backend.input_label[0]}")
        times = []
        frame = None
        for _ in range(max(1, args.times)):
            t0 = time.perf_counter()
            frame = backend.grab()
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        h, w = frame.shape[:2]
        _ok(f"{w}x{h}  min {times[0]:.0f} ms  median {times[len(times)//2]:.0f} ms  "
            f"max {times[-1]:.0f} ms  -> ~{1000/times[len(times)//2]:.1f} fps")
        print(f"     mean brightness {frame.mean():.1f}")
        out = args.out or os.path.join("sessions", "adb", "adb-frame.png")
        save_image(out, frame)
        _ok(f"sample frame -> {out}")
        _info("ADB capture is NOT affected by the emulator window being covered or "
              "minimised - that is its whole advantage over window capture.")
        _warn(f"but it costs ~{times[len(times)//2]:.0f} ms per frame. For frame-accurate "
              f"play use --backend maafw or gdi (window capture, ~33 ms) and keep the "
              f"emulator visible.")
    except Exception as exc:  # noqa: BLE001
        _fail(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        backend.close()
    return 0


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------

def cmd_preview(args) -> int:
    """Show every cropped template so the user can eyeball them.

    Templates are crops of screenshots, so the only way to be sure one is right
    is to look at it.  Two outputs: a compact contact sheet PNG (scaled so it is
    readable at a glance) and an HTML gallery with the real pixel sizes, which
    is what makes a wrong crop obvious.
    """
    tdir = args.dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "templates")
    if not os.path.isdir(tdir):
        _fail(f"no template directory: {tdir}")
        return 1

    names = sorted(n for n in os.listdir(tdir)
                   if n.lower().endswith((".png", ".bmp", ".jpg", ".jpeg")))
    if args.filter:
        names = [n for n in names if args.filter.lower() in n.lower()]
    if not names:
        _warn(f"no template images in {tdir}")
        return 1

    cells = []
    for name in names:
        path = os.path.join(tdir, name)
        img = load_image(path)
        h, w = img.shape[:2]
        grey = img.astype(np.float32).mean(axis=2)
        cells.append({
            "name": name, "path": path, "w": w, "h": h,
            "std": round(float(grey.std()), 2),
            "mean": round(float(grey.mean()), 1),
            "img": img,
        })

    print(f"{len(cells)} template(s) in {tdir}\n")
    print(f"{'name':<26}{'size':>11}{'std':>9}{'mean':>8}  verdict")
    print("-" * 74)
    problems = 0
    for c in cells:
        # Same rule the matcher enforces: a low-contrast template is useless.
        if c["std"] < 4.0:
            verdict = _c("TOO FLAT - will be rejected by the matcher", "red")
            problems += 1
        elif c["std"] < 12.0:
            verdict = _c("low contrast, may misfire", "yellow")
        elif c["w"] < 16 or c["h"] < 16:
            verdict = _c("very small, noisy", "yellow")
        else:
            verdict = _c("ok", "green")
        print(f"{c['name']:<26}{c['w']:>5}x{c['h']:<5}{c['std']:>9.2f}{c['mean']:>8.1f}  {verdict}")

    sheet_path = args.out or os.path.join(os.path.dirname(tdir), "sessions", "templates-sheet.png")
    html_path = os.path.splitext(sheet_path)[0] + ".html"
    _contact_sheet(cells, sheet_path, cell=args.cell)
    _template_gallery_html(cells, html_path)
    print()
    _ok(f"contact sheet -> {sheet_path}")
    _ok(f"gallery (real pixel sizes) -> {html_path}")
    _info("open the gallery with:  start " + os.path.abspath(html_path))
    if problems:
        _warn(f"{problems} template(s) are too flat to match reliably - re-crop them "
              f"from an area with visible detail, or use method=color")
    return 0


def _contact_sheet(cells: list[dict], out_path: str, cell: int = 96) -> None:
    """Tile every template into one image, centred on a neutral background."""
    cols = min(6, max(1, len(cells)))
    rows = (len(cells) + cols - 1) // cols
    pad = 6
    sheet = np.full((rows * (cell + pad) + pad, cols * (cell + pad) + pad, 3),
                    40, dtype=np.uint8)
    for i, c in enumerate(cells):
        r, col = divmod(i, cols)
        y0 = pad + r * (cell + pad)
        x0 = pad + col * (cell + pad)
        img = c["img"]
        h, w = img.shape[:2]
        # Nearest-neighbour fit, preserving the template's own aspect ratio so a
        # wrongly-shaped crop is visible immediately.
        scale = min(cell / max(w, 1), cell / max(h, 1))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        ys = np.clip((np.arange(nh) / scale).astype(np.int64), 0, h - 1)
        xs = np.clip((np.arange(nw) / scale).astype(np.int64), 0, w - 1)
        small = img[ys][:, xs]
        oy = y0 + (cell - nh) // 2
        ox = x0 + (cell - nw) // 2
        sheet[oy:oy + nh, ox:ox + nw] = small
        sheet[y0 - 1:y0, x0:x0 + cell] = 90      # frame
        sheet[y0 + cell:y0 + cell + 1, x0:x0 + cell] = 90
    save_image(out_path, sheet)


def _template_gallery_html(cells: list[dict], out_path: str) -> None:
    """Write an HTML page showing each template at its true size."""
    import html as _html

    rows = []
    for c in cells:
        # Inline the image so the page works anywhere and needs no server.
        import base64
        with open(c["path"], "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        ext = os.path.splitext(c["name"])[1].lstrip(".").lower() or "png"
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        flag = ""
        if c["std"] < 4.0:
            flag = ' class="flat"'
        elif c["std"] < 12.0:
            flag = ' class="low"'
        rows.append(
            f'<tr{flag}><td class="n">{_html.escape(c["name"])}</td>'
            f'<td class="s">{c["w"]}&times;{c["h"]}</td>'
            f'<td class="s">{c["std"]}</td>'
            f'<td class="i"><img src="data:{mime};base64,{b64}" '
            f'alt="{_html.escape(c["name"])}"></td></tr>'
        )
    doc = f"""<!doctype html>
<meta charset="utf-8">
<title>simauto templates</title>
<style>
 body {{ background:#1b1b1f; color:#e8e8ea; font:14px/1.5 "Segoe UI",system-ui,sans-serif; margin:24px; }}
 h1 {{ font-size:18px; font-weight:600; margin:0 0 4px; }}
 p.hint {{ color:#9a9aa2; margin:0 0 18px; }}
 table {{ border-collapse:collapse; }}
 th,td {{ padding:6px 12px; border-bottom:1px solid #2e2e34; vertical-align:middle; }}
 th {{ text-align:left; color:#9a9aa2; font-weight:500; }}
 td.n {{ font-family:Consolas,monospace; }}
 td.s {{ color:#9a9aa2; text-align:right; }}
 td.i {{ background:#26262b; }}
 td.i img {{ image-rendering:pixelated; display:block; max-width:420px; }}
 tr.flat td.n {{ color:#ff7b72; }}
 tr.low  td.n {{ color:#e3b341; }}
 tr.flat td.i, tr.low td.i {{ outline:1px solid #444; }}
</style>
<h1>simauto templates &mdash; {len(cells)} file(s)</h1>
<p class="hint">Shown at true pixel size. <b>&times;N</b> is the size the matcher will see.
Red name = too flat (std &lt; 4), the matcher rejects it. Amber = low contrast.</p>
<table>
<tr><th>name</th><th>size</th><th>std</th><th>image</th></tr>
{''.join(rows)}
</table>
"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)


# --------------------------------------------------------------------------
# ocr
# --------------------------------------------------------------------------

def cmd_ocr(args) -> int:
    """Read text off the screen (or an image) - the robust alternative to templates."""
    from .matcher import ocr_strings
    from .ocr import OcrEngine, default_model_dir, ocr_available

    if not ocr_available():
        _fail("OCR is unavailable: need the maafw binding plus det.onnx/rec.onnx/keys.txt")
        _info("MAAHG2 ships a usable model directory; point --model at it")
        return 2
    _info(f"OCR model: {args.model or default_model_dir()}")

    if args.frame:
        frame = load_image(args.frame)
        _info(f"image {args.frame} ({frame.shape[1]}x{frame.shape[0]})")
    else:
        from .adb_backend import AdbBackend, AdbBackendUnavailable
        try:
            backend = AdbBackend(adb_path=args.adb_path, address=args.address)
        except AdbBackendUnavailable as exc:
            _fail(str(exc))
            return 2
        try:
            frame = backend.grab()
        finally:
            backend.close()
        _ok(f"live screen {frame.shape[1]}x{frame.shape[0]} via ADB")

    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    if roi:
        _info(f"restricting to ROI {roi} (much faster than a full-frame pass)")

    engine = OcrEngine.shared(args.model)
    # Warm up so the reported timing is a real one, not the model load.
    engine.recognize(frame, roi)
    t0 = time.perf_counter()
    hits = ocr_strings(frame, roi)
    elapsed = (time.perf_counter() - t0) * 1000

    if not hits:
        _warn(f"no text recognised in {elapsed:.0f} ms")
        return 1
    print(f"\n{len(hits)} string(s) in {elapsed:.0f} ms"
          + (f" (avg {elapsed/len(hits):.0f} ms each)" if hits else ""))
    print(f"{'text':<34}{'conf':>6}   box  (click point)")
    print("-" * 78)
    for text, score, box in sorted(hits, key=lambda h: (h[2][1], h[2][0])):
        if args.min_conf and score < args.min_conf:
            continue
        cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
        print(f"{text:<34}{score:>6.3f}   {str(box):<22} ({cx}, {cy})")
    print()
    _info("use these as recognisers with  \"method\": \"ocr\", \"text\": [\"...\"]")
    _info("full-frame OCR costs seconds; give every OCR recogniser a tight roi")
    return 0


def cmd_gui(args) -> int:
    """启动图形界面(仿 MAA 前端)。"""
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        _fail(f"tkinter 不可用:{exc}")
        return 2
    return gui_main()


# --------------------------------------------------------------------------
# stagemap
# --------------------------------------------------------------------------

def cmd_stagemap(args) -> int:
    """Report (and optionally enter) stages whose first-clear reward is unclaimed.

    Detection is by the cyan crystal icon, not by OCR of the stage labels: those
    labels are small outlined text that OCR misses outright, while the icon is a
    solid colour blob worth ~350 pixels on an unclaimed node and 0 on a claimed
    one.
    """
    from .stagemap import find_first_clear_markers

    window, cap = _open_capture(args)
    try:
        frame = cap.grab()
        if getattr(args, "frame", None):
            frame = load_image(args.frame)
        markers = find_first_clear_markers(frame)
        if not markers:
            _info("没有检测到「首通奖励未领取」的关卡(本页)")
            return 1
        _ok(f"检测到 {len(markers)} 个未领取首通奖励的关卡:")
        for i, m in enumerate(markers):
            print(f"   [{i}] {m.describe()}")
        if getattr(args, "enter", False):
            t = markers[0]
            x, y = t.node_point
            print()
            _info(f"进入第 1 个:点节点 ({x}, {y})")
            if args.dry_run:
                _info("[dry-run] 未点击")
                return 0
            cap.click(x, y)
        return 0
    finally:
        cap.close()


# --------------------------------------------------------------------------
# resonance
# --------------------------------------------------------------------------

def cmd_resonance(args) -> int:
    """Read the resonance shop, then optionally refresh N times buying targets.

    Everything is overridable on the command line so the shop can be driven
    without editing a config: ``--refresh 3``, ``--buy/--no-buy``,
    ``--watchlist <file>``.
    """
    from .engine import Config
    from .resonance import (
        load_watchlist, read_currency, read_refresh_state, read_slots,
        watchlist_hits, watchlist_path,
    )

    window, cap = _open_capture(args)
    try:
        frame = cap.grab()
        slots = read_slots(frame, None)
        state = read_refresh_state(frame, None)
        names = load_watchlist(args.watchlist_file)

        print(f"{'slot':<6}{'name':<26}{'owned':<8}price")
        print("-" * 62)
        watch_hits = watchlist_hits(slots, names)
        for s in slots:
            mark = "YES" if any(s is w for w in watch_hits) else ""
            print(f"[{s.index}]   {s.name or '?':<24}{'已拥有' if s.owned else '未拥有':<8}"
                  f"{s.price:<8}{_c('  <== 名单中', 'yellow') if mark else ''}")
        print()
        currency = read_currency(frame)
        _info(f"共鸣币 {currency if currency is not None else '?'}   "
              f"剩余刷新 {state['remaining'] if state['remaining'] is not None else '0(已用完)'} 次   "
              f"{state['countdown'] or ''}")
        if state.get("cost"):
            _info(f"提示: 免费次数已用完,再刷新将 {state['cost']}")
        _info(f"名单文件 {watchlist_path(args.watchlist_file)}  ({len(names)} 项)")

        targets = [s for s in watch_hits if not s.owned]
        if watch_hits and not targets:
            _info(f"名单里的装备在场但已拥有: {[s.name for s in watch_hits]}")
        if targets:
            _ok(f"可购买目标: {[f'{s.name} @ {s.exchange_point}' for s in targets]}")
        else:
            _info("当前没有名单里且未拥有的装备")

        if not args.refresh:
            _info("未指定 --refresh N,只读不点。加 --refresh 3 让它刷新并自动购买。")
            return 0

        # Build an equivalent one-shot config and reuse the engine action, so
        # there is exactly one implementation of the refresh/buy logic.
        cfg = Config({
            "name": "resonance-cli",
            "mode": "once",
            # Say explicitly that this runs over ADB: FrameSource would otherwise
            # try to resolve a window from an empty spec and fail.
            "capture": {"backend": "adb", "address": args.address},
            "recognizers": {},
            "tasks": {"noop": {"actions": [{"action": "log", "message": "x"}]}},
            "steps": [{
                "action": "resonance_refresh",
                "watchlist_file": args.watchlist_file,
                "watchlist": names if args.watchlist_file else [],
                "refresh_point": [int(v) for v in args.refresh_point.split(",")],
                "max_refresh": int(args.refresh),
                "buy": not args.no_buy,
                "currency_min": int(args.currency_min),
                "currency_roi": [int(v) for v in args.currency_roi.split(",")],
                "settle": float(args.settle),
            }],
            "session_dir": "sessions",
        }, None)
        return _run_inline(cfg, args, cap, window)
    finally:
        cap.close()


def _run_inline(cfg, args, capture, window) -> int:
    """Run a programmatically built config against an already-open capture."""
    from .engine import Engine, FrameSource
    source = FrameSource(cfg, capture=capture, input_controller=capture, window=window)
    engine = Engine(cfg, frame_source=source, input_controller=capture,
                    dry_run=bool(getattr(args, "dry_run", False)),
                    debug=bool(getattr(args, "debug", False)),
                    log=lambda m: print(m, flush=True))
    try:
        print()
        engine.run()
    except EngineError as exc:
        _fail(str(exc))
        return 1
    finally:
        print()
        print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def cmd_list(args) -> int:
    infos = list_windows(visible_only=not args.all, min_size=args.min_size,
                         titles_only=not args.all)
    if args.filter:
        needle = args.filter.lower()
        infos = [i for i in infos if needle in i.title.lower()
                 or needle in i.class_name.lower()
                 or needle in i.process_name.lower()]
    if not infos:
        _warn("no matching windows; try --all or --min-size 0")
        return 1
    print(f"{'hwnd':>12}  {'pid':>7}  {'size':>11}  {'process':<24} class / title")
    print("-" * 110)
    for info in infos:
        print(
            f"0x{info.hwnd:08X}  {info.pid:>7}  "
            f"{info.client_size[0]:>5}x{info.client_size[1]:<5}  "
            f"{info.process_name[:24]:<24} {info.class_name} | {info.title[:48]}"
        )
    print()
    _info(f"{len(infos)} window(s). Use --title <substring> or --hwnd 0x... with other commands.")
    _info("Tip: pick the emulator, then run `simauto probe --title <name>`.")
    return 0


# --------------------------------------------------------------------------
# tree
# --------------------------------------------------------------------------

def cmd_tree(args) -> int:
    """Dump the window/child hierarchy.

    Emulators render into a *child* window, not the top-level one.  When the
    top-level handle yields black frames, this is how you find the handle that
    actually has the picture in it.
    """
    root = resolve_window(_window_arg(args))
    _info(f"hierarchy of {root.describe()}")
    lines: list[str] = []

    def walk(hwnd: int, depth: int) -> None:
        if depth > args.depth:
            return
        try:
            size = _client_size_of(hwnd)
            cls = _class_of(hwnd)
            title = _title_of(hwnd)
            vis = "visible" if _visible_of(hwnd) else "hidden"
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{'  ' * depth}(error: {exc})")
            return
        lines.append(
            f"{'  ' * depth}0x{hwnd:08X} {size[0]:>5}x{size[1]:<5} {vis:<7} "
            f"{cls[:34]:<34} {title[:40]!r}"
        )
        for child in _children_of(hwnd):
            walk(child, depth + 1)

    walk(root.hwnd, 0)
    print("\n".join(lines))
    print()
    _info("If the top-level handle gives black frames, probe a child handle "
          "(e.g. `probe --hwnd 0x...`) that has the render size.")
    return 0


def _client_size_of(hwnd):
    from .win32 import get_client_size
    return get_client_size(hwnd)


def _class_of(hwnd):
    from .win32 import get_class_name
    return get_class_name(hwnd)


def _title_of(hwnd):
    from .win32 import get_window_text
    return get_window_text(hwnd)


def _visible_of(hwnd):
    from .win32 import is_visible
    return is_visible(hwnd)


def _children_of(hwnd):
    from .win32 import enum_child_windows
    return enum_child_windows(hwnd)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    problems = 0
    print(f"simauto {__version__}  |  {sys.platform}  |  python {sys.version.split()[0]}")
    print(f"cwd: {os.getcwd()}")

    if sys.platform != "win32":
        _fail("this tool only drives Windows emulator windows (Win32 GDI + SendInput)")
        return 2
    _ok("running on Windows")

    try:
        import numpy
        _ok(f"numpy {numpy.__version__}")
    except ImportError:
        _fail("numpy is missing — recognition cannot work")
        problems += 1

    try:
        from PIL import Image  # noqa: F401
        _ok("Pillow present (templates in .png/.jpg can be read)")
    except ImportError:
        _warn("Pillow missing: only .bmp/.png screenshots can be read; "
              "install with `pip install pillow`")
        problems += 1

    try:
        import yaml  # noqa: F401
        _ok("PyYAML present (YAML configs work)")
    except ImportError:
        _info("PyYAML missing: use JSON configs, or `pip install pyyaml`")

    try:
        import maa  # noqa: F401
        _ok("maafw present: FramePool/PrintWindow screencap available "
            "(best for GPU-rendered emulator windows)")
    except ImportError:
        _info("maafw not installed (optional): `python -m pip install maafw` adds "
              "MaaFramework's FramePool/PrintWindow capture, which works on emulator "
              "windows where GDI returns black")

    try:
        from .win32 import enable_dpi_awareness, enum_windows
        aware = enable_dpi_awareness()
        _ok(f"DPI awareness: {'per-monitor' if aware else 'NOT set (coordinates may be scaled!)'}")
        if not aware:
            problems += 1
        n = len(enum_windows())
        _ok(f"Win32 window enumeration works ({n} top-level windows)")
    except Exception as exc:  # noqa: BLE001
        _fail(f"Win32 API access failed: {type(exc).__name__}: {exc}")
        problems += 1
        return 1

    # Any emulator looking windows?
    hints = ("mumu", "nemu", "nox", "ldplayer", "dnplayer", "bluestacks", "hd-player",
             "memu", "qemu", "vbox", "virtualbox", "android", "雷电", "夜神", "逍遥")
    found = []
    for info in list_windows(visible_only=False):
        blob = f"{info.title} {info.class_name} {info.process_name}".lower()
        if any(h in blob for h in hints):
            found.append(info)
    if found:
        _ok(f"found {len(found)} emulator-looking window(s):")
        for info in found[:10]:
            print(f"     {info.describe()}")
        _info("next: `simauto probe --title <that title>`")
    else:
        _warn("no emulator-looking window found. Start your emulator first, "
              "or check `simauto list --all`")

    _info("input methods available: sendinput (needs focus), post, send (no focus needed)")
    print()
    if problems:
        _warn(f"{problems} issue(s) worth fixing")
    else:
        _ok("environment looks usable")
    return 0


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def cmd_probe(args) -> int:
    window = resolve_window(_window_arg(args))
    _info(f"probing {window.describe()}")
    return _probe_and_report(
        window,
        backend=(getattr(args, "backend", None) or "auto").lower(),
        out_dir=args.out or os.path.join("sessions", "probe"),
    )


# --------------------------------------------------------------------------
# grab
# --------------------------------------------------------------------------

def _own_process_ids() -> set[int]:
    """Our own pid plus our ancestors' (i.e. the terminal running us).

    A console window that is simply *sitting over* the emulator is the normal
    case when you run these commands from a terminal, so warning about it is
    crying wolf.  Anything owned by this process tree is ignored.
    """
    import ctypes
    pids = {os.getpid()}
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap and snap != -1:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            parents: dict[int, int] = {}
            if kernel32.Process32First(snap, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(snap)
            pid = os.getpid()
            for _ in range(8):          # walk up a few levels, stop at a repeat
                parent = parents.get(pid)
                if not parent or parent in pids:
                    break
                pids.add(parent)
                pid = parent
    except Exception:  # noqa: BLE001 - diagnostic only
        pass
    return pids


def _window_occluded(window) -> tuple[bool, str]:
    """Is the window minimised, or covered by a *foreign* process's window?

    Screen-reading capture cannot tell "the game" from "whatever is on top", so
    the useful moment to warn is now, before the user wonders why matching
    fails.  Comparing the captured frame against a fresh desktop read would be
    circular (a screen read always equals the screen), so this asks the window
    manager instead: who owns the topmost window over the client area?
    """
    if not getattr(window, "hwnd", 0):
        return False, ""
    try:
        from .win32 import get_client_size, get_process_id, is_iconic, window_from_point
    except ImportError:
        return False, ""
    try:
        if is_iconic(window.hwnd):
            return True, "the window is minimised"
    except OSError:
        return False, ""
    own = _own_process_ids()
    try:
        target_pid = get_process_id(window.hwnd)
    except OSError:
        target_pid = 0
    try:
        w, h = get_client_size(window.hwnd)
        if w <= 0 or h <= 0:
            return False, ""
        for cx, cy in ((w // 2, h // 2), (w // 4, h // 4), (3 * w // 4, 3 * h // 4)):
            pt = _point_on_screen(window.hwnd, cx, cy)
            if pt is None:
                continue
            top = window_from_point(*pt)
            if not top or top == window.hwnd:
                continue
            # Qt-based emulators render into a *separate sibling/child* window
            # owned by the same process, so hwnd inequality means nothing.  Same
            # process == same application == not an obstruction.
            if target_pid and get_process_id(top) == target_pid:
                continue
            if _root_of(top) == window.hwnd:
                continue
            if get_process_id(top) in own:
                continue     # our own console sitting on top: not a problem
            return True, f"another program's window (0x{top:08X}) is over the emulator"
    except OSError:
        return False, ""
    return False, ""


def _point_on_screen(hwnd: int, x: int, y: int):
    try:
        from .win32 import client_to_screen
        return client_to_screen(hwnd, x, y)
    except OSError:
        return None


def _root_of(hwnd: int) -> int:
    try:
        from .win32 import get_root_window
        return get_root_window(hwnd)
    except OSError:
        return hwnd


def cmd_grab(args) -> int:
    window, cap = _open_capture(args)
    try:
        if args.times > 1:
            samples = []
            for _ in range(args.times):
                t0 = time.perf_counter()
                frame = cap.grab()
                samples.append(((time.perf_counter() - t0) * 1000, frame))
                time.sleep(args.interval)
            times = [s[0] for s in samples]
            frame = samples[-1][1]
            _info(f"{args.times} grabs: min {min(times):.1f} ms  "
                  f"avg {sum(times)/len(times):.1f} ms  max {max(times):.1f} ms")
        else:
            frame = cap.grab()
        out = args.out or os.path.join("sessions", "grabs",
                                       time.strftime("frame-%H%M%S.png"))
        if not os.path.dirname(out):
            # A bare filename would land in the project root and get lost among
            # the source files; give it a home instead.
            out = os.path.join("sessions", "grabs", out)
        save_image(out, frame)
        h, w = frame.shape[:2]
        _ok(f"captured {w}x{h} from {window.title!r} -> {out}")
        print(f"     mean brightness {frame.mean():.1f}  "
              f"unique colours ~{len(np.unique(frame.reshape(-1, 3), axis=0))}")
        # Coverage note: suppressed by default.  A terminal window sitting over
        # the emulator is the normal case, the check cannot reliably tell that
        # apart from a real obstruction, and the frame's own statistics (a
        # sudden change in mean brightness) already reveal the problem.  Use
        # --verbose when diagnosing, or `simauto verify` for the real answer.
        if args.verbose and getattr(cap, "name", "") != "adb":
            occluded, why = _window_occluded(window)
            if occluded:
                _info(f"note: {why} - this backend reads the screen, so frames may "
                      f"show the wrong thing (raise the emulator, or use --backend adb)")
        if args.roi:
            x, y, rw, rh = (int(v) for v in args.roi.split(","))
            sub = crop_roi(frame, (x, y, rw, rh))
            sub_path = os.path.splitext(out)[0] + "-roi.png"
            save_image(sub_path, sub)
            _ok(f"roi {x},{y},{rw}x{rh} -> {sub_path}")
    finally:
        cap.close()
    return 0


# --------------------------------------------------------------------------
# crop
# --------------------------------------------------------------------------

def cmd_crop(args) -> int:
    if not args.frame:
        window, cap = _open_capture(args)
        try:
            frame = cap.grab()
        finally:
            cap.close()
        _info(f"captured live frame from {window.title!r}")
    else:
        frame = load_image(args.frame)
        _info(f"loaded {args.frame} ({frame.shape[1]}x{frame.shape[0]})")

    if args.roi:
        x, y, w, h = (int(v) for v in args.roi.split(","))
    elif args.gui:
        picked = _gui_pick(frame)
        if picked is None:
            _warn("selection cancelled")
            return 1
        x, y, w, h = picked
    else:
        _fail("give --roi x,y,w,h, or --gui to drag a box")
        return 2

    sub = crop_roi(frame, (x, y, w, h))
    out = args.out or os.path.join("templates", "template.png")
    save_image(out, sub)
    _ok(f"saved {w}x{h} template -> {out}")

    if args.preview:
        overlay = _draw_boxes(frame.copy(), [(x, y, w, h)])
        preview = os.path.splitext(out)[0] + "-where.png"
        save_image(preview, overlay)
        _info(f"location preview: {preview}")

    # Immediately score the new template against the frame it came from: a
    # template cut from the wrong spot scores badly even on its own source
    # frame, which catches most cropping mistakes before they reach the config.
    probe_spec = {"method": "template", "template": out, "threshold": 0.0,
                  "roi": [x, y, w, h]}
    try:
        res = run_detector(frame, probe_spec)
        info = suggest_threshold(frame, load_image(out), roi=(x, y, w, h))
        _ok(f"self-check: score {res.score:.4f} inside its own ROI "
            f"(runner-up elsewhere: {info.get('runner_up', 'n/a')})")
        if res.score < 0.9:
            _warn("even the source rectangle scores below 0.9 — the UI there may be "
                  "animated or semi-transparent; use method=color instead")
        elif info.get("suggested_threshold"):
            _info(f"safe threshold suggestion: {info['suggested_threshold']}")
    except (ImageError, ValueError) as exc:
        _warn(f"self-check failed: {exc}")
    _info(f'config snippet: {{"method": "template", "template": "{out}", '
          f'"roi": [{x}, {y}, {w}, {h}], "threshold": 0.88}}')
    return 0


def _gui_pick(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Drag-a-rectangle picker.  Tkinter only, opened maximised if too big."""
    try:
        import tkinter as tk
    except ImportError:
        _fail("tkinter is not available; use --roi x,y,w,h instead")
        return None

    root = tk.Tk()
    root.title("simauto crop — drag a box, then press Enter (Esc cancels)")
    h, w = frame.shape[:2]
    photo = _to_photoimage(root, frame)
    canvas = tk.Canvas(root, width=min(w, 1200), height=min(h, 800), highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)
    canvas.image = photo  # keep a reference

    state: dict[str, Any] = {"x0": 0, "y0": 0, "rect": None, "box": None}

    def on_press(ev):
        state["x0"], state["y0"] = ev.x, ev.y
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(ev.x, ev.y, ev.x, ev.y,
                                                outline="#00ff00", width=2)

    def on_drag(ev):
        if state["rect"]:
            canvas.coords(state["rect"], state["x0"], state["y0"], ev.x, ev.y)

    def on_release(ev):
        x0, y0 = state["x0"], state["y0"]
        x, y = min(x0, ev.x), min(y0, ev.y)
        ww, hh = abs(ev.x - x0), abs(ev.y - y0)
        state["box"] = (x, y, ww, hh)
        print(f"selection: {x},{y},{ww},{hh}", flush=True)

    def on_key(ev):
        if ev.keysym == "Escape":
            state["box"] = None
            root.destroy()
        elif ev.keysym == "Return":
            root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Key>", on_key)
    root.focus_force()
    root.mainloop()
    box = state["box"]
    if not box or box[2] < 3 or box[3] < 3:
        return None
    return box


def _to_photoimage(root, frame: np.ndarray):
    """Build a Tk image without requiring Pillow (PPM is plain text)."""
    try:
        from PIL import Image, ImageTk
        h, w = frame.shape[:2]
        im = Image.fromarray(np.ascontiguousarray(frame, dtype=np.uint8), "RGB")
        if h > 800 or w > 1200:
            scale = min(1200 / w, 800 / h)
            im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(im)
    except ImportError:
        pass
    h, w = frame.shape[:2]
    if h > 800 or w > 1200:
        # Nearest-neighbour downscale; good enough to draw a box on.
        scale = min(1200 / w, 800 / h)
        idx_y = (np.arange(int(h * scale)) / scale).astype(np.int64)
        idx_x = (np.arange(int(w * scale)) / scale).astype(np.int64)
        frame = frame[idx_y][:, idx_x]
    import tkinter as tk
    h, w = frame.shape[:2]
    header = f"P6\n{w} {h}\n255\n".encode("ascii")
    import base64
    data = base64.b64encode(header + np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
    return tk.PhotoImage(data=data)


# --------------------------------------------------------------------------
# match
# --------------------------------------------------------------------------

def cmd_match(args) -> int:
    frame = load_image(args.frame)
    if args.template:
        specs = [{"name": os.path.basename(args.template), "method": "template",
                  "template": args.template}]
    elif args.config:
        cfg = load_config(args.config)
        names = [n.strip() for n in args.recognizers.split(",")] if args.recognizers \
            else list(cfg.recognizers)
        specs = []
        for name in names:
            spec = cfg.recognizer(name)
            if str(spec.get("method", "template")).lower() == "template":
                tpl = spec.get("template")
                spec = dict(spec)
                spec["template"] = cfg.template(str(tpl))
            specs.append(spec)
    else:
        _fail("give --template or --config")
        return 2

    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    threshold = float(args.threshold) if args.threshold is not None else None
    boxes: list[tuple[int, int, int, int]] = []
    failures = 0
    print(f"frame {args.frame} ({frame.shape[1]}x{frame.shape[0]})"
          + (f"  roi {roi}" if roi else ""))
    print(f"{'name':<32} {'method':<9} {'score':>8} {'thr':>5}  {'hit':<4} location")
    print("-" * 100)
    for spec in specs:
        spec = dict(spec)
        if roi and not spec.get("roi"):
            spec["roi"] = roi
        if threshold is not None:
            spec["threshold"] = threshold
        if "scales" in spec:
            spec["scales"] = parse_scale_list(str(spec["scales"]))
        elif args.scales:
            spec["scales"] = parse_scale_list(args.scales)
        try:
            res = run_detector(frame, spec)
        except (ImageError, ValueError) as exc:
            _fail(f"{spec.get('name', '?'):<32} {exc}")
            failures += 1
            continue
        mark = _c("HIT", "green") if res.hit else _c("no", "grey")
        print(f"{str(spec.get('name') or spec.get('template'))[:32]:<32} "
              f"{res.method:<9} {res.score:>8.4f} {res.threshold:>5.2f}  {mark:<4} "
              f"{res.box or '-'} {res.metrics.get('error', '')}")
        if res.box:
            boxes.append(res.box)
        if not res.hit:
            failures += 1

    if args.out and boxes:
        overlay = _draw_boxes(frame.copy(), boxes)
        save_image(args.out, overlay)
        _ok(f"annotated image -> {args.out}")

    if args.calibrate and args.template:
        info = suggest_threshold(frame, load_image(args.template), roi=roi)
        print()
        _info(f"calibration: best={info.get('best_score')} "
              f"runner_up={info.get('runner_up')} "
              f"safe_threshold={info.get('suggested_threshold')}")
        if info.get("runner_up") is not None and info.get("best_score") is not None:
            margin = info["best_score"] - info["runner_up"]
            if margin < 0.08:
                _warn(f"margin is only {margin:.3f}: this template will misfire. "
                      f"Shrink the ROI, or switch to method=color.")
            else:
                _ok(f"margin {margin:.3f} — comfortable")

    print()
    _info(f"{len(specs) - failures}/{len(specs)} recognisers matched")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------

def cmd_validate(args) -> int:
    try:
        cfg = load_config(args.config)
    except (EngineError, OSError, json.JSONDecodeError) as exc:
        _fail(f"config unreadable: {exc}")
        return 2
    _ok(f"config {cfg.name!r}: {len(cfg.tasks)} tasks, "
        f"{len(cfg.recognizers)} recognisers, mode={cfg.data.get('mode', 'events')}")
    print(f"     reference resolution: {cfg.reference[0]}x{cfg.reference[1]}")
    print(f"     window spec: {cfg.window_spec}")
    print(f"     capture: {cfg.capture_cfg or '(default)'}   input: {cfg.input_cfg or '(default)'}")

    problems = 0
    for name in cfg.recognizers:
        spec = cfg.recognizer(name)
        if str(spec.get("method", "template")).lower() != "template":
            continue
        tpls = spec.get("template")
        for tpl in (tpls if isinstance(tpls, list) else [tpls]):
            path = cfg.resolve_path(str(tpl))
            if not os.path.exists(path):
                _fail(f"recognizer {name!r}: template missing -> {path}")
                problems += 1
            else:
                img = load_image(path)
                if img.shape[0] < 4 or img.shape[1] < 4:
                    _warn(f"recognizer {name!r}: template is tiny "
                          f"({img.shape[1]}x{img.shape[0]}px); matching will be noisy")
                if name not in (spec.get("name") or ""):
                    pass
    tasks = ", ".join(cfg.tasks)
    _info(f"tasks: {tasks}")
    if "priority" in cfg.data:
        _info(f"priority order: {cfg.data['priority']}")
    print()
    if problems:
        _fail(f"{problems} template file(s) missing")
        return 1
    _ok("all templates present")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(args) -> int:
    try:
        cfg = load_config(args.config)
    except (EngineError, OSError, json.JSONDecodeError) as exc:
        _fail(f"config unreadable: {exc}")
        return 2
    if args.mode:
        cfg.data["mode"] = args.mode
    if args.task:
        cfg.data["mode"] = "once"
        cfg.data["steps"] = [{"action": "log", "message": f"running task {args.task}"}]
        cfg.tasks.setdefault(args.task, {"actions": []})

    engine = None
    try:
        # CLI flags override the config's capture/input blocks.
        if args.screencap:
            # --screencap means different things per backend: a maafw method
            # (framepool/screendc/...) or an ADB method (rawbynetcat/...).
            if args.backend in ("adb", "android"):
                cfg.capture_cfg["adb_screencap"] = args.screencap
            else:
                cfg.capture_cfg["screencap"] = args.screencap
        if args.mouse:
            cfg.capture_cfg["mouse"] = args.mouse
            cfg.input_cfg.setdefault("method", args.mouse)
        if args.adb_input:
            cfg.capture_cfg["adb_input"] = args.adb_input
        if args.address:
            cfg.capture_cfg["address"] = args.address
        if args.adb_path:
            cfg.capture_cfg["adb_path"] = args.adb_path
        engine = Engine(
            cfg,
            input_method=args.input,
            capture_method=None if args.method == "auto" else args.method,
            backend=args.backend,
            debug=args.debug,
            record=args.record,
            record_every=args.record_every,
            dry_run=args.dry_run,
            max_ticks=args.max_ticks,
        )
    except (EngineError, ImageError, LookupError, MaaBackendUnavailable) as exc:
        _fail(str(exc))
        return 2

    backend_name = getattr(engine.source.capture, "name", "gdi")
    if backend_name == "adb":
        cap_desc = (f"{getattr(engine.source.capture, 'screencap_label', ('?',))[0]}"
                    f"/{getattr(engine.source.capture, 'input_label', ('?',))[0]}")
    elif backend_name == "maafw":
        cap_desc = describe_screencap_mask(
            getattr(engine.source.capture, "screencap_mask", 0)
        )
    else:
        cap_desc = getattr(engine.source.capture, "mode", "?")
    input_desc = getattr(engine.input, "name", None) or getattr(engine.input, "method", "?")
    print(f"window : {engine.source.window.describe()}")
    print(f"capture: {backend_name}:{cap_desc}   input: {input_desc}"
          + ("   [DRY RUN]" if args.dry_run else ""))
    print(f"mode   : {cfg.data.get('mode', 'events')}   "
          f"reference {cfg.reference[0]}x{cfg.reference[1]}")
    print(f"session: {engine.recorder.session if engine.recorder.enabled else '(recording off)'}")
    print("-" * 78 + "\n")
    if args.dry_run:
        _info("dry run: recognition runs, nothing is clicked")

    if args.task:
        try:
            engine.run_task(args.task)
        except EngineError as exc:
            _fail(str(exc))
            return 1
        finally:
            engine.input.close()
            engine.source.close()
        print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
        return 0

    status = 0
    try:
        reason = engine.run()
        print()
        _info(f"stopped: {reason}")
    except KeyboardInterrupt:
        print()
        _warn("interrupted by user")
    except EngineError as exc:
        _fail(str(exc))
        status = 1
    finally:
        try:
            engine.input.close()
            engine.source.close()
        except Exception:  # noqa: BLE001
            pass
    print()
    print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
    return status


# --------------------------------------------------------------------------
# Overlay drawing (numpy only: 3px green border per box)
# --------------------------------------------------------------------------

def _draw_boxes(frame: np.ndarray, boxes) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    for i, (x, y, bw, bh) in enumerate(boxes):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(w, int(x) + int(bw)), min(h, int(y) + int(bh))
        if x1 <= x0 or y1 <= y0:
            continue
        colour = _BOX_COLOURS[i % len(_BOX_COLOURS)]
        t = 2
        out[y0:min(y0 + t, y1), x0:x1] = colour
        out[max(y1 - t, y0):y1, x0:x1] = colour
        out[y0:y1, x0:min(x0 + t, x1)] = colour
        out[y0:y1, max(x1 - t, x0):x1] = colour
    return out


_BOX_COLOURS = [
    (0, 255, 0), (255, 0, 255), (0, 200, 255), (255, 200, 0), (0, 120, 255),
]


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def _add_window_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--title", help="window title substring (e.g. MuMu)")
    p.add_argument("--class-name", dest="class_name", help="window class substring")
    p.add_argument("--hwnd", help="exact window handle, e.g. 0x00123456")
    p.add_argument("--index", type=int, default=0, help="pick the Nth match (default 0)")
    p.add_argument("--regex", action="store_true", help="treat --title/--class-name as regex")
    p.add_argument("--focus", action="store_true",
                   help="activate the window first (needed by screen-reading methods "
                        "such as screendc, and by sendinput clicks)")


def _add_capture_args(p: argparse.ArgumentParser) -> None:
    # default=None so "not given on the command line" does not overwrite the
    # config file's capture.backend.  Defaulting this to "auto" silently
    # discarded a config that said backend=adb.
    p.add_argument("--backend", default=None, choices=("auto", "gdi", "maafw", "adb"),
                   help="capture implementation: auto prefers maafw, gdi needs no "
                        "packages, adb reads the device (works while covered); "
                        "default: whatever the config says")
    p.add_argument("--method", default="auto",
                   choices=list(PROBE_MODES) + ["auto"],
                   help="GDI capture method; 'auto' probes and picks (default: auto)")
    p.add_argument("--screencap", default=None,
                   help="screencap method: for maafw 'framepool'/'background'/..., "
                        "for adb 'rawbynetcat'/'default'/...")
    p.add_argument("--mouse", default=None,
                   help="maafw input method, e.g. postmessage (default) or "
                        "sendmessagewithcursorpos")
    p.add_argument("--adb-path", dest="adb_path", default=None,
                   help="path to adb.exe (default: PATH, then the known emulator installs)")
    p.add_argument("--address", default=None,
                   help="ADB device address, e.g. 127.0.0.1:16384 for MuMu 12")
    p.add_argument("--adb-input", dest="adb_input", default=None,
                   help="ADB input method: maatouch (fast) / adb (slow) / default")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simauto",
        description="Image-recognition bot for Android emulators on Windows "
                    "(MAA-style tasks; GDI built in, MaaFramework optional).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "typical first run:\n"
            "  simauto list\n"
            "  simauto doctor\n"
            "  python -m pip install maafw        # optional: background screencap\n"
            "  simauto probe --title MuMu\n"
            "  simauto grab --title MuMu -o frame.png\n"
            "  simauto crop --frame frame.png --roi 1000,600,240,120 -o templates/attack.png\n"
            "  simauto match --frame frame.png --template templates/attack.png --calibrate\n"
            "  simauto run --config configs/bh2-starter.json --debug --dry-run\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"simauto {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list candidate windows")
    p.add_argument("--all", action="store_true", help="include hidden/untitled windows")
    p.add_argument("--min-size", type=int, default=200, help="minimum client width/height")
    p.add_argument("--filter", help="substring filter on title/class/process")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("doctor", help="check environment and look for emulators")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("tree", help="dump the window/child hierarchy (find the render window)")
    _add_window_args(p)
    p.add_argument("--depth", type=int, default=3)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("find", help="auto-locate the emulator and probe its capture methods")
    p.add_argument("--index", type=int, default=0,
                   help="which candidate to probe (default 0 = best ranked)")
    p.add_argument("--limit", type=int, default=6, help="how many candidates to list")
    p.add_argument("--all", action="store_true",
                   help="consider every window, not just emulator-looking ones")
    p.add_argument("--backend", default="auto", choices=("auto", "gdi", "maafw"))
    p.add_argument("--out", help="directory for sample frames")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("adb", help="list ADB devices / test the ADB capture backend")
    p.add_argument("--list", action="store_true", help="only list devices, then exit")
    p.add_argument("--address", help="device address, e.g. 127.0.0.1:16384")
    p.add_argument("--adb-path", dest="adb_path", help="path to adb.exe")
    p.add_argument("--screencap", help="ADB screencap method (default: auto speed test)")
    p.add_argument("--times", type=int, default=5, help="how many frames to time")
    p.add_argument("--out", help="save a sample frame here")
    p.set_defaults(func=cmd_adb)

    p = sub.add_parser("probe", help="test every capture backend on a window")
    _add_window_args(p)
    p.add_argument("--out", help="directory for sample frames (default sessions/probe)")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("grab", help="save a screenshot of the window")
    _add_window_args(p)
    _add_capture_args(p)
    p.add_argument("-o", "--out", help="output image path")
    p.add_argument("--roi", help="also save this x,y,w,h region")
    p.add_argument("--times", type=int, default=1, help="grab N frames and report timing")
    p.add_argument("--interval", type=float, default=0.1, help="delay between grabs")
    p.add_argument("--verbose", action="store_true",
                   help="also report whether another window covers the emulator")
    p.set_defaults(func=cmd_grab)

    p = sub.add_parser("preview", help="show all cropped templates (contact sheet + gallery)")
    p.add_argument("--dir", help="template directory (default: <project>/templates)")
    p.add_argument("--filter", help="only names containing this substring")
    p.add_argument("--out", help="contact sheet output path")
    p.add_argument("--cell", type=int, default=96, help="cell size in the contact sheet")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("ocr", help="read text off the screen (robust alternative to templates)")
    p.add_argument("--frame", help="image file (default: grab the live screen over ADB)")
    p.add_argument("--roi", help="x,y,w,h - strongly recommended, full-frame OCR is slow")
    p.add_argument("--min-conf", type=float, default=0.3, help="hide results below this confidence")
    p.add_argument("--model", help="OCR model directory (det.onnx/rec.onnx/keys.txt)")
    p.add_argument("--address", help="ADB device address for --frame-less use")
    p.add_argument("--adb-path", dest="adb_path", help="path to adb.exe")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("resonance", help="read the resonance shop / refresh and buy from the list",
                       description="Reads the six exchange slots, then optionally refreshes N "
                                   "times, buying anything named in watchlist.txt that is not "
                                   "already owned.")
    _add_capture_args(p)
    p.add_argument("--refresh", type=int, default=0,
                   help="how many times to refresh (0 = only read, the default)")
    p.add_argument("--watchlist-file", dest="watchlist_file",
                   help="watchlist text file (default: <project>/watchlist.txt)")
    p.add_argument("--no-buy", dest="no_buy", action="store_true",
                   help="refresh but never buy")
    p.add_argument("--refresh-point", dest="refresh_point", default="1088,153",
                   help="refresh button x,y (default 1088,153)")
    p.add_argument("--currency-roi", dest="currency_roi", default="330,120,230,60",
                   help="x,y,w,h holding the 共鸣币 balance")
    p.add_argument("--currency-min", dest="currency_min", type=int, default=6000,
                   help="skip buying when the balance is below this")
    p.add_argument("--settle", type=float, default=2.5, help="seconds to wait after each action")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="never click")
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_resonance)

    p = sub.add_parser("stagemap", help="find stages whose first-clear reward is unclaimed",
                       description="Detects the cyan crystal icon on stage cards by colour, "
                                   "so it does not depend on reading the small stage labels.")
    _add_capture_args(p)
    p.add_argument("--frame", help="image file to inspect instead of the live screen")
    p.add_argument("--enter", action="store_true",
                   help="tap the first detected stage to open it")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="report only")
    p.set_defaults(func=cmd_stagemap)

    p = sub.add_parser("gui", help="launch the graphical interface",
                       description="Tkinter front-end modelled on MAA's 一键长草 page. "
                                   "清体力 is one of the task checkboxes, not a separate tab.")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser(
        "verify",
        help="check whether background (covered-window) capture really works",
        description="Distinguishes 'FramePool/PrintWindow capture the window' from "
                    "'MaaFramework silently fell back to copying the desktop', by "
                    "comparing each method against two independent screen reads.",
    )
    _add_window_args(p)
    p.add_argument("--rounds", type=int, default=2,
                   help="how many times to repeat (median is used; a live game is noisy)")
    p.add_argument("--threshold", type=float, default=2.0,
                   help="mean-abs-diff below which two frames count as 'the same picture'")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("crop", help="cut a template out of a frame")
    _add_window_args(p)
    _add_capture_args(p)
    p.add_argument("--frame", help="use this image instead of a live capture")
    p.add_argument("--roi", help="x,y,w,h in image pixels")
    p.add_argument("--gui", action="store_true", help="drag the rectangle with the mouse")
    p.add_argument("-o", "--out", default=os.path.join("templates", "template.png"))
    p.add_argument("--preview", action="store_true", help="also save a box-on-frame image")
    p.set_defaults(func=cmd_crop)

    p = sub.add_parser("match", help="run recognisers against an image")
    p.add_argument("--frame", required=True)
    p.add_argument("--template")
    p.add_argument("--config")
    p.add_argument("--recognizers", help="comma separated names (default: all)")
    p.add_argument("--roi", help="x,y,w,h override")
    p.add_argument("--threshold", type=float)
    p.add_argument("--scales", help='e.g. "0.9:1.1:0.05"')
    p.add_argument("--calibrate", action="store_true",
                   help="report a safe threshold for --template")
    p.add_argument("--out", help="save an annotated copy here")
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("validate", help="check a config and its template files")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run a config")
    p.add_argument("--config", required=True)
    _add_window_args(p)  # accepted but config wins; kept for symmetry
    _add_capture_args(p)
    p.add_argument("--input", choices=("sendinput", "post", "send"),
                   help="override input method from the config")
    p.add_argument("--mode", choices=("once", "loop", "events"), help="override mode")
    p.add_argument("--task", help="run a single task once and exit")
    p.add_argument("--debug", action="store_true", help="log every recogniser score")
    p.add_argument("--dry-run", action="store_true", help="recognise but never click")
    p.add_argument("--record", action="store_true", help="save every tick frame")
    p.add_argument("--record-every", type=float, default=0.0,
                   help="save a frame every N seconds (0 = off)")
    p.add_argument("--max-ticks", type=int, help="stop after N polls")
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (GBK on Chinese systems),
    # which turns game/config names into mojibake in every message we print.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    from .win32 import enable_dpi_awareness
    enable_dpi_awareness()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print()
        _warn("interrupted")
        return 130
    except FileNotFoundError as exc:
        _fail(f"file not found: {exc.filename or exc}")
        _info("if this is a template, crop it first: "
              "`simauto crop --frame frame.png --gui -o templates/<name>.png`")
        _info("check what is missing with `simauto validate --config <config>`")
        return 1
    except (EngineError, ImageError, LookupError, MaaBackendUnavailable) as exc:
        _fail(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - never dump a bare traceback at a user
        _fail(f"{type(exc).__name__}: {exc}")
        _info("run the same command with --debug for more detail, or "
              "`simauto validate --config <config>` to check the config and templates")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
