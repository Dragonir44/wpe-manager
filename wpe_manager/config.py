"""Configuration, path auto-detection and persisted state.

Everything the app needs to know about *where* things live (the Steam
workshop library for Wallpaper Engine, and the engine assets folder) plus
the persisted per-screen assignments lives here. Paths are auto-detected on
first run but always overridable from the GUI settings dialog.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Steam app id for Wallpaper Engine. Workshop content lives under
# <library>/steamapps/workshop/content/<APP_ID>/<wallpaper_id>/
APP_ID = "431960"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wpe-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
# Tracks the running backend process of each screen: {screen: {"pid", "id"}}.
ENGINE_FILE = CONFIG_DIR / "engine.json"
# Cached Steam Workshop metadata (resolution) keyed by wallpaper id.
METADATA_FILE = CONFIG_DIR / "metadata.json"
# Per-wallpaper property overrides: {wallpaper_id: {property_key: value}}.
PROPERTIES_FILE = CONFIG_DIR / "properties.json"

# Steam roots we know how to look inside, in rough order of likelihood.
_STEAM_ROOTS = [
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".steam/root",
    Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
]


def _library_paths_from_vdf(steam_root: Path) -> list[Path]:
    """Read steamapps/libraryfolders.vdf to discover *all* Steam libraries.

    This is what lets us find a library sitting on another drive (e.g. an
    NTFS game mount) instead of only the default one under $HOME.
    """
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return []
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    # Entries look like:   "path"   "/mnt/JeuxVite/SteamLibrary"
    return [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', text)]


def detect_library_dir() -> Path | None:
    """Return the workshop/content/<APP_ID> folder that actually holds items."""
    candidates: list[Path] = []
    for root in _STEAM_ROOTS:
        candidates.append(root)                      # the root is itself a library
        candidates.extend(_library_paths_from_vdf(root))
    seen: set[Path] = set()
    for lib in candidates:
        content = lib / "steamapps" / "workshop" / "content" / APP_ID
        if content in seen:
            continue
        seen.add(content)
        if content.is_dir() and any(content.iterdir()):
            return content
    return None


def _assets_from_library(library_dir: Path) -> Path | None:
    """assets sit at <steamapps>/common/wallpaper_engine/assets of the same lib.

    We resolve() first so that a library_dir which is actually a symlink into
    another drive (e.g. the default path pointing at an NTFS mount) derives its
    assets from the *real* location, not the empty default one.
    """
    real = library_dir.resolve()
    try:
        steamapps = real.parents[2]  # .../content/APP_ID -> .../steamapps
    except IndexError:
        return None
    assets = steamapps / "common" / "wallpaper_engine" / "assets"
    return assets if assets.is_dir() else None


def detect_assets_dir(library_dir: Path | None) -> Path | None:
    """The engine's shared assets live next to the app install of the same lib."""
    if library_dir is not None:
        found = _assets_from_library(library_dir)
        if found is not None:
            return found
    # Fallback: scan every library Steam knows about for the assets folder.
    for root in _STEAM_ROOTS:
        for lib in [root, *_library_paths_from_vdf(root)]:
            assets = lib / "steamapps" / "common" / "wallpaper_engine" / "assets"
            if assets.is_dir():
                return assets
    return None


@dataclass
class Config:
    library_dir: str = ""
    assets_dir: str = ""
    silent: bool = False
    fps: int = 30
    # Overlap window (ms) when swapping a screen's wallpaper: the new one is
    # started and left to render for this long before the old is killed, so
    # there's no black gap. 0 disables the overlap.
    overlap_ms: int = 1200

    @property
    def library_path(self) -> Path | None:
        return Path(self.library_dir) if self.library_dir else None

    @property
    def assets_path(self) -> Path | None:
        return Path(self.assets_dir) if self.assets_dir else None

    def is_usable(self) -> bool:
        return bool(self.library_dir) and Path(self.library_dir).is_dir()


def load_config() -> Config:
    """Load config from disk, filling in any missing paths by auto-detection."""
    data: dict = {}
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})

    if not cfg.library_dir:
        detected = detect_library_dir()
        if detected:
            cfg.library_dir = str(detected)
    if not cfg.assets_dir:
        detected = detect_assets_dir(cfg.library_path)
        if detected:
            cfg.assets_dir = str(detected)
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2))


def load_assignments() -> dict[str, dict]:
    """Persisted per-screen assignment.

    Each screen maps to a small dict describing what it should show:
        {"mode": "single",   "id": "<wallpaper id>"}
        {"mode": "playlist", "playlist": "<playlist name>"}

    Older files stored a plain {screen: "<id>"} mapping; those are migrated to
    the "single" form transparently.
    """
    if STATE_FILE.is_file():
        try:
            data = json.loads(STATE_FILE.read_text())
            raw = data.get("assignments", {})
            if isinstance(raw, dict):
                out: dict[str, dict] = {}
                for screen, val in raw.items():
                    if isinstance(val, str):  # legacy format
                        out[str(screen)] = {"mode": "single", "id": val}
                    elif isinstance(val, dict) and val.get("mode"):
                        out[str(screen)] = val
                return out
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_assignments(assignments: dict[str, dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"assignments": assignments}, indent=2))


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
# A playlist is a plain dict: {"name", "ids": [...], "interval_min": int,
# "order": "sequential"|"random"}.
def load_playlists() -> list[dict]:
    if PLAYLISTS_FILE.is_file():
        try:
            data = json.loads(PLAYLISTS_FILE.read_text())
            pls = data.get("playlists", [])
            if isinstance(pls, list):
                return [p for p in pls if isinstance(p, dict) and p.get("name")]
        except (OSError, json.JSONDecodeError):
            pass
    return []


