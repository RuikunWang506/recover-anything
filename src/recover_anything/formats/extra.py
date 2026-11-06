"""Still more file types: old Office documents and other OLE2 files (.doc,
.xls, .ppt, Outlook .msg, Thumbs.db), MPEG program streams (.MPG/.VOB from
DVD and older camcorders, .MOD/.TOD), RAR and 7z archives, Photoshop files,
and AMR voice memos from phones and voice recorders.

Same contract as more.py: probe(head), measure(read, start, limit) -> Result
with the exact length, validate(read, length, sized, ...) -> (status, note,
check). The rules for "ok" are the same too: a file whose contents can't be
checked is called recovered only when it was never deleted (`layout_known`).
"""
from __future__ import annotations

import struct
import zlib

from .others import Result

WEAK = ("its structure checks out, but its contents can't be checked here, so it can't prove all of them "
        "are its own")
SAME_KIND = ("every frame checks out, but another recording's frames would too, so it can't prove all of them "
             "are its own")


def _structure_verdict(m: Result, length: int, layout_known: bool, ok_note: str = "structure checked"):
    if m.end <= 0:
        return "corrupt", m.reason or "not a valid file", "structure"
    if not m.complete:
        return "partial", m.reason, "structure"
    if m.end < length:
        return "partial", "extra data after the end of the file", "structure"
    return ("ok", ok_note, "structure") if layout_known else ("unverified", WEAK, "structure")


# ------------------------------------------------------------------ OLE2 / Compound File
OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
MAX_OLE = 2 << 30


def probe_ole(head: bytes) -> bool:
    if len(head) < 76 or head[:8] != OLE_SIG or head[28:30] != b"\xfe\xff":
        return False
    major = struct.unpack_from("<H", head, 26)[0]
    shift, mini = struct.unpack_from("<HH", head, 30)
    return (major, shift) in ((3, 9), (4, 12)) and mini == 6


