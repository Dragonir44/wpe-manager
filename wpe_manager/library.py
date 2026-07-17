"""Scanning the Steam workshop folder into a list of Wallpaper entries."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Preview files are usually declared in project.json, but fall back to these.
_PREVIEW_GLOBS = ("preview.*", "*.preview.*")


@dataclass(frozen=True)
class Wallpaper:
    id: str            # workshop id (folder name)
    title: str
    type: str          # "scene", "video", "web", ...
    folder: Path       # full path to the wallpaper folder
    preview: Path | None

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


def _load_one(folder: Path) -> Wallpaper | None:
    if not folder.is_dir():
        return None
    project = folder / "project.json"
    title = folder.name
    wtype = "unknown"
    preview_name: str | None = None
    if project.is_file():
        try:
            data = json.loads(project.read_text(encoding="utf-8", errors="ignore"))
            title = str(data.get("title") or folder.name).strip() or folder.name
            wtype = str(data.get("type") or "unknown").lower()
            preview_name = data.get("preview")
        except (OSError, json.JSONDecodeError):
            pass
    return Wallpaper(
        id=folder.name,
        title=title,
        type=wtype,
        folder=folder,
        preview=_find_preview(folder, preview_name),
    )


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
