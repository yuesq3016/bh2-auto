"""PyInstaller entry point: no arguments opens the GUI, otherwise the CLI."""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        from simauto.gui import main as gui_main

        return gui_main()

    from simauto.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
