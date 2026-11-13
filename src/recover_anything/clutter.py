"""Files an operating system leaves on a card for itself, never the owner's:
a Mac's hidden ._ companions (one per file it copied onto FAT or exFAT, 4 KB
of Finder metadata whose name ends like the photo's), .DS_Store, Spotlight and
file-event folders, and Windows' System Volume Information. They are never
saved, never reported as lost and never counted as the owner's files.
(Thumbs.db is not on this list: it holds previews of deleted photos.)"""
from __future__ import annotations

import re
from typing import Optional

_NAMES = {".ds_store", ".localized", ".apdisk", ".volumeicon.icns", "indexervolumeguid", "wpsettings.dat"}
_FOLDERS = re.compile(r"^(\.spotlight-v100|\.fseventsd|\.temporaryitems|\.documentrevisions-v100|"
                      r"system volume information)(/|$)", re.I)


def is_clutter(path: Optional[str], name: Optional[str]) -> bool:
    n = name or ""
    if n.startswith("._") or n.lower() in _NAMES:
        return True
    return bool(_FOLDERS.match((path or "").strip("/")))
