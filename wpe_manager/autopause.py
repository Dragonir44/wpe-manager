"""Automatically pause the wallpapers while chosen apps are running.

Live wallpapers are continuous GPU renderers: a game sharing the GPU with them
loses a lot of frames. The backend does pause a wallpaper when a *true*
fullscreen window covers its output, but that misses two very common cases:

  * multi-monitor — only the covered screen pauses; the wallpapers on the other
    monitors keep rendering and still share the same GPU;
  * borderless-windowed games (what most streamers run) — never seen as
    fullscreen, so nothing pauses at all.

This watcher polls the running processes and, as soon as any app from a
user-defined list appears, asks the rotation controller to stop *every*
wallpaper — freeing the GPU completely. When the app is gone, the wallpapers
are restored automatically. Matching on the executable name (not the window
state) is what makes it work regardless of fullscreen vs borderless.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time

from PySide6.QtCore import QObject, QTimer

_POLL_MS = 2500
_OWN_PID = os.getpid()

# GPU-load fallback tuning. Pausing removes the wallpapers' own GPU cost, so we
# use hysteresis (a lower resume threshold) plus sustained windows to avoid
# flapping: a brief spike must persist to pause, and load must stay low a while
# to resume.
_GPU_BUSY_GLOB = "/sys/class/drm/card[0-9]*/device/gpu_busy_percent"
_GPU_HYSTERESIS = 25       # resume threshold = pause threshold - this
_GPU_HIGH_SECONDS = 6.0    # busy must stay >= threshold this long to pause
_GPU_LOW_SECONDS = 8.0     # busy must stay < resume threshold this long to resume


# nvidia-smi discovery is cached: resolve the binary once, then reuse (or skip
# entirely on machines that don't have it).
_nvidia_smi: dict = {"checked": False, "path": None}


def _amd_busy() -> int | None:
    """Busiest AMD GPU from the amdgpu sysfs `gpu_busy_percent` attribute."""
    best: int | None = None
    for path in glob.glob(_GPU_BUSY_GLOB):
        try:
            with open(path) as fh:
                v = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        if best is None or v > best:
            best = v
    return best


def _nvidia_busy() -> int | None:
    """Busiest NVIDIA GPU via `nvidia-smi` (proprietary driver), or None.

    Uses the stable CSV query interface; a GPU reporting `[N/A]` (utilisation
    unsupported) is skipped rather than treated as 0."""
    if not _nvidia_smi["checked"]:
        _nvidia_smi["path"] = shutil.which("nvidia-smi")
        _nvidia_smi["checked"] = True
    exe = _nvidia_smi["path"]
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    best: int | None = None
    for line in out.stdout.splitlines():
        try:
            v = int(line.strip())
        except ValueError:
            continue
        if best is None or v > best:
            best = v
    return best


def gpu_busy_percent() -> int | None:
    """Current GPU utilisation (0-100), or None if it can't be read.

    Reads AMD's amdgpu sysfs `gpu_busy_percent` first (fast, no subprocess);
    falls back to `nvidia-smi` on NVIDIA's proprietary driver. Intel and
    nouveau expose no simple percentage here, so the caller disables the
    GPU-load trigger (the app-list trigger still works everywhere)."""
    amd = _amd_busy()
    if amd is not None:
        return amd
    return _nvidia_busy()


def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_names(pid: str) -> tuple[str, str]:
    """(comm, executable basename) for a pid; empty strings if unreadable.

    `comm` is the kernel's process name, truncated to 15 chars; the basename of
    argv[0] is usually the full, more descriptive name (e.g. the untruncated
    launcher name), so we keep both for matching."""
    comm = _read(f"/proc/{pid}/comm").strip()
    cmdline = _read(f"/proc/{pid}/cmdline")
    argv0 = cmdline.split("\0", 1)[0] if cmdline else ""
    base = os.path.basename(argv0) if argv0 else ""
    return comm, base


def _iter_user_procs():
    """Yield (comm, basename) for this user's real processes.

    Skips our own process and anything owned by another user — we only ever act
    on the current user's apps. Kernel threads (no cmdline) still yield their
    comm, which is harmless for matching."""
    uid = os.getuid()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return
    for pid in pids:
        if not pid.isdigit() or int(pid) == _OWN_PID:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        comm, base = _proc_names(pid)
        if comm or base:
            yield comm, base


def running_app_names() -> list[str]:
    """Distinct, human-pickable names of the apps currently running.

    Prefers the (untruncated) executable basename over `comm`. Deduplicated and
    case-insensitively sorted — this feeds the "add from running apps" picker."""
    names: set[str] = set()
    for comm, base in _iter_user_procs():
        name = base or comm
        if name:
            names.add(name)
    return sorted(names, key=str.casefold)


def any_running(patterns: list[str]) -> bool:
    """True if any configured pattern matches a running process.

    A pattern matches when it equals, or is contained in, a process's `comm` or
    executable basename (case-insensitive) — tolerant of version suffixes and
    of `comm`'s 15-char truncation."""
    pats = [p.casefold() for p in patterns if p]
    if not pats:
        return False
    for comm, base in _iter_user_procs():
        for h in (comm.casefold(), base.casefold()):
            if h and any(p in h for p in pats):
                return True
    return False


class AutoPauseWatcher(QObject):
    """Polls the process list and pauses/resumes the wallpapers accordingly.

    Owns nothing but a timer and references to the config (read live, so a
    settings change takes effect on the next tick) and the controller it drives.
    """

    def __init__(self, cfg, controller, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.controller = controller
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)
        # GPU-load state machine.
        self._gpu_wants = False        # latched decision of the GPU trigger
        self._cond_since: float | None = None  # when the pending flip condition began

    def start(self) -> None:
        self._timer.start()
        self._tick()  # react immediately instead of waiting a full interval

    def _tick(self) -> None:
        want = self._should_pause()
        if want and not self.controller.is_paused():
            self.controller.pause()
        elif not want and self.controller.is_paused():
            self.controller.resume()

    def _should_pause(self) -> bool:
        # The app list takes precedence: while a listed app runs, stay paused
        # and keep the GPU trigger disarmed.
        if any_running(self.cfg.pause_apps):
            self._gpu_wants = False
            self._cond_since = None
            return True
        return self._gpu_should_pause()

    def _gpu_should_pause(self) -> bool:
        if not self.cfg.gpu_pause_enabled:
            self._gpu_wants = False
            self._cond_since = None
            return False
        busy = gpu_busy_percent()
        if busy is None:
            return self._gpu_wants  # no sensor → hold whatever we decided
        high = self.cfg.gpu_pause_threshold
        low = max(0, high - _GPU_HYSTERESIS)
        # We're chasing the *opposite* of the latched state: while not paused we
        # wait for a sustained high load; while paused, a sustained low one.
        want_flip = busy >= high if not self._gpu_wants else busy < low
        if want_flip:
            now = time.monotonic()
            if self._cond_since is None:
                self._cond_since = now
            needed = _GPU_HIGH_SECONDS if not self._gpu_wants else _GPU_LOW_SECONDS
            if now - self._cond_since >= needed:
                self._gpu_wants = not self._gpu_wants
                self._cond_since = None
        else:
            self._cond_since = None
        return self._gpu_wants