def save_playlists(playlists: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLISTS_FILE.write_text(json.dumps({"playlists": playlists}, indent=2))


# --------------------------------------------------------------------------- #
# Steam metadata cache (resolution)
# --------------------------------------------------------------------------- #
def load_metadata() -> dict[str, dict]:
    if METADATA_FILE.is_file():
        try:
            data = json.loads(METADATA_FILE.read_text())
            if isinstance(data, dict):
                # Ignore a pre-"resolutions" cache so the app re-syncs cleanly.
                entries = [v for v in data.values() if isinstance(v, dict)]
                if entries and not any("resolutions" in v for v in entries):
                    return {}
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_metadata(meta: dict[str, dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_FILE.write_text(json.dumps(meta, indent=2))


# --------------------------------------------------------------------------- #
# Per-wallpaper property overrides
# --------------------------------------------------------------------------- #
def load_properties() -> dict[str, dict]:
    if PROPERTIES_FILE.is_file():
        try:
            data = json.loads(PROPERTIES_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_properties(data: dict[str, dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROPERTIES_FILE.write_text(json.dumps(data, indent=2))


_WPE_ID_RE = re.compile(r"/431960/(\d+)/")


def import_wpe_playlists(config_json: Path) -> list[dict]:
    """Extract playlists from a Wallpaper Engine config.json.

    Only the portable part is taken: the playlist name, its wallpaper ids
    (parsed out of the S:/.../431960/<id>/... item paths) and the timer
    settings. Monitor assignments are intentionally ignored — WPE's internal
    monitor names don't map onto Linux connector names.
    """
    try:
        data = json.loads(config_json.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("steamuser", {}).get("general", {}).get("playlists", [])
    result: list[dict] = []
    for pl in raw if isinstance(raw, list) else []:
        if not isinstance(pl, dict):
            continue
        ids: list[str] = []
        for item in pl.get("items", []):
            m = _WPE_ID_RE.search(str(item))
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        if not ids:
            continue
        settings = pl.get("settings", {}) if isinstance(pl.get("settings"), dict) else {}
        order = "random" if str(settings.get("order", "")).lower() == "random" else "sequential"
        try:
            interval = max(1, int(settings.get("delay", 30)))
        except (TypeError, ValueError):
            interval = 30
        result.append({
            "name": str(pl.get("name") or "sans-nom"),
            "ids": ids,
            "interval_min": interval,
            "order": order,
        })
    return result


def wpe_config_path(cfg: "Config") -> Path | None:
    """Best guess for the WPE config.json (holds the user's playlists)."""
    assets = cfg.assets_path
    if assets is None:
        return None
    candidate = assets.parent / "config.json"  # .../wallpaper_engine/config.json
    return candidate if candidate.is_file() else None


# --------------------------------------------------------------------------- #
# Desktop integration (autostart + application-menu launcher)
# --------------------------------------------------------------------------- #
_AUTOSTART_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "autostart"
AUTOSTART_FILE = _AUTOSTART_DIR / "wpe-manager.desktop"

_APPLICATIONS_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
) / "applications"
LAUNCHER_FILE = _APPLICATIONS_DIR / "wpe-manager.desktop"


def _module_exec(args: str = "") -> str:
    """Command line to (re)launch the app via the current interpreter.

    `<python> -m wpe_manager [args]` works both for a pipx/pip install (its venv
    python) and a source checkout.
    """
    base = f"{shlex.quote(sys.executable)} -m wpe_manager"
    return f"{base} {args}".strip()


def _workdir() -> Path:
    """Directory containing the wpe_manager package, used as the entry's Path so
    `-m wpe_manager` resolves even from a source checkout (cwd would be $HOME)."""
    return Path(__file__).resolve().parent.parent


def _write_desktop_entry(path: Path, exec_line: str, comment: str,
                         extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Wallpaper Engine Manager\n"
        f"Exec={exec_line}\n"
        f"Path={_workdir()}\n"
        "Icon=preferences-desktop-wallpaper\n"
        f"Comment={comment}\n"
        + extra
    )


# -- autostart --------------------------------------------------------------- #
def is_autostart_enabled() -> bool:
    return AUTOSTART_FILE.is_file()


def install_autostart() -> None:
    """Write a freedesktop autostart entry that launches the tray daemon."""
    _write_desktop_entry(
        AUTOSTART_FILE,
        _module_exec("--daemon"),
        "Restaure et fait tourner les fonds d'écran au démarrage",
        extra="X-GNOME-Autostart-enabled=true\n",
    )


def remove_autostart() -> None:
    AUTOSTART_FILE.unlink(missing_ok=True)


def set_autostart(enabled: bool) -> None:
    install_autostart() if enabled else remove_autostart()


# -- application-menu launcher ---------------------------------------------- #
def is_launcher_installed() -> bool:
    return LAUNCHER_FILE.is_file()


def install_launcher() -> None:
    """Write a launcher into the application menu (KDE/GNOME/etc.)."""
    _write_desktop_entry(
        LAUNCHER_FILE,
        _module_exec(),
        "Gérer les fonds d'écran animés (Wallpaper Engine)",
        extra="Terminal=false\nCategories=Utility;Settings;\nStartupNotify=true\n",
    )


def remove_launcher() -> None:
    LAUNCHER_FILE.unlink(missing_ok=True)


def set_launcher(enabled: bool) -> None:
    install_launcher() if enabled else remove_launcher()
