"""Signature carving: find files by their first bytes, then measure exact length.

On FAT/exFAT every file starts at a cluster boundary, so we only look there.
That alone removes almost every false positive (thumbnails inside photos,
headers inside archives) and makes scanning fast.
"""
from __future__ import annotations

import re
from bisect import bisect_right

from .formats import jpeg, more, others
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
] + [(k, v[0], v[3], v[4]) for k, v in more.KINDS.items()]
GROUP = {k: g for k, _, _, g in FORMATS}
# One container holds several kinds of file: an iPhone HEIC photo and a Canon
# CR3 RAW are ISO BMFF like an MP4, a WAV recording is RIFF like an AVI.
EXT_GROUP = {".heic": "photo", ".heif": "photo", ".avif": "photo", ".cr3": "photo", ".webp": "photo",
             ".wav": "audio", ".m4a": "audio", ".ogv": "video"}


def group_of(kind: str, ext: str | None = None) -> str:
    """'photo', 'video', 'audio', 'document' or 'other', from the file's own type."""
    return EXT_GROUP.get((ext or "").lower()) or GROUP.get(kind, "other")


_SIG = re.compile(rb"\xff\xd8\xff|ftyp|\x89PNG|GIF8[79]a|II\*\x00|MM\x00\*|IIR[OS]|IIU\x00|RIFF|%PDF-|PK\x03\x04"
                  rb"|fLaC|OggS\x00|\x1a\x45\xdf\xa3|SQLite format 3\x00|\x1f\x8b\x08|BM.{4}\x00\x00\x00\x00"
                  rb"|ID3[\x02-\x04]\x00|\xff[\xe2-\xe5\xf2-\xf5\xfa-\xfd]"
                  rb"|\x47(?=.{187}\x47.{187}\x47|.{191}\x47.{191}\x47)"
                  + more._extra.SIGNATURES,
                  re.DOTALL)
HEAD = 1024  # bytes a probe may look at (a transport stream needs three packets)
# Free space reads back as all zeros or all 0xFF, and no header can start in a
# stretch of one repeated byte, so the scan skips such blocks.
_BLANK = 64 << 10
_BLANKS = (bytes(_BLANK), b"\xff" * _BLANK)


def _busy(blob: bytes, n: int) -> list:
    """The stretches of blob[:n] that aren't blank, as [start, end) pairs."""
    spans: list = []
    for s in range(0, n, _BLANK):
        e = min(n, s + _BLANK)
        if e - s == _BLANK and (blob.startswith(_BLANKS[0], s) or blob.startswith(_BLANKS[1], s)):
            continue
        if spans and spans[-1][1] == s:
            spans[-1][1] = e
        else:
            spans.append([s, e])
    return spans


def _at_sectors(blob: bytes, n: int, first: int, align: int) -> list:
    """Offsets first, first + align, ... below n where a header that begins with
    zero bytes starts. One byte per sector is searched, so zeros cost nothing."""
    out = []
    for sig in more._extra.AT_SECTOR:
        k = len(sig) - 1
        col = blob[first + k:n + k:align]  # byte k of every sector
        i = col.find(sig[k])
        while i >= 0:
            c = first + i * align
            if blob.startswith(sig, c):
                out.append(c)
            i = col.find(sig[k], i + 1)
    return out


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
          "zip": others.measure_zip}.get(kind) or more.KINDS[kind][1]
    r = fn(read, start, limit)
    return r.end, r.complete, r.ext, r.reason, r.break_pos, None


def _weak(kind: str, head: bytes) -> bool:
    """Headers that also turn up inside other files: a bare MP3 frame header
    (4 bytes), a transport stream's tables, repeated every fraction of a
    second through a recording (only a real start has its counter at 0), or a
    program stream's packs."""
    if kind == "mp3":
        return head[:3] != b"ID3"
    if kind == "ts":
        lay = more._ts_layout(head)
        return not lay or head[lay[1] + 3] & 15 != 0
    if kind == "ps":
        return True  # a program stream has a pack every 2 KB: one at a cluster start is usually mid-recording
    return False