class _Ole:
    """Reads and cross-checks the allocation table and the folder of an OLE2 file."""

    def __init__(self, read, start: int, limit: int):
        self.read, self.start, self.limit = read, start, limit
        h = read(start, 512)
        self.ss = 1 << struct.unpack_from("<H", h, 30)[0]
        self.nfat, self.dir0 = struct.unpack_from("<II", h, 44)
        self.mf0, self.nmf, self.dif0, self.ndif = struct.unpack_from("<IIII", h, 60)
        self.cutoff = struct.unpack_from("<I", h, 56)[0]
        self.head = h

    def sector(self, n: int) -> bytes:
        return self.read(self.start + (n + 1) * self.ss, self.ss)

    def fat(self):
        """The allocation table, and the sectors that hold it."""
        if self.nfat > (MAX_OLE // self.ss) // (self.ss // 4) + 1:
            raise ValueError("implausibly many allocation sectors")
        locs = list(struct.unpack_from("<109I", self.head, 76))
        d, seen = self.dif0, set()
        while d not in (ENDOFCHAIN, FREESECT) and len(seen) <= self.ndif:
            if d in seen or (d + 2) * self.ss > self.limit:
                raise ValueError("broken extended allocation list")
            seen.add(d)
            blk = self.sector(d)
            if len(blk) < self.ss:
                raise ValueError("truncated")
            vals = struct.unpack("<%dI" % (self.ss // 4), blk)
            locs += vals[:-1]
            d = vals[-1]
        locs = [x for x in locs if x not in (FREESECT, ENDOFCHAIN)][:self.nfat]
        if len(locs) != self.nfat:
            raise ValueError("allocation sectors missing")
        fat = []
        for s in locs:
            if (s + 2) * self.ss > self.limit:
                raise ValueError("allocation sector past the end")
            blk = self.sector(s)
            if len(blk) < self.ss:
                raise ValueError("truncated")
            fat.extend(struct.unpack("<%dI" % (self.ss // 4), blk))
        for s in locs:
            if s >= len(fat) or fat[s] != FATSECT:
                raise ValueError("an allocation sector isn't marked as one")  # foreign data, almost surely
        for s in seen:
            if s >= len(fat) or fat[s] != DIFSECT:
                raise ValueError("an extended allocation sector isn't marked as one")
        return fat, locs

    @staticmethod
    def chain(fat, first: int, cap: int):
        out, seen = [], set()
        c = first
        while c != ENDOFCHAIN:
            if c >= len(fat) or c in seen or len(out) > cap:
                return None
            seen.add(c)
            out.append(c)
            c = fat[c]
        return out


def _ole_ext(names) -> str:
    n = set(names)
    if "WordDocument" in n:
        return ".doc"
    if "Workbook" in n or "Book" in n:
        return ".xls"
    if "PowerPoint Document" in n:
        return ".ppt"
    if any(x.startswith("__substg1.0_") for x in n):
        return ".msg"
    if "Catalog" in n:
        return ".db"  # Thumbs.db
    if "VisioDocument" in n:
        return ".vsd"
    return ".ole"


def measure_ole(read, start: int, limit: int, check: bool = False) -> Result:
    try:
        o = _Ole(read, start, limit)
        fat, locs = o.fat()
    except (ValueError, struct.error) as e:
        return Result(0, False, ".ole", str(e))
    last = max((i for i, v in enumerate(fat) if v != FREESECT), default=-1)
    if last < 0:
        return Result(0, False, ".ole", "empty allocation table")
    end = (last + 2) * o.ss
    if end > limit:
        return Result(limit, False, ".ole", "cut short", break_pos=limit)
    dirs = o.chain(fat, o.dir0, last + 1)
    if not dirs:
        return Result(end, False, ".ole", "the folder chain is broken")
    blob = b"".join(o.sector(s) for s in dirs[:4096])
    names, used = [], {s: "fat" for s in locs}
    for s in dirs:
        used[s] = "dir"
    entries = len(blob) // 128
    root = blob[:128]
    if len(root) < 128 or root[66] != 5:
        return Result(end, False, ".ole", "no root entry")
    for i in range(entries):
        e = blob[i * 128:(i + 1) * 128]
        typ = e[66]
        if typ == 0:
            continue
        if typ not in (1, 2, 5) or e[67] > 1:
            return Result(end, False, ".ole", "a folder entry is damaged")
        nlen = struct.unpack_from("<H", e, 64)[0]
        if nlen > 64 or nlen % 2:
            return Result(end, False, ".ole", "a folder entry is damaged")
        for k in (68, 72, 76):
            v = struct.unpack_from("<I", e, k)[0]
            if v != FREESECT and v >= entries:
                return Result(end, False, ".ole", "a folder entry points nowhere")
        name = e[:max(0, nlen - 2)].decode("utf-16-le", "replace")
        names.append(name)
        first = struct.unpack_from("<I", e, 116)[0]
        size = struct.unpack_from("<Q", e, 120)[0] & (0xFFFFFFFF if o.ss == 512 else (1 << 64) - 1)
        if check and (typ == 2 and size >= o.cutoff or typ == 5) and size:
            need = (size + o.ss - 1) // o.ss
            ch = o.chain(fat, first, last + 1)
            if ch is None or len(ch) < need or (typ == 2 and len(ch) != need):
                return Result(end, False, ".ole", "a stream's chain doesn't match its size")
            for s in ch:
                if s in used:
                    return Result(end, False, ".ole", "two parts claim the same sector")
                used[s] = "data"
    if check:
        if o.nmf:
            mf = o.chain(fat, o.mf0, last + 1)
            if mf is None or len(mf) != o.nmf:
                return Result(end, False, ".ole", "the small-stream table is broken")
            for s in mf:
                if s in used:
                    return Result(end, False, ".ole", "two parts claim the same sector")
                used[s] = "minifat"
        for i in range(last + 1):
            v = fat[i]
            if v not in (FREESECT, FATSECT, DIFSECT) and i not in used:
                return Result(end, False, ".ole", "the allocation table lists sectors nothing uses")
    return Result(end, True, _ole_ext(names))


def validate_ole(read, length: int, sized: bool, layout_known: bool = False, **_):
    return _structure_verdict(measure_ole(read, 0, length, check=True), length, layout_known)


# ------------------------------------------------------------------ MPEG program stream
PS_MIN_PACKS = 8


def _pack(h: bytes):
    """Length of the pack header at h, and its clock (or None)."""
    if len(h) < 14 or h[:4] != b"\x00\x00\x01\xba":
        return None
    b = h[4]
    if b >> 6 == 1:  # MPEG-2
        if not (b & 4 and h[6] & 4 and h[8] & 4 and h[9] & 1 and h[12] & 3 == 3):
            return None
        scr = ((b >> 3 & 7) << 30) | ((b & 3) << 28) | (h[5] << 20) | ((h[6] >> 3) << 15) | ((h[6] & 3) << 13) \
            | (h[7] << 5) | (h[8] >> 3)
        return 14 + (h[13] & 7), scr
    if b >> 4 == 2:  # MPEG-1
        if not (b & 1 and h[6] & 1 and h[8] & 1 and h[9] & 0x80 and h[11] & 1):
            return None
        scr = ((b >> 1 & 7) << 30) | (h[5] << 22) | ((h[6] >> 1) << 15) | (h[7] << 7) | (h[8] >> 1)
        return 12, scr
    return None


def probe_ps(head: bytes) -> bool:
    return _pack(head[:16]) is not None


def measure_ps(read, start: int, limit: int) -> Result:
    from .more import _Win
    w = _Win(read, start)
    pos, packs, clock, reason, last = 0, 0, None, "packs stop here", None
    first_clock = None
    ext = ".mpg"
    while pos < limit:
        p = _pack(w.get(pos, 16))
        if p is None:
            break
        n, scr = p
        if clock is not None and not (clock <= scr <= clock + 90000 * 10):
            reason = "the clock jumps (another recording's data?)"
            break
        if first_clock is None:
            first_clock = scr
        clock = scr
        last = pos
        q = pos + n
        ok = True
        while q + 4 <= limit:
            h = w.get(q, 6)
            if h[:3] != b"\x00\x00\x01":
                ok = False
                break
            sid = h[3]
            if sid == 0xBA:
                break
            if sid == 0xB9:  # program end code: the recording ends here
                return Result(q + 4, True, ext, last=last, kind="start" if first_clock == 0 else None)
            if sid < 0xBB or len(h) < 6 or q + 6 > limit:
                ok = False
                break
            q += 6 + struct.unpack(">H", h[4:6])[0]
        if not ok or q > limit:
            break
        pos = q
        packs += 1
    if packs < PS_MIN_PACKS:
        return Result(0, False, ext, "too short")
    return Result(pos, False, ext, reason, break_pos=pos, last=last,
                  kind="start" if first_clock == 0 else None)


def validate_ps(read, length: int, sized: bool, after: bytes = b"", grid=None, layout_known: bool = False, **_):
    from .more import NO_START, _sized_verdict, same_cluster
    m = measure_ps(read, 0, length)
    a = m.last if m.last is not None else 0
    tail_ok = layout_known or m.complete or same_cluster(grid, a, max(a, m.end - 1))
    ends_here = (m.end == length and not m.complete and after[:4] != b"\x00\x00\x01\xba"
                 and same_cluster(grid, a, length + 3))
    st = _sized_verdict(m, length, sized, "every pack checked", "structure", ends_here, tail_ok)
    if st[0] == "ok" and not sized and m.kind != "start":
        return "unverified", NO_START, "structure"
    if st[0] == "ok" and not layout_known:
        return "unverified", SAME_KIND, "structure"  # packs sit at the same places in every such file
    return st


# ------------------------------------------------------------------ RAR
RAR4 = b"Rar!\x1a\x07\x00"
RAR5 = b"Rar!\x1a\x07\x01\x00"


def probe_rar(head: bytes) -> bool:
    return head[:7] == RAR4 or head[:8] == RAR5


def _vint(b: bytes, p: int):
    v, shift = 0, 0
    for i in range(10):
        if p + i >= len(b):
            return None
        c = b[p + i]
        v |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return v, p + i + 1
    return None


def measure_rar(read, start: int, limit: int, check: bool = False) -> Result:
    """Walks every block and checks every header's checksum. With `check`,
    stored (uncompressed) files are checksummed too; returns kind='full' if
    every file in it could be checked that way."""
    head = read(start, 8)
    all_checked = True
    if head[:8] == RAR5:
        pos = 8
        while pos < limit:
            h = read(start + pos, 4 + 3 * 10)
            if len(h) < 7:
                break
            hs = _vint(h, 4)
            if not hs or hs[0] < 2 or hs[0] > 2 << 20:
                return Result(pos, False, ".rar", "a block header is damaged", break_pos=pos)
            size, q = hs
            hdr = read(start + pos + 4, (q - 4) + size)
            if zlib.crc32(hdr) & 0xFFFFFFFF != struct.unpack("<I", h[:4])[0]:
                return Result(pos, False, ".rar", "a block's checksum doesn't match", break_pos=pos)
            body = hdr[q - 4:]
            t = _vint(body, 0)
            f = t and _vint(body, t[1])
            if not f:
                return Result(pos, False, ".rar", "a block header is damaged", break_pos=pos)
            typ, flags = t[0], f[0]
            r = f[1]
            if flags & 1:
                ex = _vint(body, r)
                if not ex:
                    return Result(pos, False, ".rar", "a block header is damaged", break_pos=pos)
                r = ex[1]
            data = 0
            if flags & 2:
                ds = _vint(body, r)
                if not ds:
                    return Result(pos, False, ".rar", "a block header is damaged", break_pos=pos)
                data = ds[0]
            if typ == 2:
                all_checked = False  # RAR5 compression can't be undone here
            pos += q + size + data
            if typ == 5:
                return Result(pos, True, ".rar", kind="full" if all_checked else None)
        return Result(pos, False, ".rar", "no end block", break_pos=pos)
    if head[:7] != RAR4:
        return Result(0, False, ".rar", "not a RAR archive")
    pos = 7
    while pos < limit:
        h = read(start + pos, 11)
        if len(h) < 7:
            break
        crc, typ, flags, hsize = struct.unpack("<HBHH", h[:7])
        if typ < 0x72 or typ > 0x7B or hsize < 7:
            return Result(pos, False, ".rar", "a block header is damaged", break_pos=pos)
        hdr = read(start + pos, hsize)
        if len(hdr) < hsize or (zlib.crc32(hdr[2:]) & 0xFFFF) != crc:
            return Result(pos, False, ".rar", "a block's checksum doesn't match", break_pos=pos)
        add = 0
        if flags & 0x8000 or typ in (0x74, 0x7A):
            add = struct.unpack("<I", hdr[7:11])[0]
            if typ in (0x74, 0x7A) and flags & 0x100 and hsize >= 36:
                add |= struct.unpack("<I", hdr[32:36])[0] << 32
        if typ == 0x74:
            method = hdr[25] if hsize > 25 else 0
            if check and method == 0x30 and not flags & 0x04:  # stored, not encrypted: check its data
                want = struct.unpack("<I", hdr[16:20])[0]
                c, got = 0, 0
                while got < add:
                    piece = read(start + pos + hsize + got, min(1 << 20, add - got))
                    if not piece:
                        break
                    c = zlib.crc32(piece, c)
                    got += len(piece)
                if got < add or (c & 0xFFFFFFFF) != want:
                    return Result(pos, False, ".rar", "a stored file's checksum doesn't match", break_pos=pos)
            else:
                all_checked = False
        pos += hsize + add
        if typ == 0x7B:
            return Result(pos, True, ".rar", kind="full" if all_checked else None)
    # an old archive may end without an end block: after the last file
    return Result(pos, False, ".rar", "no end block", break_pos=pos)


def validate_rar(read, length: int, sized: bool, layout_known: bool = False, **_):
    m = measure_rar(read, 0, length, check=True)
    if m.complete and m.end == length and m.kind == "full":
        return "ok", "every file's checksum verified", "full"
    return _structure_verdict(m, length, layout_known, "every header's checksum verified")


# ------------------------------------------------------------------ 7z
SEVENZ = b"7z\xbc\xaf\x27\x1c"


def probe_7z(head: bytes) -> bool:
    return len(head) >= 32 and head[:6] == SEVENZ and head[6] == 0 and head[7] <= 4 \
        and zlib.crc32(head[12:32]) & 0xFFFFFFFF == struct.unpack_from("<I", head, 8)[0]


def measure_7z(read, start: int, limit: int) -> Result:
    h = read(start, 32)
    if not probe_7z(h):
        return Result(0, False, ".7z", "damaged start header")
    off, size, crc = struct.unpack_from("<QQI", h, 12)
    end = 32 + off + size
    if size == 0 or size > 1 << 30 or end > limit:
        return Result(min(end, limit) if end > limit else 0, False, ".7z", "cut short", break_pos=limit)
    c, got = 0, 0
    while got < size:
        piece = read(start + 32 + off + got, min(1 << 20, size - got))
        if not piece:
            break
        c = zlib.crc32(piece, c)
        got += len(piece)
    if got < size or (c & 0xFFFFFFFF) != crc:
        return Result(32 + off, False, ".7z", "its index's checksum doesn't match", break_pos=32 + off)
    return Result(end, True, ".7z")


def validate_7z(read, length: int, sized: bool, layout_known: bool = False, **_):
    return _structure_verdict(measure_7z(read, 0, length), length, layout_known, "its index's checksum verified")


# ------------------------------------------------------------------ Photoshop
def probe_psd(head: bytes) -> bool:
    if len(head) < 26 or head[:4] != b"8BPS" or head[6:12] != b"\0" * 6:
        return False
    ver = struct.unpack_from(">H", head, 4)[0]
    ch, hh, ww, depth, mode = struct.unpack_from(">HIIHH", head, 12)
    return ver in (1, 2) and 1 <= ch <= 56 and 0 < hh <= 300000 and 0 < ww <= 300000 and depth in (1, 8, 16, 32) \
        and mode <= 9


def _psd_resources_ok(read, start: int, pos: int, stop: int) -> bool:
    """Image resources are blocks that each start with a known signature."""
    q = pos
    while q < stop:
        sig = read(start + q, 7)
        if len(sig) < 7 or sig[:4] not in (b"8BIM", b"MeSa", b"AgHg", b"PHUT", b"DCSR"):
            return False
        q += 6 + ((sig[6] + 2) & ~1)  # signature, id, padded name
        dl = read(start + q, 4)
        if len(dl) < 4:
            return False
        n = struct.unpack(">I", dl)[0]
        q += 4 + n + (n & 1)
    return q == stop


def measure_psd(read, start: int, limit: int) -> Result:
    h = read(start, 26)
    if not probe_psd(h):
        return Result(0, False, ".psd", "damaged header")
    ver = struct.unpack_from(">H", h, 4)[0]
    ch, hh, ww, depth, _mode = struct.unpack_from(">HIIHH", h, 12)
    ext = ".psd" if ver == 1 else ".psb"
    pos = 26
    # colour data, image resources, layers and masks (an 8-byte length in a PSB)
    for i, width in enumerate((4, 4, 8 if ver == 2 else 4)):
        n = read(start + pos, width)
        if len(n) < width:
            return Result(pos, False, ext, "cut short", break_pos=pos)
        ln = int.from_bytes(n, "big")
        if pos + width + ln > limit:
            return Result(limit, False, ext, "cut short", break_pos=limit)
        if i == 1 and not _psd_resources_ok(read, start, pos + width, pos + width + ln):
            return Result(pos, False, ext, "an image resource is damaged", break_pos=pos)
        pos += width + ln
    comp = struct.unpack(">H", read(start + pos, 2))[0] if pos + 2 <= limit else -1
    pos += 2
    row = (ww * depth + 7) // 8
    if comp == 0:
        pos += ch * hh * row
    elif comp == 1:
        cw = 2 if ver == 1 else 4
        rows = ch * hh
        if rows > 50_000_000:
            return Result(pos, False, ext, "implausible size")
        tab = read(start + pos, rows * cw)
        if len(tab) < rows * cw:
            return Result(pos, False, ext, "cut short", break_pos=pos)
        counts = struct.unpack(">%d%s" % (rows, "H" if cw == 2 else "I"), tab)
        if max(counts) > row * 2 + 2:
            return Result(pos, False, ext, "the picture's row table is damaged", break_pos=pos)
        pos += rows * cw + sum(counts)
    else:
        return Result(pos, False, ext, "compressed picture data: its length isn't recorded")
    if pos > limit:
        return Result(limit, False, ext, "cut short", break_pos=limit)
    return Result(pos, True, ext)


def validate_psd(read, length: int, sized: bool, layout_known: bool = False, **_):
    return _structure_verdict(measure_psd(read, 0, length), length, layout_known)


# ------------------------------------------------------------------ AMR voice memos
AMR_NB = b"#!AMR\n"
AMR_WB = b"#!AMR-WB\n"
_NB = (12, 13, 15, 17, 19, 20, 26, 31, 5, 6, 5, 5, -1, -1, -1, 0)
_WB = (17, 23, 32, 36, 40, 46, 50, 58, 60, 5, -1, -1, -1, -1, 0, 0)
AMR_MIN_FRAMES = 16


def probe_amr(head: bytes) -> bool:
    return head[:6] == AMR_NB or head[:9] == AMR_WB


def measure_amr(read, start: int, limit: int) -> Result:
    from .more import _Win
    w = _Win(read, start)
    wb = w.get(0, 9) == AMR_WB
    sizes = _WB if wb else _NB
    pos = 9 if wb else 6
    ext = ".awb" if wb else ".amr"
    n, last = 0, None
    while pos < limit:
        b = w.get(pos, 1)
        if not b:
            break
        h = b[0]
        ft = (h >> 3) & 15
        if h & 0x83 or sizes[ft] < 0:  # padding bits must be zero; reserved frame types don't occur
            break
        size = sizes[ft]
        if pos + 1 + size > limit:
            break
        if h == 0 and not any(w.get(pos + 1, size)):
            break  # zero bytes read as a silent frame: this is blank space after the recording
        last = pos
        pos += 1 + size
        n += 1
    if n < AMR_MIN_FRAMES:
        return Result(0, False, ext, "not enough audio frames")
    return Result(pos, False, ext, "frames stop here", break_pos=pos, last=last, kind="start")


def validate_amr(read, length: int, sized: bool, after: bytes = b"", grid=None, layout_known: bool = False, **_):
    from .more import _sized_verdict, same_cluster
    m = measure_amr(read, 0, length)
    a = m.last if m.last is not None else 0
    tail_ok = layout_known or same_cluster(grid, a, max(a, m.end - 1))
    sizes = _WB if read(0, 9) == AMR_WB else _NB
    ft = (after[0] >> 3) & 15 if after else 0
    # another frame could follow (blank space, all zeros, isn't one)
    nxt_ok = bool(after) and not after[0] & 0x83 and sizes[ft] >= 0 and any(after[:1 + sizes[ft]])
    ends_here = m.end == length and not nxt_ok and same_cluster(grid, a, length)
    st = _sized_verdict(m, length, sized, "every frame checked", "structure", ends_here, tail_ok)
    if st[0] == "ok" and not layout_known:
        return "unverified", SAME_KIND, "structure"  # every frame of every such file looks alike
    return st


# ------------------------------------------------------------------ registry
# kind: (probe, measure, validate, max size, group)
KINDS = {
    "ole": (probe_ole, measure_ole, validate_ole, MAX_OLE, "document"),
    "rar": (probe_rar, measure_rar, validate_rar, 1 << 40, "document"),
    "7z": (probe_7z, measure_7z, validate_7z, 1 << 40, "document"),
    "psd": (probe_psd, measure_psd, validate_psd, 4 << 30, "photo"),
    "amr": (probe_amr, measure_amr, validate_amr, 1 << 30, "audio"),
    "ps": (probe_ps, measure_ps, validate_ps, 1 << 40, "video"),
}
STREAMS = {"ps"}
DENSE = {"amr", "ps"}
SIGNATURES = (rb"|\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1|Rar!\x1a\x07[\x00\x01]|7z\xbc\xaf\x27\x1c|8BPS[\x00][\x01\x02]"
              rb"|#!AMR")
# Headers that begin with zero bytes. Most of a card is zeros, so these are
# looked for only where a file can start, never byte by byte (carve._at_sectors).
AT_SECTOR = (b"\x00\x00\x01\xba",)
