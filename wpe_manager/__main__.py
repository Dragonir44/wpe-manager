"""Entry point.

    python -m wpe_manager              launch the GUI
    python -m wpe_manager --autostart  re-apply the saved wallpapers, no window
                                       (replaces this process with the backend,
                                       so it's suitable for a KDE autostart entry)
"""
from __future__ import annotations

import sys

from . import config, engine


def _autostart() -> int:
    """Re-apply the saved wallpapers (one detached process per screen) and exit.

    The launched processes are detached (own session), so they keep running
    after this short-lived entry point returns.
    """
    cfg = config.load_config()
    assignments = {s: w for s, w in config.load_assignments().items() if w}
    if not cfg.is_usable() or not assignments:
        return 0
    engine.stop()  # clear any stale pids before repopulating
    engine.apply(cfg, assignments)
    return 0


def main() -> int:
    if "--autostart" in sys.argv:
        return _autostart()

    from PySide6.QtWidgets import QApplication

    from .gui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Wallpaper Engine Manager")
    cfg = config.load_config()
    win = MainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
