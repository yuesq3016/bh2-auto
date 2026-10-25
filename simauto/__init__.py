"""simauto — a small, dependency-light image-recognition bot for Android
emulators running on Windows.

The design follows the lessons of MAA (MaaAssistantArknights) and MaaFramework:
tasks are data, recognition is template/colour matching against small ROIs in a
reference resolution, and the loop polls instead of modelling game state.

Quick start::

    python -m simauto list                        # find your emulator window
    python -m simauto doctor                      # environment + window check
    python -m simauto probe --title MuMu          # which capture backend works
    python -m simauto grab --title MuMu -o frame.png
    python -m simauto crop --frame frame.png --roi 1000,600,200,100 -o templates/attack.png
    python -m simauto match --frame frame.png --template templates/attack.png
    python -m simauto run --config configs/bh2-starter.json --debug

Capture goes through one of two interchangeable backends: the built-in GDI
implementation, or MaaFramework's Win32 controller (``pip install maafw``),
which adds FramePool/PrintWindow capture that works on GPU-rendered emulator
windows where GDI returns black.  ``backend: auto`` prefers MaaFramework.
"""

from __future__ import annotations

import os
import sys

__version__ = "0.2.0"


def _enable_vendor_dir() -> str | None:
    """Put the project's ``vendor/`` directory on ``sys.path``.

    ``tools/setup-maafw.ps1`` unpacks the maafw wheel there instead of running
    pip, because a wheel is just a ZIP and this machine's Python cannot write
    to its own site-packages.  Adding the directory here means the optional
    MaaFramework backend works with no installation step at all.
    """
    vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.append(vendor)
        return vendor
    return None


VENDOR_DIR = _enable_vendor_dir()

from .engine import Config, Engine, EngineError, FrameSource, load_config
from .imaging import ImageError, load_image, save_image
from .inputs import InputController
from .maa_backend import (
    MaaBackend, MaaBackendUnavailable, maa_available, probe_screencap_methods,
)
from .matcher import MatchResult, match_bar, match_color, match_template, run_detector
from .scanner import WindowCapture, WindowInfo, find_window, list_windows, probe_window, resolve_window

__all__ = [
    "__version__",
    "VENDOR_DIR",
    "Config", "Engine", "EngineError", "FrameSource", "load_config",
    "ImageError", "load_image", "save_image",
    "InputController",
    "MaaBackend", "MaaBackendUnavailable", "maa_available", "probe_screencap_methods",
    "MatchResult", "match_bar", "match_color", "match_template", "run_detector",
    "WindowCapture", "WindowInfo", "find_window", "list_windows",
    "probe_window", "resolve_window",
]
