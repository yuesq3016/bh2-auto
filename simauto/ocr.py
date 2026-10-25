"""Text recognition, via MaaFramework's bundled PaddleOCR.

Why this exists: template matching fails whenever the *appearance* of a control
changes - a button gets reskinned, the background art behind the nav bar
changes, the game applies a seasonal theme.  Every one of those was hit during
development of this project, and each time the fix was to re-crop a template.

Text does not change.  ``点击领取奖励``, ``一键派遣``, ``使魔探险``, ``开战`` are the
same strings whatever colour the button is.  Recognising the words instead of
the pixels removes that whole class of maintenance.

The OCR engine itself is PaddleOCR PP-OCRv4 (det + rec ONNX), shipped as data
files and driven by MaaFramework's ``fastdeploy_ppocr`` plugin - so nothing here
trains a model or needs a GPU.  The model files are the ones MAAHG2 (MIT) ships
under ``assets/resource/mix/model/ocr``; point ``ocr.model_dir`` at any
equivalent directory.

Deliberately *not* using MaaFramework's own pipeline/recognition layer: its
``expected`` filter reported ``hit=false`` for a perfect match in testing, and
its results are wrapped in a TaskDetail that hides the per-string boxes.  Calling
the recognition and reading ``raw_detail['all']`` gives every recognised string
with its box and score, which is what a task engine actually needs.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .imaging import ImageError, crop_roi, encode_png as save_png_bytes
from .maa_backend import MaaBackendUnavailable, init_maa_environment

__all__ = ["OcrEngine", "TextHit", "ocr_available", "default_model_dir", "terminate_ocr"]


# Where to look for the OCR model files, in order.  The project-local copy wins
# so the tool keeps working after the source folder (a Downloads directory) is
# cleaned up.  A directory is acceptable if it holds det.onnx + rec.onnx +
# keys.txt, either directly or in an "ocr" child.
_MODEL_DIR_HINTS = (
    "vendor/ocr-model",
    "vendor/maa-ocr",
    ".maa/ocr-model",
    r"C:\Users\14520\Downloads\MAAHG2-win-x86_64-v1.1.0\assets\resource\mix\model\ocr",
    r"C:\Users\14520\Downloads\MAAHG2-win-x86_64-v1.1.0\assets\resource\mix\model",
)


@dataclass
class TextHit:
    """One recognised string."""

    text: str
    score: float
    box: tuple[int, int, int, int]          # x, y, w, h

    @property
    def loc(self) -> tuple[int, int]:
        x, y, w, h = self.box
        return (x + w // 2, y + h // 2)

    def describe(self) -> str:
        return f"{self.text!r} score={self.score:.3f} box={self.box}"


def default_model_dir() -> str | None:
    """First existing OCR model directory."""
    for hint in _MODEL_DIR_HINTS:
        path = hint if os.path.isabs(hint) else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), hint)
        if _looks_like_model_dir(path):
            return path
        child = os.path.join(path, "ocr")
        if _looks_like_model_dir(child):
            return child
    return None


def _looks_like_model_dir(path: str) -> bool:
    return all(os.path.exists(os.path.join(path, f))
               for f in ("det.onnx", "rec.onnx", "keys.txt"))


def ocr_available(model_dir: str | None = None) -> bool:
    """True when the binding is importable and the model files are present."""
    try:
        from maa.pipeline import JOCR  # noqa: F401
        from maa.tasker import Tasker  # noqa: F401
    except ImportError:
        return False
    return bool(model_dir or default_model_dir())


class OcrEngine:
    """Lazy, process-wide OCR session.

    Loading the models costs a couple of seconds and a few hundred MB, so one
    instance is shared.  Everything is guarded by a lock because MaaFramework
    runs its controllers on internal threads.
    """

    _instance: "OcrEngine | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, model_dir: str | None = None, controller: Any = None):
        self.model_dir = model_dir or default_model_dir()
        if not self.model_dir:
            raise MaaBackendUnavailable(
                "no OCR model directory found (needs det.onnx + rec.onnx + keys.txt). "
                "MAAHG2 ships one at "
                r"C:\Users\14520\Downloads\MAAHG2-win-x86_64-v1.1.0\assets\resource\mix\model\ocr"
            )
        self._lock = threading.RLock()
        self._tasker = None
        self._controller = controller
        self._owns_controller = False
        self.strings_seen = 0
        self.images_done = 0
        self.total_ms = 0.0

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def shared(cls, model_dir: str | None = None, controller: Any = None) -> "OcrEngine":
        """The process-wide engine.

        The first caller's ``model_dir`` wins (a later one cannot move the loaded
        models), so a config that names its own directory is honoured as long as
        nothing else created the engine first.
        """
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(model_dir, controller)
            elif controller is not None and cls._instance._controller is None:
                cls._instance._controller = controller
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:
            inst, cls._instance = cls._instance, None
        if inst is not None:
            inst.close()

    def _ensure(self) -> None:
        """Build the Resource+Tasker once."""
        with self._lock:
            if self._tasker is not None:
                return
            init_maa_environment()
            from maa.resource import Resource  # type: ignore
            from maa.tasker import Tasker  # type: ignore

            resource = Resource()
            resource.post_ocr_model(self.model_dir).wait()
            if self._controller is None:
                # Tasker.inited requires *a* controller even though we always
                # feed images in directly, so hand it a do-nothing one.  The
                # debug controller was tried first and refused to construct
                # without a directory of valid images; an empty custom
                # controller has no such requirements and needs no device.
                from maa.controller import CustomController  # type: ignore

                class _NullController(CustomController):
                    def connect(self) -> bool:
                        return True

                    def request_uuid(self) -> str:
                        return "simauto-ocr"

                    def start_app(self, intent: str) -> bool:
                        return True

                    def stop_app(self, intent: str) -> bool:
                        return True

                    def screencap(self) -> np.ndarray:
                        # Never called: every recognition gets an explicit image.
                        return np.zeros((8, 8, 4), dtype=np.uint8)

                    def click(self, x: int, y: int) -> bool:
                        return True

                    def swipe(self, x1: int, y1: int, x2: int, y2: int,
                              duration: int) -> bool:
                        return True

                    def touch_down(self, contact: int, x: int, y: int,
                                   pressure: int) -> bool:
                        return True

                    def touch_move(self, contact: int, x: int, y: int,
                                   pressure: int) -> bool:
                        return True

                    def touch_up(self, contact: int) -> bool:
                        return True

                    def click_key(self, keycode: int) -> bool:
                        return True

                    def input_text(self, text: str) -> bool:
                        return True

                    def key_down(self, keycode: int) -> bool:
                        return True

                    def key_up(self, keycode: int) -> bool:
                        return True

                self._controller = _NullController()
                self._controller.post_connection().wait()
                self._owns_controller = True

            tasker = Tasker()
            tasker.bind(resource, self._controller)
            if not tasker.inited:
                raise MaaBackendUnavailable(
                    "MaaFramework Tasker failed to initialise for OCR "
                    f"(model dir {self.model_dir})"
                )
            self._tasker = tasker
            self._resource = resource

    def close(self) -> None:
        with self._lock:
            self._tasker = None

    # -- recognition -------------------------------------------------------

    def recognize(self, frame: np.ndarray,
                  roi: tuple[int, int, int, int] | None = None) -> list[TextHit]:
        """All text in ``frame`` (or in ``roi``), with boxes in frame coordinates.

        Boxes come back relative to the cropped ROI, so they are offset back to
        frame space here - otherwise a caller restricting the search area would
        get coordinates it cannot click.
        """
        self._ensure()
        region = crop_roi(frame, roi)
        if region.size == 0:
            raise ImageError("empty ROI for OCR")
        origin = (roi[0], roi[1]) if roi else (0, 0)

        from maa.pipeline import JOCR, JRecognitionType  # type: ignore

        img = np.ascontiguousarray(region, dtype=np.uint8)
        t0 = time.perf_counter()
        with self._lock:
            job = self._tasker.post_recognition(
                JRecognitionType.OCR, JOCR(expected=[]), img
            )
            job.wait()
            detail = job.get()
        self.total_ms += (time.perf_counter() - t0) * 1000.0
        self.images_done += 1

        hits: list[TextHit] = []
        raw = self._raw_all(detail)
        for item in raw:
            try:
                box = [int(v) for v in item["box"]]
                text = str(item.get("text", ""))
            except (KeyError, TypeError, ValueError):
                continue
            if not text:
                continue
            if origin != (0, 0):
                box[0] += origin[0]
                box[1] += origin[1]
            hits.append(TextHit(text=text, score=float(item.get("score", 0.0)),
                                box=(box[0], box[1], box[2], box[3])))
        self.strings_seen += len(hits)
        return hits

    @staticmethod
    def _raw_all(detail: Any) -> list[dict]:
        """Dig ``raw_detail['all']`` out of whatever wrapper came back."""
        if detail is None:
            return []
        # TaskDetail -> its nodes -> recognition detail
        nodes = getattr(detail, "nodes", None)
        if nodes:
            for node in nodes:
                rd = getattr(node, "recognition", None)
                if rd is None:
                    continue
                found = OcrEngine._raw_all(rd)
                if found:
                    return found
        raw = getattr(detail, "raw_detail", None)
        if isinstance(raw, dict):
            all_items = raw.get("all") or raw.get("results") or []
            if isinstance(all_items, list):
                return [i for i in all_items if isinstance(i, dict)]
        # Fall back to the typed results if raw_detail is unavailable.
        results = getattr(detail, "all_results", None) or []
        out = []
        for r in results:
            box = getattr(r, "box", None)
            if box is None:
                continue
            try:
                coords = [int(v) for v in box]
            except TypeError:
                continue
            out.append({"box": coords, "text": getattr(r, "text", ""),
                        "score": float(getattr(r, "score", 0.0))})
        return out

    def find(self, frame: np.ndarray, wanted: Iterable[str],
             *, roi: tuple[int, int, int, int] | None = None,
             threshold: float = 0.5, mode: str = "contains") -> TextHit | None:
        """Best hit whose text matches any of ``wanted``.

        ``mode``: ``contains`` (default, substring), ``exact``, or ``startswith``.
        Substring matching is the useful default because OCR occasionally splits
        a label or drops a character; requiring an exact string would make the
        whole thing brittle again.
        """
        wanted = [w for w in wanted if w]
        if not wanted:
            return None
        hits = self.recognize(frame, roi)
        best: TextHit | None = None
        for hit in hits:
            if hit.score < threshold:
                continue
            for needle in wanted:
                if mode == "exact":
                    ok = hit.text == needle
                elif mode == "startswith":
                    ok = hit.text.startswith(needle)
                else:
                    ok = needle in hit.text
                if ok:
                    # Prefer the longer matched needle, then the higher score:
                    # "一键领取" should win over "领取" when both are present.
                    rank = (len(needle), hit.score)
                    if best is None or rank > getattr(best, "_rank", (-1, -1)):
                        hit._rank = rank        # type: ignore[attr-defined]
                        best = hit
                    break
        return best


def terminate_ocr() -> None:
    """Drop the shared engine (used by tests and at shutdown)."""
    OcrEngine.reset()
