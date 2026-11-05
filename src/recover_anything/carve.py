"""Signature carving: find files by their first bytes, then measure exact length.

On FAT/exFAT every file starts at a cluster boundary, so we only look there.
That alone removes almost every false positive (thumbnails inside photos,
headers inside archives) and makes scanning fast.
"""
from __future__ import annotations

import re
from bisect import bisect_right

from .formats import jpeg, others
from .records import Found

# kind, probe, max size, media group
FORMATS = [
    ("jpeg", jpeg.probe, 200 << 20, "photo"),
    ("isobmff", others.probe_isobmff, 1 << 40, "video"),
    ("png", others.probe_png, 256 << 20, "photo"),
    ("gif", others.probe_gif, 64 << 20, "photo"),
    ("tiff", others.probe_tiff, 300 << 20, "photo"),
    ("riff", others.probe_riff, 1 << 32, "video"),
    ("pdf", others.probe_pdf, 512 << 20, "document"),
    ("zip", others.probe_zip, 2 << 30, "document"),
]
GROUP = {k: g for k, _, _, g in FORMATS}
_SIG = re.compile(rb"\xff\xd8\xff|ftyp|\x89PNG|GIF8[79]a|II\*\x00|MM\x00\*|IIR[OS]|IIU\x00|RIFF|%PDF-|PK\x03\x04",
                  re.DOTALL)


def identify(head: bytes):
    for kind, probe, mx, group in FORMATS:
        if probe(head):
            return kind
    return None


def measure(src, kind: str, start: int, limit: int):
    """Return (length, complete, ext, reason, break_pos) for a file at `start`."""
    read = src.read
    mx = dict((k, m) for k, _, m, _ in FORMATS)[kind]
    limit = min(limit, mx)
    if kind == "jpeg":
        m = jpeg.measure(read, start, limit)
        ext = ".jpg"
        return m.end, m.complete, ext, m.reason, m.break_pos, m.header
    fn = {"isobmff": others.measure_isobmff, "png": others.measure_png, "gif": others.measure_gif,
          "tiff": others.measure_tiff, "riff": others.measure_riff, "pdf": others.measure_pdf,
          "zip": others.measure_zip}[kind]
    r = fn(read, start, limit)
    return r.end, r.complete, r.ext, r.reason, r.break_pos, None


def find_headers(src, start: int, end: int, align: int, base: int, chunk: int = 16 << 20, progress=None):
    """Yield (offset, kind) for every recognized header at an aligned offset.
    `base` is the offset alignment is measured from (the cluster heap start)."""
    pos = start
    while pos < end:
        n = min(chunk, end - pos)
        blob = src.read_uncached(pos, n + 16)
        for mo in _SIG.finditer(blob, 0, n + 8):
            j = mo.start()
            if mo.group() == b"ftyp":
                j -= 4
            if j < 0 or j >= n:
                continue
            off = pos + j
            if (off - base) % align:
                continue
            kind = identify(blob[j:j + 16])
            if kind:
                yield off, kind
        pos += n
        if progress:
            progress(pos - start, end - start)


class Claimed:
    """Sorted, non-overlapping byte ranges that already belong to found files."""

    def __init__(self):
        self.starts: list[int] = []
        self.ends: list[int] = []

    def add(self, s: int, e: int):
        i = bisect_right(self.starts, s)
        self.starts.insert(i, s)
        self.ends.insert(i, e)

    def covering(self, off: int):
        i = bisect_right(self.starts, off) - 1
        if i >= 0 and self.starts[i] <= off < self.ends[i]:
            return self.ends[i]
        return None


def carve(src, region_start: int, region_end: int, align: int, base: int, known_starts: set,
          claimed: Claimed, progress=None, kinds=None):
    """Carve every recognizable file in a region. Returns Found records."""
    out = []
    region_end = min(region_end, getattr(src, "size", region_end) or region_end)
    headers = list(find_headers(src, region_start, region_end, align, base, progress=progress))
    starts = [h[0] for h in headers]
    for idx, (off, kind) in enumerate(headers):
        if kinds and GROUP[kind] not in kinds:
            continue
        if off in known_starts or claimed.covering(off) is not None:
            continue
        nxt = starts[idx + 1] if idx + 1 < len(starts) else region_end
        # A PDF's end is found by searching for its last end marker, so don't let
        # the search run into the next file on the card.
        limit = (nxt - off) if kind == "pdf" else region_end - off
        try:
            length, complete, ext, reason, brk, header = measure(src, kind, off, limit)
        except Exception:
            continue  # a header we can't make sense of: skip it, keep going
        if length <= 0:
            continue
        if kind != "jpeg" and not complete and length < 4096:
            continue
        rec = Found(kind=kind, ext=ext, extents=[(off, length)], origin="carved", deleted=True)
        rec.status = "unverified" if complete else "partial"
        rec.note = "" if complete else reason
        rec.meta["next_header"] = nxt
        if brk is not None:
            rec.meta["break_pos"] = off + brk
        if header is not None:
            rec.meta["jpeg_header"] = header
        out.append(rec)
        # Not claimed yet: a file that merely *looks* complete may be
        # fragmented, and the headers inside its span may be real files.
        # Embedded copies are dropped after validation (pipeline._drop_embedded).
    return out, headers
