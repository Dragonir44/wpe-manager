"""Per-screen assignment + playlist rotation.

The controller owns the *desired* state (each screen shows either a single
wallpaper or a rotating playlist) and translates it into the concrete
screen -> wallpaper-id map that engine.apply() reconciles. A single QTimer
advances any playlist whose interval has elapsed; because engine.apply() only
restarts screens whose wallpaper actually changed, advancing one screen never
disturbs the others.

Rotation only advances while the app's event loop is running (i.e. while the
GUI is open). The --autostart entry restores the first frame of each screen;
continuous rotation resuming headless is a future (tray/daemon) job.
"""
from __future__ import annotations

import random
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import config, engine

# How often we check whether any playlist is due to advance.
_TICK_MS = 5000


class RotationController(QObject):
    changed = Signal()  # concrete state changed — refresh the UI

    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.assignments: dict[str, dict] = config.load_assignments()
        self.playlists: dict[str, dict] = {p["name"]: p for p in config.load_playlists()}
        self._pos: dict[str, int] = {}          # screen -> index into playlist ids
        self._next_due: dict[str, float] = {}    # screen -> monotonic deadline
        self._paused = False                      # auto-pause: wallpapers stopped, assignments kept
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    # -- playlist storage -------------------------------------------------- #
    def playlist_names(self) -> list[str]:
        return sorted(self.playlists.keys(), key=str.casefold)

    def save_playlists(self) -> None:
        config.save_playlists(list(self.playlists.values()))

    def upsert_playlist(self, playlist: dict) -> None:
        self.playlists[playlist["name"]] = playlist
        self.save_playlists()

    def delete_playlist(self, name: str) -> None:
        self.playlists.pop(name, None)
        self.save_playlists()
        # Drop any screen still pointing at it.
        for screen, a in list(self.assignments.items()):
            if a.get("mode") == "playlist" and a.get("playlist") == name:
                del self.assignments[screen]
        self.apply()

    # -- assignment -------------------------------------------------------- #
    def assign_single(self, screen: str, wid: str) -> None:
        self.assignments[screen] = {"mode": "single", "id": wid}
        self._pos.pop(screen, None)
        self._next_due.pop(screen, None)
        self.apply()

    def assign_playlist(self, screen: str, name: str) -> None:
        if name not in self.playlists:
            return
        self.assignments[screen] = {"mode": "playlist", "playlist": name}
        self._pos[screen] = self._start_pos(name)
        self.apply()

    def _start_pos(self, name: str) -> int:
        """Where a playlist begins when (re)started: the first wallpaper if
        'start_first' is set or the order is sequential; a random one if the
        order is random (so random playlists don't always open on item 0)."""
        pl = self.playlists.get(name)
        ids = pl.get("ids", []) if pl else []
        if not ids:
            return 0
        if pl.get("start_first") or pl.get("order") != "random":
            return 0
        return random.randrange(len(ids))

    def _overlap_for(self, screen: str) -> int:
        """Transition (overlap) in ms for a screen: a per-playlist override if
        set, otherwise the global setting."""
        a = self.assignments.get(screen)
        if a and a.get("mode") == "playlist":
            pl = self.playlists.get(a.get("playlist", ""))
            if pl and pl.get("overlap_ms") is not None:
                return int(pl["overlap_ms"])
        return int(self.cfg.overlap_ms)

    def clear(self, screen: str) -> None:
        self.assignments.pop(screen, None)
        self._pos.pop(screen, None)
        self._next_due.pop(screen, None)
        self.apply()

    def stop_all(self) -> None:
        self._paused = False
        self.assignments.clear()
        self._pos.clear()
        self._next_due.clear()
        self._timer.stop()
        engine.stop()
        config.save_assignments({})
        self.changed.emit()

    # -- auto-pause (driven by autopause.AutoPauseWatcher) ----------------- #
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop every wallpaper process to free the GPU, keeping the desired
        assignments intact so resume() can restore them. Used while a game or
        other heavy app runs; unlike stop_all() this is meant to be temporary."""
        if self._paused:
            return
        self._paused = True
        self._timer.stop()
        engine.stop()
        self.changed.emit()

    def resume(self) -> None:
        """Relaunch the wallpapers that pause() stopped, from the (retained)
        assignments — including any edits made while paused."""
        if not self._paused:
            return
        self._paused = False
        self.apply()

    # -- describe current state (for the UI) ------------------------------- #
    def describe(self, screen: str) -> str:
        a = self.assignments.get(screen)
        if not a:
            return "—"
        if a["mode"] == "single":
            return f"fond : {a['id']}"
        return f"playlist : {a.get('playlist')}"

    def current_id(self, screen: str) -> str | None:
        a = self.assignments.get(screen)
        if not a:
            return None
        if a["mode"] == "single":
            return a.get("id")
        pl = self.playlists.get(a.get("playlist", ""))
        if not pl or not pl.get("ids"):
            return None
        idx = self._pos.get(screen, 0) % len(pl["ids"])
        return pl["ids"][idx]

    # -- engine driving ---------------------------------------------------- #
    def _concrete(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for screen in self.assignments:
            wid = self.current_id(screen)
            if wid:
                out[screen] = wid
        return out

    def _reset_due(self) -> None:
        now = time.monotonic()
        self._next_due.clear()
        has_playlist = False
        for screen, a in self.assignments.items():
            if a.get("mode") == "playlist":
                pl = self.playlists.get(a.get("playlist", ""))
                if pl and len(pl.get("ids", [])) > 1:
                    self._next_due[screen] = now + max(1, pl.get("interval_min", 30)) * 60
                    has_playlist = True
        if has_playlist and not self._timer.isActive():
            self._timer.start()
        elif not has_playlist:
            self._timer.stop()

    def refresh_wallpaper(self, wid: str) -> None:
        """Relaunch any screen currently showing `wid` so new property overrides
        take effect (the backend applies them only at launch)."""
        for screen, proc in list(engine.snapshot().items()):
            if proc.get("id") == wid:
                self._swap(screen, wid, force=True)

    def _swap(self, screen: str, wid: str, force: bool = False) -> None:
        """Switch one screen to `wid` with an overlap: start the new wallpaper,
        let it render, then kill the old one — no black gap. `force` relaunches
        even when the same wallpaper is already showing (e.g. after a property
        change)."""
        old = engine.get_proc(screen)
        if not force and old and old.get("id") == wid and engine.alive(old.get("pid")):
            return  # already showing it
        new_pid = engine.start_screen(self.cfg, screen, wid)
        engine.register(screen, new_pid, wid)
        old_pid = old.get("pid") if old else None
        if engine.alive(old_pid):
            delay = max(0, self._overlap_for(screen))
            QTimer.singleShot(delay, lambda p=old_pid: engine.kill_pid(p))

    def apply(self) -> None:
        # Initialise the start position for playlist screens not yet started
        # this session (so random playlists open on a random wallpaper).
        for screen, a in self.assignments.items():
            if a.get("mode") == "playlist" and screen not in self._pos:
                self._pos[screen] = self._start_pos(a.get("playlist", ""))
        # While auto-paused, persist assignment edits but launch nothing and
        # keep the rotation timer stopped — resume() will bring them on screen.
        if self._paused:
            config.save_assignments(self.assignments)
            self.changed.emit()
            return
        concrete = self._concrete()
        # Stop screens that are no longer assigned.
        for screen in list(engine.snapshot()):
            if screen not in concrete:
                engine.kill_screen(screen)
        # (Re)apply each assigned screen with an overlapping swap.
        for screen, wid in concrete.items():
            self._swap(screen, wid)
        config.save_assignments(self.assignments)
        self._reset_due()
        self.changed.emit()

    def _advance(self, screen: str) -> None:
        a = self.assignments.get(screen)
        pl = self.playlists.get(a.get("playlist", "")) if a else None
        if not pl:
            return
        ids = pl.get("ids", [])
        n = len(ids)
        if n <= 1:
            return
        cur = self._pos.get(screen, 0) % n
        if pl.get("order") == "random":
            nxt = random.randrange(n - 1)
            if nxt >= cur:  # skip the current one → never repeat back-to-back
                nxt += 1
        else:
            nxt = (cur + 1) % n
        self._pos[screen] = nxt

    def _tick(self) -> None:
        now = time.monotonic()
        due = [s for s, t in self._next_due.items() if now >= t]
        if not due:
            return
        for screen in due:
            self._advance(screen)
            wid = self.current_id(screen)
            if wid:
                self._swap(screen, wid)  # overlapping swap → no black gap
        for screen in due:
            pl = self.playlists.get(self.assignments[screen].get("playlist", ""))
            self._next_due[screen] = now + max(1, pl.get("interval_min", 30)) * 60
        self.changed.emit()