def find_headers(src, start: int, end: int, align: int, base: int, chunk: int = 16 << 20, progress=None):
    """Yield (offset, kind) for every recognized header at an aligned offset.
    `base` is the offset alignment is measured from (the cluster heap start)."""
    pos = start
    while pos < end:
        n = min(chunk, end - pos)
        blob = src.read_uncached(pos, n + HEAD)
        hits: dict = {}
        for s, e in _busy(blob, n):
            # (a header may begin in the blank bytes just before a busy stretch)
            for mo in _SIG.finditer(blob, max(0, s - 16), min(len(blob), e + HEAD)):
                j = mo.start()
                if j >= e:
                    break
                g = mo.group()
                # where the file would start: 'ftyp' sits 4 bytes in; an AVCHD
                # transport stream has a 4-byte timestamp before each packet
                cands = (j - 4,) if g == b"ftyp" else (j, j - 4) if g == b"\x47" else (j,)
                for c in cands:
                    if c < 0 or c in hits or (pos + c - base) % align:
                        continue
                    kind = identify(blob[c:c + HEAD])
                    if kind:
                        hits[c] = kind
                        break
        for c in _at_sectors(blob, n, (base - pos) % align, align):
            if c not in hits:
                kind = identify(blob[c:c + HEAD])
                if kind:
                    hits[c] = kind
        for c in sorted(hits):
            yield pos + c, hits[c], _weak(hits[c], blob[c:c + HEAD])
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


def _measure_pdf(src, off: int, sure: list, region_end: int):
    """A PDF's end is found by searching for its end marker, so the search
    must not run into the next file on the card. Its compressed streams can
    look like another file's header by chance, and it may hold a picture that
    starts a sector: with no end marker before the next header, look on past
    it, but never past the next PDF."""
    i = bisect_right(sure, (off, "~"))
    first = None
    for k in range(i, min(i + 8, len(sure)) + 1):
        stop = sure[k][0] if k < len(sure) else region_end
        got = measure(src, "pdf", off, stop - off)
        if got[1]:
            return got
        first = first or got  # no end marker in reach: its first piece, as far as the next header
        if k == len(sure) or sure[k][1] == "pdf":
            break
    return first


def carve(src, region_start: int, region_end: int, align: int, base: int, known_starts: set,
          claimed: Claimed, progress=None, kinds=None, inside_known=()):
    """Carve every recognizable file in a region. Returns Found records."""
    out = []
    region_end = min(region_end, getattr(src, "size", region_end) or region_end)
    found = list(find_headers(src, region_start, region_end, align, base, progress=progress))
    headers = [(off, kind) for off, kind, _weak in found]
    # A bare MP3 frame header is only 4 bytes and turns up by chance in other
    # files' data; it doesn't count as "a file starts here" for rebuilding.
    sure = [(off, kind) for off, kind, weak in found if not weak]
    starts = [h[0] for h in headers]
    inside: dict = {}  # stream kind -> end of the last one measured
    known_spans = list(inside_known)
    ks = [k[0] for k in known_spans]
    for idx, (off, kind) in enumerate(headers):
        if kind in more.STREAMS and off < inside.get(kind, -1):
            continue  # a frame or packet in the middle of a recording already found
        if kind in more.STREAMS and known_spans:
            i = bisect_right(ks, off) - 1
            if i >= 0 and known_spans[i][0] < off < known_spans[i][1] and known_spans[i][2] == kind:
                continue  # ... or one the folders name
        if kinds and kind not in ("isobmff", "riff") and GROUP[kind] not in kinds:
            continue  # quick skip; containers that hold several kinds are decided after measuring
        if off in known_starts or claimed.covering(off) is not None:
            continue
        nxt = starts[idx + 1] if idx + 1 < len(starts) else region_end
        try:
            if kind == "pdf":
                length, complete, ext, reason, brk, header = _measure_pdf(src, off, sure, region_end)
            else:
                length, complete, ext, reason, brk, header = measure(src, kind, off, region_end - off)
        except Exception:
            continue  # a header we can't make sense of: skip it, keep going
        if length <= 0:
            continue
        if kind in more.STREAMS:
            inside[kind] = off + length
        if kinds and group_of(kind, ext) not in kinds:
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
    return out, sure
