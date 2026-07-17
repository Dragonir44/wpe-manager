"""Driving the linux-wallpaperengine backend — one process per screen.

Running an independent process per screen means changing or adding a wallpaper
on one screen never disturbs the others (no flicker on already-running screens).
A small JSON map (engine.json) records the pid and wallpaper id currently live
on each screen so that apply() can reconcile: only the screens whose assignment
actually changed get torn down and relaunched.

Every process is launched detached (own session/process group) so it survives
the GUI closing; pids are persisted so a later GUI session or the --autostart
entry can find and manage them.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess

from . import config

BACKEND = "linux-wallpaperengine"


# --------------------------------------------------------------------------- #
# Process bookkeeping
# --------------------------------------------------------------------------- #
def _load_procs() -> dict[str, dict]:
    if config.ENGINE_FILE.is_file():
        try:
            data = json.loads(config.ENGINE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_procs(procs: dict[str, dict]) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.ENGINE_FILE.write_text(json.dumps(procs, indent=2))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)  # whole detached group
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


# --------------------------------------------------------------------------- #
# Command building / launching
# --------------------------------------------------------------------------- #
def build_command(cfg: config.Config, screen: str, wid: str) -> list[str]:
    """Backend command for a single screen.

    We always pass the *full path* to the wallpaper folder (not just the id)
    and an explicit --assets-dir — the two things the other GUIs got wrong on a
    non-default Steam library.
    """
    cmd = [BACKEND]
    if cfg.assets_dir:
        cmd += ["--assets-dir", cfg.assets_dir]
    if cfg.silent:
        cmd += ["--silent"]
    if cfg.fps and cfg.fps != 30:
        cmd += ["--fps", str(cfg.fps)]
    library = cfg.library_path
    target = str(library / wid) if library else wid
    cmd += ["--screen-root", screen, "--bg", target]
    return cmd


def _launch(cfg: config.Config, screen: str, wid: str) -> int:
    cmd = build_command(cfg, screen, wid)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def is_running() -> bool:
    return any(_alive(int(p["pid"])) for p in _load_procs().values() if "pid" in p)


# -- fine-grained control (used by the overlap/transition swap) ------------- #
def snapshot() -> dict[str, dict]:
    return _load_procs()


def get_proc(screen: str) -> dict | None:
    return _load_procs().get(screen)


def alive(pid) -> bool:
    return isinstance(pid, int) and _alive(pid)


def start_screen(cfg: config.Config, screen: str, wid: str) -> int:
    """Launch a wallpaper for one screen WITHOUT touching any existing one."""
    return _launch(cfg, screen, wid)


def register(screen: str, pid: int, wid: str) -> None:
    procs = _load_procs()
    procs[screen] = {"pid": pid, "id": wid}
    _save_procs(procs)


def kill_pid(pid: int) -> None:
    if _alive(pid):
        _kill(pid)


def kill_screen(screen: str) -> None:
    procs = _load_procs()
    entry = procs.pop(screen, None)
    if entry and _alive(int(entry.get("pid", -1))):
        _kill(int(entry["pid"]))
    _save_procs(procs)


def stop() -> None:
    """Kill every running screen process."""
    for entry in _load_procs().values():
        pid = entry.get("pid")
        if isinstance(pid, int) and _alive(pid):
            _kill(pid)
    _save_procs({})


def apply(cfg: config.Config, concrete: dict[str, str]) -> None:
    """Reconcile running processes with a concrete screen -> wallpaper-id map.

    Only screens whose wallpaper changed (or whose process died) are restarted;
    screens dropped from the map are stopped. Untouched screens keep running
    as-is — no flicker. Persisting the *desired* assignments (single/playlist)
    is the caller's job; here we only deal with what's on screen right now.
    """
    active = {s: w for s, w in concrete.items() if w}

    procs = _load_procs()
    new_procs: dict[str, dict] = {}

    # Screens no longer wanted → kill.
    for screen, entry in procs.items():
        if screen not in active:
            pid = entry.get("pid")
            if isinstance(pid, int) and _alive(pid):
                _kill(pid)

    # Wanted screens → (re)launch only when needed.
    for screen, wid in active.items():
        entry = procs.get(screen)
        pid = entry.get("pid") if entry else None
        same = bool(entry) and entry.get("id") == wid and isinstance(pid, int) and _alive(pid)
        if same:
            new_procs[screen] = entry  # leave it running untouched
            continue
        if isinstance(pid, int) and _alive(pid):
            _kill(pid)  # same screen, different wallpaper → restart just this one
        new_procs[screen] = {"pid": _launch(cfg, screen, wid), "id": wid}

    _save_procs(new_procs)
