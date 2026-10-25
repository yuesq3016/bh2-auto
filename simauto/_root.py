"""Project-root resolution that also works inside a PyInstaller bundle."""

from __future__ import annotations

import os
import sys


def project_root() -> str:
    """Directory that holds configs/, templates/ and vendor/.

    - Source checkout: the parent of the `simauto` package.
    - Frozen (PyInstaller) build: the directory holding the .exe, so the
      distributed zip can keep assets next to the executable.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def project_path(*parts: str) -> str:
    return os.path.join(project_root(), *parts)
