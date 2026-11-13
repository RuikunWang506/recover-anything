"""Files a computer moved to the trash folder on the card itself.

Deleting from a card or USB stick in Finder, a Linux file manager or (for USB
drives with a Recycle Bin) Windows does not erase anything: the file is moved
into a hidden folder on the same card, still whole and still allocated:

  macOS     /.Trashes/<uid>/NAME
  Linux     /.Trash-<uid>/files/NAME, with /.Trash-<uid>/info/NAME.trashinfo
            (also /.Trash/<uid>/files/NAME)
  Windows   /$RECYCLE.BIN/<SID>/$R<id>.ext, with $I<id>.ext holding the
            original path; older Windows: /RECYCLED/ or /RECYCLER/<SID>/
  Android   (11 and later, Gallery and Google Photos) renamed in place to
            .trashed-<expiry>-NAME for 30 days

To the owner these files are deleted, so recovery hands them back by default.
Their original names come back from the Linux and Windows records.
"""
from __future__ import annotations

import re
import struct
from typing import Optional
from urllib.parse import unquote

_MAC = re.compile(r"^\.trashes(/|$)", re.I)
_LINUX = re.compile(r"^(\.trash-[^/]+|\.trash/[^/]+)/files(/|$)", re.I)
_LINUX_TOP = re.compile(r"^(\.trash-[^/]+|\.trash/[^/]+)/files$", re.I)
_WIN = re.compile(r"^\$recycle\.bin(/|$)", re.I)
_WIN_OLD = re.compile(r"^recycle[dr](/|$)", re.I)
# A deleted FAT short name loses its first character; readers show "_".
_WIN_DATA = re.compile(r"^[$_]R([A-Z0-9]{6})(\..*)?$", re.I)
# Android's trash: the name only. A deleted FAT short name loses its first
# character, but these names are long names, which keep it.
_ANDROID = re.compile(r"^\.trashed-(\d{9,11})-(.+)$")


def kind(path: Optional[str], name: Optional[str]) -> Optional[str]:
    """'mac', 'linux' or 'windows' when the file NAME in folder PATH is one
    the user deleted into a trash folder on the card; None otherwise (the
    trash's own bookkeeping files are not the user's)."""
    p = (path or "").strip("/")
    n = name or ""
    if _ANDROID.match(n):
        return "android"
    if _MAC.match(p):
        return None if n in (".DS_Store",) or n.startswith("._") else "mac"
    if _LINUX.match(p):
        return "linux"
    if _WIN.match(p):
        if p.count("/") == 1 and _WIN_DATA.match(n):
            return "windows"
        return "windows" if p.count("/") >= 2 else None   # inside a deleted folder
    if _WIN_OLD.match(p):
        return None if n.upper() in ("INFO", "INFO2", "DESKTOP.INI") else "windows"
    return None


def bookkeeping(path: Optional[str], name: Optional[str]) -> bool:
    """True for the trash folder's own records (original paths, folder
    settings), which are not files the user lost."""
    p = (path or "").strip("/")
    n = name or ""
    if _MAC.match(p):
        return n == ".DS_Store" or n.startswith("._")
    if re.match(r"^(\.trash-[^/]+|\.trash/[^/]+)/(info|directorysizes)(/|$)", p, re.I) or \
            re.match(r"^(\.trash-[^/]+|\.trash/[^/]+)$", p, re.I) and n.lower() == "directorysizes":
        return True
    if _WIN.match(p):
        return n.lower() == "desktop.ini" or (p.count("/") == 1 and n[:2].upper() in ("$I", "_I"))
    if _WIN_OLD.match(p):
        return n.upper() in ("INFO", "INFO2", "DESKTOP.INI")
    return False


def companion(path: Optional[str], name: Optional[str]) -> Optional[tuple]:
    """(folder, name) of the record that holds this trashed file's original
    path, or None."""
    p = (path or "").strip("/")
    n = name or ""
    if _LINUX_TOP.match(p):
        return p[:-len("files")] + "info", n + ".trashinfo"
    if _WIN.match(p) and p.count("/") == 1:
        m = _WIN_DATA.match(n)
        if m:
            return p, "$I" + m.group(1) + (m.group(2) or "")
    return None


def android_name(name: Optional[str]) -> Optional[str]:
    """The name a photo had before Android moved it to the trash."""
    m = _ANDROID.match(name or "")
    return m.group(2) if m else None


def original_path(data: bytes) -> Optional[str]:
    """The original path from a Linux .trashinfo or a Windows $I record, or
    None when the bytes are neither (a deleted record may be overwritten)."""
    if data[:12] == b"[Trash Info]":
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("Path="):
                return unquote(line[5:].strip()) or None
        return None
    if len(data) >= 28:
        version = struct.unpack_from("<Q", data, 0)[0]
        if version == 1 and len(data) >= 24 + 6:
            raw = data[24:24 + 520]
        elif version == 2:
            n = struct.unpack_from("<I", data, 24)[0]
            if not 3 <= n <= 32767:
                return None
            raw = data[28:28 + 2 * n]
        else:
            return None
        text = raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
        if re.match(r"^[A-Za-z]:\\.", text) or text.startswith("\\\\"):
            return text
    return None


def split(original: str) -> tuple:
    """(folder, name) of an original path, Windows or POSIX."""
    norm = original.replace("\\", "/")
    folder, _, name = norm.rstrip("/").rpartition("/")
    if re.match(r"^[A-Za-z]:$", folder):
        folder = ""
    elif re.match(r"^[A-Za-z]:/", folder):
        folder = folder[3:]
    return folder.strip("/"), name
