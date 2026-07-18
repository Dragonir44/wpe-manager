"""Scanning the Steam workshop folder into a list of Wallpaper entries."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Preview files are usually declared in project.json, but fall back to these.
_PREVIEW_GLOBS = ("preview.*", "*.preview.*")

# Local content ratings (project.json) mapped to Wallpaper Engine's age buckets.
AGE_LABELS = {
    "everyone": "Pour tout public",
    "questionable": "13 ans minimum",
    "mature": "Adulte",
}


@dataclass(frozen=True)
class Wallpaper:
    id: str            # workshop id (folder name)
    title: str
    type: str          # "scene", "video", "web", ...
    folder: Path       # full path to the wallpaper folder
    preview: Path | None
    tags: tuple[str, ...] = ()     # genre tags from project.json
    size_bytes: int = 0            # total size on disk
    age: str = ""                  # "everyone" | "questionable" | "mature" | ""

    @property
    def has_preview(self) -> bool:
        return self.preview is not None and self.preview.is_file()


def _find_preview(folder: Path, declared: str | None) -> Path | None:
    if declared:
        p = folder / declared
        if p.is_file():
            return p
    for pattern in _PREVIEW_GLOBS:
        for match in sorted(folder.glob(pattern)):
            if match.is_file():
                return match
    return None


def _folder_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _load_one(folder: Path) -> Wallpaper | None:
    if not folder.is_dir():
        return None
    project = folder / "project.json"
    title = folder.name
    wtype = "unknown"
    preview_name: str | None = None
    tags: tuple[str, ...] = ()
    age = ""
    if project.is_file():
        try:
            data = json.loads(project.read_text(encoding="utf-8", errors="ignore"))
            title = str(data.get("title") or folder.name).strip() or folder.name
            wtype = str(data.get("type") or "unknown").lower()
            preview_name = data.get("preview")
            raw_tags = data.get("tags")
            if isinstance(raw_tags, list):
                tags = tuple(str(t) for t in raw_tags if t)
            age = str(data.get("contentrating") or "").lower()
        except (OSError, json.JSONDecodeError):
            pass
    return Wallpaper(
        id=folder.name,
        title=title,
        type=wtype,
        folder=folder,
        preview=_find_preview(folder, preview_name),
        tags=tags,
        size_bytes=_folder_size(folder),
        age=age,
    )


# --------------------------------------------------------------------------- #
# Per-wallpaper customisable properties (project.json -> general.properties)
# --------------------------------------------------------------------------- #
_EDITABLE_TYPES = ("color", "bool", "slider", "combo", "textinput")


@dataclass(frozen=True)
class Property:
    key: str
    type: str                 # color | bool | slider | combo | textinput
    label: str
    default: object
    order: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[tuple[str, object], ...] = ()   # (label, value) for combo


def _pretty_label(text: str | None, key: str) -> str:
    """Human label: use the property's text unless it's a ui_ translation key or
    HTML, in which case prettify the key (or the stripped key)."""
    t = (text or "").strip()
    if t and not t.startswith("ui_") and "<" not in t:
        return t
    base = t[3:] if t.startswith("ui_") else key
    base = base.replace("_", " ").strip()
    return (base[:1].upper() + base[1:]) if base else key


def read_properties(folder: Path) -> list[Property]:
    """Editable properties of a wallpaper, sorted by their declared order."""
    project = folder / "project.json"
    if not project.is_file():
        return []
    try:
        data = json.loads(project.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = (data.get("general") or {}).get("properties") or {}
    props: list[Property] = []
    for key, v in raw.items():
        if not isinstance(v, dict) or v.get("type") not in _EDITABLE_TYPES:
            continue
        options: tuple[tuple[str, object], ...] = ()
        if v.get("type") == "combo":
            options = tuple(
                (_pretty_label(o.get("label"), str(o.get("value"))), o.get("value"))
                for o in v.get("options", []) if isinstance(o, dict)
            )
        props.append(Property(
            key=key,
            type=v["type"],
            label=_pretty_label(v.get("text"), key),
            default=v.get("value"),
            order=float(v.get("order") or 0),
            minimum=v.get("min"),
            maximum=v.get("max"),
            options=options,
        ))
    props.sort(key=lambda p: (p.order, p.label.casefold()))
    return props


def format_property(value) -> str:
    """Render a property value for the backend's `--set-property key=value`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def scan(library_dir: Path) -> list[Wallpaper]:
    """Return every wallpaper in the library, sorted by title (case-insensitive)."""
    if not library_dir.is_dir():
        return []
    wallpapers: list[Wallpaper] = []
    for entry in library_dir.iterdir():
        wp = _load_one(entry)
        if wp is not None:
            wallpapers.append(wp)
    wallpapers.sort(key=lambda w: w.title.casefold())
    return wallpapers
