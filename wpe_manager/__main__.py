"""Entry point.

    python -m wpe_manager              launch the GUI (with a system-tray icon)
    python -m wpe_manager --daemon     start hidden in the tray and re-apply the
                                       saved wallpapers, so playlist rotation
                                       resumes at session login. Suitable for an
                                       autostart entry (see config.install_autostart).

    --autostart is kept as an alias of --daemon for older autostart entries.

Only ONE instance runs at a time. A second launch just tells the running one to
show its window (and exits), so the daemon started at login and a later manual
launch never fight over the config files.
"""
from __future__ import annotations

import os
import sys

from . import config

_SERVER_NAME = f"wpe-manager-{os.getuid()}"


def _signal_existing(daemon: bool) -> bool:
    """If another instance is listening, ask it to show its window and return
    True (we should exit). Return False if we're the first instance."""
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_SERVER_NAME)
    if not sock.waitForConnected(300):
        return False
    # A daemon relaunch shouldn't pop the window open; a plain launch should.
    sock.write(b"noop" if daemon else b"show")
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def main() -> int:
    daemon = "--daemon" in sys.argv or "--autostart" in sys.argv

    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from .gui import MainWindow, app_icon

    app = QApplication(sys.argv)
    app.setApplicationName("Wallpaper Engine Manager")
    app.setWindowIcon(app_icon())
    # The tray keeps the app alive when the window is hidden/closed.
    app.setQuitOnLastWindowClosed(False)

    # Single-instance guard: hand off to a running instance if there is one.
    if _signal_existing(daemon):
        return 0

    cfg = config.load_config()
    win = MainWindow(cfg)

    # We're the primary instance: listen so later launches can reach us.
    QLocalServer.removeServer(_SERVER_NAME)  # clear a stale socket if any
    server = QLocalServer(app)
    server.listen(_SERVER_NAME)

    def _on_new_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return
        if conn.waitForReadyRead(300):
            msg = bytes(conn.readAll().data())
            if msg.startswith(b"show"):
                win.showNormal()
                win.raise_()
                win.activateWindow()
        conn.disconnectFromServer()

    server.newConnection.connect(_on_new_connection)

    if daemon:
        # Resume the saved assignments (launches wallpapers, arms rotation
        # timers) and stay hidden in the tray.
        if cfg.is_usable():
            win.controller.apply()
        # If there's no tray to live in, fall back to showing the window so the
        # app isn't invisible and unreachable.
        if win._tray is None:
            win.show()
    else:
        win.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
