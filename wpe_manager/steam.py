"""Enriching wallpapers with Steam Workshop metadata (resolution).

The one thing the local files don't carry is the wallpaper's native
resolution — it's a Workshop tag the author sets at upload, packed away from
project.json. Steam's *public* Web API returns it (no key, no login), so we
fetch it once, cache it to disk, and from then on filtering by resolution is
instant and offline.

Only the resolution (+ file size) is taken here; genre/type/age all live in the
local project.json already (see library.py).
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Callable

_ENDPOINT = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_BATCH = 100
# Resolution tags come in many forms: "1920 x 1080", "Ultrawide 2560 x 1080",
# "Portrait 1080 x 1920", "Dual 3840 x 1080", "1920 x 1080 - Full HD". So we
# search for the "W x H" *anywhere* in the tag rather than anchoring the string.
_RES_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)")


# --------------------------------------------------------------------------- #
# Resolution → aspect-ratio family (used by the GUI filters)
# --------------------------------------------------------------------------- #
FAMILY_LABELS = {
    "standard": "Standard 4:3",
    "16:9": "Écran large 16:9",
    "21:9": "Ultrawide 21:9",
    "multi": "Multi-écran",
    "portrait": "Portrait",
}
# Order shown in the filter menu.
FAMILY_ORDER = ["standard", "16:9", "21:9", "multi", "portrait"]


def aspect_family(w: int, h: int) -> str | None:
    """Bucket a width/height into the same families Wallpaper Engine uses."""
    if not w or not h:
        return None
    r = w / h
    if r < 0.9:
        return "portrait"
    if r < 1.6:
        return "standard"
    if r < 1.9:
        return "16:9"
    if r < 2.6:
        return "21:9"
    return "multi"


def parse_wh(res: str) -> tuple[int, int]:
    """'2560 x 1080' (or 'Ultrawide 2560 x 1080') -> (2560, 1080); (0, 0) if none."""
    m = _RES_RE.search(res or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def families_of(resolutions: list[str]) -> set[str]:
    fams = set()
    for r in resolutions:
        f = aspect_family(*parse_wh(r))
        if f:
            fams.add(f)
    return fams


def _resolutions_from_tags(tags: list) -> list[str]:
    """All distinct 'W x H' resolutions a wallpaper is tagged with.

    A wallpaper can carry several (e.g. a 16:9 and an ultrawide variant); we keep
    them all so it shows up under every screen it actually fits."""
    out: list[str] = []
    for t in tags:
        tag = t.get("tag") if isinstance(t, dict) else str(t)
        m = _RES_RE.search(str(tag or ""))
        if m:
            s = f"{int(m.group(1))} x {int(m.group(2))}"
            if s not in out:
                out.append(s)
    return out


def _chunks(seq: list[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _post(ids: list[str]) -> list[dict]:
    form = {"itemcount": len(ids)}
    for i, wid in enumerate(ids):
        form[f"publishedfileids[{i}]"] = wid
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(_ENDPOINT, data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    return payload.get("response", {}).get("publishedfiledetails", []) or []


def fetch_metadata(
    ids: list[str],
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, dict]:
    """Fetch resolution (+ file size) for `ids`, in batches.

    Returns {id: {"resolution", "w", "h", "file_size"}}. Batches that fail
    (network hiccup, rate limit) are skipped so a partial result is still
    usable; the caller can re-sync later to fill the gaps.
    """
    out: dict[str, dict] = {}
    total = len(ids)
    done = 0
    for batch in _chunks(ids, _BATCH):
        try:
            for d in _post(batch):
                wid = str(d.get("publishedfileid"))
                out[wid] = {
                    "resolutions": _resolutions_from_tags(d.get("tags", []) or []),
                    "file_size": int(d.get("file_size") or 0),
                }
        except Exception:
            pass  # skip this batch; partial result is fine
        done += len(batch)
        if progress:
            progress(min(done, total), total)
    return out
