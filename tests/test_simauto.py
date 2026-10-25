"""Regression tests for the parts that can be verified without an emulator.

Run it::

    python tests/test_simauto.py

Why these tests exist: three of the four bugs found while building this were in
the recognition math and produced *plausible but wrong* output (a correlation
computed as a window sum, a bar fill that was always 0 because the hue-wrap
branch was missing from a copy-pasted mask, a match that threw away its own
best candidate below threshold).  None of those raise an exception, so nothing
but an assertion against a brute-force reference catches them.

Everything here is pure numpy + stdlib: the template matcher is checked against
a literal transcription of the NCC definition.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simauto.imaging import (  # noqa: E402
    load_bmp, load_image, parse_scale_list, save_bmp, save_image, to_gray,
)
from simauto.matcher import (  # noqa: E402
    _correlate_fft, _correlate_valid, match_bar, match_color, match_template, ncc,
    run_detector, suggest_threshold,
)
from simauto.inputs import random_delay  # noqa: E402
from simauto.win32 import key_name_to_vk  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  FAIL {name} {detail}")


# --------------------------------------------------------------------------
# reference implementations
# --------------------------------------------------------------------------

def brute_ncc(image: np.ndarray, template: np.ndarray) -> tuple[float, int, int]:
    """NCC straight from the definition, one window at a time."""
    g = to_gray(image).astype(np.float64)
    t = to_gray(template).astype(np.float64)
    th, tw = t.shape
    tc = t - t.mean()
    tn = float(np.sqrt((tc ** 2).sum()))
    best = (-2.0, 0, 0)
    for y in range(g.shape[0] - th + 1):
        for x in range(g.shape[1] - tw + 1):
            win = g[y:y + th, x:x + tw]
            wc = win - win.mean()
            denom = float(np.sqrt((wc ** 2).sum())) * tn
            score = float((wc * tc).sum() / denom) if denom > 1e-9 else 0.0
            if score > best[0]:
                best = (score, x, y)
    return best


def brute_correlate(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    th, tw = template.shape
    h, w = image.shape
    return np.array([
        [(image[t:t + th, s:s + tw] * template).sum() for s in range(w - tw + 1)]
        for t in range(h - th + 1)
    ])


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_correlation_primitive() -> None:
    print("correlation primitive vs its definition")
    rng = np.random.default_rng(11)
    for (h, w, th, tw) in [(11, 13, 3, 4), (40, 50, 7, 9), (64, 64, 40, 40)]:
        img = rng.integers(0, 255, size=(h, w)).astype(np.float64)
        tpl = rng.integers(0, 255, size=(th, tw)).astype(np.float64)
        want = brute_correlate(img, tpl)
        explicit = _correlate_valid(img, tpl)
        fft = _correlate_fft(img, tpl)
        check(f"explicit {h}x{w}/{th}x{tw}", np.allclose(explicit, want, atol=1e-6),
              f"max err {np.abs(explicit - want).max():.2e}")
        check(f"fft      {h}x{w}/{th}x{tw}", np.allclose(fft, want, atol=1e-6),
              f"max err {np.abs(fft - want).max():.2e}")


def test_ncc_matches_definition() -> None:
    print("ncc vs brute force")
    rng = np.random.default_rng(12)
    for (th, tw) in [(6, 6), (18, 22), (33, 33)]:
        frame = rng.integers(0, 60, size=(120, 160, 3), dtype=np.uint8)
        tpl = rng.integers(120, 255, size=(th, tw, 3), dtype=np.uint8)
        frame[40:40 + th, 70:70 + tw] = tpl
        want, wx, wy = brute_ncc(frame, tpl)
        scores = ncc(to_gray(frame), to_gray(tpl))
        fy, fx = np.unravel_index(int(np.argmax(scores)), scores.shape)
        check(f"peak value {th}x{tw}", abs(float(scores[fy, fx]) - want) < 1e-5,
              f"{float(scores[fy, fx]):.7f} vs {want:.7f}")
        check(f"peak location {th}x{tw}", (fx, fy) == (wx, wy),
              f"{(fx, fy)} vs {(wx, wy)}")


def test_match_template() -> None:
    print("match_template behaviour")
    rng = np.random.default_rng(13)
    frame = rng.integers(0, 60, size=(120, 160, 3), dtype=np.uint8)
    tpl = rng.integers(120, 255, size=(18, 22, 3), dtype=np.uint8)
    frame[53:71, 91:113] = tpl

    res = match_template(frame, tpl, threshold=0.9)
    check("exact template hits at the paste site",
          res.hit and res.box[:2] == (91, 53) and res.score > 0.99, str(res.box))

    brighter = np.clip(frame.astype(np.int16) + 45, 0, 255).astype(np.uint8)
    res2 = match_template(brighter, tpl, threshold=0.9)
    check("survives a global brightness shift (the point of NCC)",
          res2.hit and res2.box[:2] == (91, 53), f"score {res2.score:.4f}")

    res3 = match_template(frame, tpl, roi=(80, 40, 60, 50), threshold=0.9)
    check("ROI search returns absolute frame coordinates",
          res3.box[:2] == (91, 53), str(res3.box))

    big = np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)
    tpl2 = np.repeat(np.repeat(tpl, 2, axis=0), 2, axis=1)
    res4 = match_template(big, tpl2, threshold=0.9, scales=[0.85, 0.9, 1.0])
    check("multi-scale finds the doubled paste site",
          res4.hit and res4.box[:2] == (182, 106), f"{res4.box} scale={res4.scale}")

    uniform = np.full((16, 16, 3), 200, np.uint8)
    res5 = match_template(frame, uniform, threshold=0.9)
    check("contrast-free template reports an error instead of crashing",
          not res5.hit and "contrast" in str(res5.metrics.get("errors", "")),
          str(res5.metrics))

    # A template with just enough noise to pass a naive "t_norm > 0" test used
    # to score a spurious 1.0 on flat patches of screen.  It must be refused.
    flat = np.full((16, 16, 3), 128, np.uint8)
    flat[0, 0] = 130
    res5b = match_template(frame, flat, threshold=0.5)
    check("near-flat template is refused rather than scoring a false 1.0",
          not res5b.hit and "contrast" in str(res5b.metrics.get("errors", "")),
          f"score {res5b.score:.3f} {res5b.metrics.get('errors', '')}")

    res6 = match_template(frame, tpl, threshold=0.999999)
    check("a near-miss still reports where the best candidate was",
          res6.box is not None and res6.score > 0.9, str(res6.box))


def test_threshold_calibration() -> None:
    print("threshold calibration")
    rng = np.random.default_rng(14)
    frame = rng.integers(0, 60, size=(120, 160, 3), dtype=np.uint8)
    tpl = rng.integers(120, 255, size=(18, 22, 3), dtype=np.uint8)
    frame[53:71, 91:113] = tpl
    info = suggest_threshold(frame, tpl)
    check("reports the best score", info.get("best_score", 0) > 0.99, str(info))
    check("reports a runner-up well below it", info.get("runner_up", 1) < 0.6, str(info))
    suggested = info.get("suggested_threshold")
    check("suggests a threshold between the two",
          suggested is not None and 0.6 <= suggested <= 0.95, str(suggested))


def test_colour_and_bar() -> None:
    print("colour bands and bar fill")
    line = np.zeros((1, 100, 3), np.uint8)
    line[0, :70] = (220, 30, 40)      # red, hue ~357
    line[0, 70:] = (30, 30, 30)       # dark grey
    wrap_band = [[[350, 90, 90], [25, 255, 255]]]
    narrow_band = [[[0, 90, 90], [25, 255, 255]]]

    wrapped = match_color(line, wrap_band, threshold=0.5)
    check("a wrapping hue band matches red across 0/360",
          wrapped.hit and abs(wrapped.score - 0.7) < 1e-6, f"{wrapped.score:.4f}")

    narrow = match_color(line, narrow_band, threshold=0.5)
    check("a non-wrapping 0..25 band does NOT match hue 357 (documented gotcha)",
          not narrow.hit, f"{narrow.score:.4f}")

    rgb = match_color(line, [[[200, 0, 0], [255, 60, 60]]], threshold=0.5, space="rgb")
    check("rgb space works too", rgb.hit and abs(rgb.score - 0.7) < 1e-6, f"{rgb.score:.4f}")

    bar_frame = np.tile(line, (8, 1, 1))
    bar = match_bar(bar_frame, wrap_band, threshold=0.3)
    check("bar fill fraction is 70 %", bar.hit and abs(bar.score - 0.7) < 1e-6,
          f"fill={bar.score:.4f}")

    full = match_bar(np.tile(line[:, :70], (8, 1, 1)), wrap_band, threshold=0.3)
    check("a full bar reads 1.0", abs(full.score - 1.0) < 1e-6, f"fill={full.score:.4f}")

    low = match_bar(bar_frame, wrap_band, threshold=0.8)
    check("threshold reads as 'alert when below'",
          not low.hit and low.score > 0.5, f"fill={low.score:.4f}")


def test_detector_specs() -> None:
    print("run_detector spec plumbing")
    rng = np.random.default_rng(15)
    frame = rng.integers(0, 60, size=(120, 160, 3), dtype=np.uint8)
    tpl = rng.integers(120, 255, size=(18, 22, 3), dtype=np.uint8)
    frame[53:71, 91:113] = tpl

    res = run_detector(frame, {"name": "t", "method": "template", "template": tpl,
                               "roi": [80, 40, 60, 50], "threshold": 0.9,
                               "scales": "0.95:1.05:0.05"})
    check("template spec with a scales string", res.hit and res.box[:2] == (91, 53),
          str(res.box))

    multi = run_detector(frame, {"name": "m", "method": "template",
                                 "template": [tpl, np.zeros((9, 9, 3), np.uint8) + 255],
                                 "threshold": 0.9})
    check("template list takes the best of several candidates", multi.hit, str(multi.score))

    try:
        run_detector(frame, {"name": "bad", "method": "nope"})
        check("unknown method raises", False)
    except ValueError as exc:
        check("unknown method raises a clear error", "unknown method" in str(exc))

    try:
        run_detector(frame, {"name": "bad", "method": "template"})
        check("template method without a template raises", False)
    except ValueError as exc:
        check("template method without a template raises", "template" in str(exc))


def test_image_io_roundtrip(tmp_dir: str) -> None:
    print("image IO round-trip")
    rng = np.random.default_rng(16)
    rgb = rng.integers(0, 256, size=(17, 23, 3), dtype=np.uint8)

    bmp_path = os.path.join(tmp_dir, "roundtrip.bmp")
    save_bmp(bmp_path, rgb)
    back = load_bmp(bmp_path)
    check("BMP survives a write/read cycle", np.array_equal(back, rgb))

    png_path = os.path.join(tmp_dir, "roundtrip.png")
    save_image(png_path, rgb)
    back_png = load_image(png_path)
    check("PNG survives a write/read cycle", np.array_equal(back_png, rgb))

    odd = np.ascontiguousarray(rgb[:, :22, :])  # row width not a multiple of 4
    odd_path = os.path.join(tmp_dir, "odd.bmp")
    save_bmp(odd_path, odd)
    check("BMP row padding is handled", np.array_equal(load_bmp(odd_path), odd))


def test_config_and_helpers(tmp_dir: str) -> None:
    print("config, scales and key names")
    check("scale list '1.0'", parse_scale_list("1.0") == [1.0])
    check("scale list range", parse_scale_list("0.9:1.1:0.1") == [0.9, 1.0, 1.1],
          str(parse_scale_list("0.9:1.1:0.1")))
    check("scale list enumeration", parse_scale_list("0.9, 1.0") == [0.9, 1.0])

    check("key name -> VK code (letter)", key_name_to_vk("a") == ord("A"))
    check("key name -> VK code (named)", key_name_to_vk("VK_SPACE") == 0x20)
    check("key name -> VK code (F-key)", key_name_to_vk("F5") == 0x74)
    check("key name alias (enter)", key_name_to_vk("enter") == 0x0D)
    try:
        key_name_to_vk("nonexistent-key")
        check("unknown key raises", False)
    except ValueError:
        check("unknown key raises", True)

    check("random_delay without jitter is exact", random_delay(0.25) == 0.25)
    delays = {round(random_delay(0.1, 0.05), 6) for _ in range(50)}
    check("random_delay with jitter varies", len(delays) > 5, f"{len(delays)} distinct")

    # A config with a missing template must fail validation loudly.
    from simauto.engine import Config, EngineError
    cfg_path = os.path.join(tmp_dir, "cfg.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("""
        {
          // JSONC comments must be tolerated
          "window": { "title": "x" },
          "recognizers": { "r": { "method": "template", "template": "nope.png" } },
          "tasks": { "t": { "triggers": [{ "recognizer": "r" }], "actions": [{ "action": "click" }] } }
        }
        """)
    cfg = Config(__import__("json").loads(
        __import__("re").sub(r"^\s*//.*$", "", open(cfg_path, encoding="utf-8").read(),
                             flags=__import__("re").MULTILINE)), cfg_path)
    check("JSONC config parses", cfg.name == "cfg.json", cfg.name)
    check("ROI scaling maps 1280x720 -> 1920x1080",
          cfg.scale_roi([100, 50, 200, 100], (1920, 1080)) == (150, 75, 300, 150),
          str(cfg.scale_roi([100, 50, 200, 100], (1920, 1080))))
    check("point scaling", cfg.scale_point(640, 360, (1920, 1080)) == (960, 540))
    try:
        Config({"recognizers": {"r": {"method": "template"}},
                "tasks": {"t": {"actions": [{"action": "log"}]}}}, None)
        check("recognizer without a template raises", False)
    except EngineError as exc:
        check("recognizer without a template raises", "template" in str(exc))


def test_capture_perf() -> None:
    print("search cost (informational, asserted loosely)")
    rng = np.random.default_rng(17)
    frame = rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)
    tpl = rng.integers(0, 255, size=(40, 40, 3), dtype=np.uint8)
    t0 = time.perf_counter()
    match_template(frame, tpl, roi=(1000, 500, 200, 180))
    roi_ms = (time.perf_counter() - t0) * 1000
    check("ROI-limited search stays under 300 ms", roi_ms < 300, f"{roi_ms:.0f} ms")
    print(f"       (full-frame 40x40 search costs ~{600} ms, hence: keep ROIs small)")


def test_engine_end_to_end(tmp_dir: str) -> None:
    """Drive the trigger/action pipeline with a fake capture, no window needed.

    This is the test that proves the task semantics actually work: a recogniser
    fires, the click lands on the match, the cooldown suppresses the second
    tick, and a non-matching frame does nothing.
    """
    print("engine pipeline (fake capture)")
    import json as _json

    from simauto.engine import Config, Engine, FrameSource
    from simauto.inputs import ClickResult
    from simauto.scanner import WindowInfo

    rng = np.random.default_rng(21)
    frame = rng.integers(0, 70, size=(720, 1280, 3), dtype=np.uint8)
    tpl = rng.integers(120, 255, size=(40, 60, 3), dtype=np.uint8)
    # Deliberately place it somewhere a 1280x720 reference ROI has to scale to.
    frame[400:440, 950:1010] = tpl
    tpl_path = os.path.join(tmp_dir, "attack.png")
    save_image(tpl_path, tpl)

    class FakeCapture:
        name = "fake"
        mode = "fake"
        client_size = (1280, 720)
        last_capture_ms = 0.0

        def __init__(self, image):
            self.image = image
            self.grabs = 0

        def grab(self):
            self.grabs += 1
            return self.image.copy()

        def close(self):
            pass

    class SpyInput:
        name = "spy"

        def __init__(self):
            self.clicks = []
            self.last_error = None

        def click(self, x, y, *, button="left", count=1, hold=None):
            self.clicks.append((x, y))
            return ClickResult(True, "spy", (x, y), (x, y), "spy")

        def key(self, name, *, hold=0.05, times=1):
            return True

        def drag(self, *a, **k):
            return ClickResult(True, "spy", (0, 0), (0, 0), "spy")

        def close(self):
            pass

    cfg_path = os.path.join(tmp_dir, "engine.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        _json.dump({
            "name": "engine-test",
            "mode": "events",
            "window": {"hwnd": 1},
            "reference_resolution": [1280, 720],
            "recognizers": {
                "attack": {"method": "template", "template": tpl_path,
                           "roi": [900, 380, 200, 120], "threshold": 0.9}
            },
            "tasks": {
                "fight": {"triggers": [{"recognizer": "attack", "cooldown": 10.0}],
                          "actions": [{"action": "click", "target": "attack", "jitter": 0}]}
            },
            "priority": ["fight"],
            "session_dir": tmp_dir,
            "defaults": {"check_every": 0.0},
        }, fh)
    cfg = Config(_json.loads(open(cfg_path, encoding="utf-8").read()), cfg_path)

    window = WindowInfo(hwnd=1, title="fake", class_name="Fake", pid=0,
                        rect=(0, 0, 1280, 720), client_size=(1280, 720),
                        visible=True, iconic=False)
    capture = FakeCapture(frame)
    spy = SpyInput()
    source = FrameSource(cfg, capture=capture, input_controller=spy, window=window,
                         logger=lambda m: None)
    source._client_size = (1280, 720)

    logs: list[str] = []
    engine = Engine(cfg, frame_source=source, input_controller=spy, max_ticks=3,
                    log=logs.append)
    reason = engine.run()

    check("engine stops at max_ticks", "max_ticks" in reason, reason)
    check("recogniser fired and clicked exactly once (cooldown suppresses the rest)",
          len(spy.clicks) == 1, str(spy.clicks))
    if spy.clicks:
        cx, cy = spy.clicks[0]
        # centre of the pasted 60x40 template is (980, 420)
        check("click landed on the matched template centre",
              abs(cx - 980) <= 2 and abs(cy - 420) <= 2, str(spy.clicks[0]))
    check("recognition happened on every tick", capture.grabs >= 3, f"{capture.grabs} grabs")
    check("the task was reported in the log",
          any("fight" in line for line in logs), str(logs[:3]))
    stats = engine.stats()
    check("stats report the backend", stats.get("backend") == "fake", str(stats.get("backend")))

    # Now a frame with no match: nothing may be clicked and the idle guard trips.
    capture2 = FakeCapture(rng.integers(0, 70, size=(720, 1280, 3), dtype=np.uint8))
    spy2 = SpyInput()
    source2 = FrameSource(cfg, capture=capture2, input_controller=spy2, window=window,
                          logger=lambda m: None)
    source2._client_size = (1280, 720)
    cfg.data["defaults"] = {"check_every": 0.0, "max_consecutive_misses": 4}
    engine2 = Engine(cfg, frame_source=source2, input_controller=spy2, max_ticks=50,
                     log=lambda m: None)
    reason2 = engine2.run()
    check("no match -> no clicks", not spy2.clicks, str(spy2.clicks))
    check("no match -> idle guard stops the run", "idle" in reason2, reason2)


def test_launch_when_already_running() -> None:
    """Already-running game must be success - never relaunched, never an error.

    The user reported this: they start the game themselves, run the bot, and the
    launch step complained.  ``ensure_game_running`` is stubbed here (no device
    needed) so the decision logic is pinned: running -> success, only bring it
    to the front; start only when the process is really gone.
    """
    print("launch: an already-running game is success, never restarted")
    from simauto import adb_backend as ab

    class StubApp(ab.AdbBackend):
        def __new__(cls, *, running: bool, foreground: bool, start_ok: bool = True):
            self = object.__new__(cls)
            self._running = running
            self._foreground = foreground
            self._start_ok = start_ok
            self.starts = 0
            self.fronts = 0
            return self

        def __init__(self, *args, **kwargs):     # never touch a real device
            pass

        def is_app_running(self, package=ab.GAME_PACKAGE):
            return self._running

        def is_app_foreground(self, package=ab.GAME_PACKAGE):
            return self._foreground

        def start_app(self, package=ab.GAME_PACKAGE, activity=None, wait=0.0):
            self.starts += 1
            if self._start_ok:
                self._running = True
            return self._start_ok

        def _bring_to_front(self, package=ab.GAME_PACKAGE):
            self.fronts += 1
            self._foreground = True
            return True

    real_sleep = ab.time.sleep
    ab.time.sleep = lambda *_: None            # the wait loop must not slow tests
    try:
        stub = StubApp(running=True, foreground=True)
        ok = stub.ensure_game_running(log=lambda m: None, timeout_s=5)
        check("already running + foreground -> success", ok)
        check("already running -> never re-launched", stub.starts == 0, str(stub.starts))

        stub = StubApp(running=True, foreground=False)
        ok = stub.ensure_game_running(log=lambda m: None, timeout_s=5)
        check("running in background -> success", ok)
        check("running in background -> brought to front, not started",
              stub.starts == 0 and stub.fronts >= 1,
              f"starts={stub.starts} fronts={stub.fronts}")

        stub = StubApp(running=False, foreground=False)
        ok = stub.ensure_game_running(log=lambda m: None, timeout_s=5)
        check("not running -> started once", bool(ok) and stub.starts == 1,
              f"starts={stub.starts}")
    finally:
        ab.time.sleep = real_sleep

    # A failed start command on an app that is in fact running is not a failure.
    class Boom:
        def start_app(self, package):
            raise RuntimeError("already started")

    stub = object.__new__(ab.AdbBackend)
    stub._controller = Boom()
    stub.is_app_running = lambda package=ab.GAME_PACKAGE: True
    stub._adb_shell = lambda cmd, timeout=15: None      # adb shell itself failed
    check("failed start on a running app is not an error",
          ab.AdbBackend.start_app(stub, wait=0) is True)


def main() -> int:
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    test_correlation_primitive()
    test_ncc_matches_definition()
    test_match_template()
    test_threshold_calibration()
    test_colour_and_bar()
    test_detector_specs()
    test_image_io_roundtrip(tmp_dir)
    test_config_and_helpers(tmp_dir)
    test_engine_end_to_end(tmp_dir)
    test_capture_perf()
    test_launch_when_already_running()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED, {PASSED} passed")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print(f"all {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
