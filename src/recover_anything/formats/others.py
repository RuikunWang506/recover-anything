"""Exact-length measurement and validation for non-JPEG formats.

Each measure_* takes read(off, n) and an absolute start, and returns a Result:
end (bytes from start), complete (looked whole), ext, and a reason.
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from array import array


class Result:
    __slots__ = ("end", "complete", "ext", "reason", "kind", "break_pos", "refs", "last")

    def __init__(self, end, complete, ext, reason="ok", kind=None, break_pos=None, refs=(), last=None):
        self.end, self.complete, self.ext, self.reason, self.kind, self.break_pos = end, complete, ext, reason, kind, break_pos
        self.refs = refs  # offsets (from the start) of files this one contains, e.g. a RAW's previews
        self.last = last  # for streams: where the last checked frame or packet header is


# ------------------------------------------------------------------ ISO BMFF
TOP_LEVEL = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pnot", b"moof", b"mfra",
             b"sidx", b"styp", b"emsg", b"prft", b"udta", b"PICT", b"junk", b"pdin", b"jp2h", b"ssix", b"mvex",
             b"Xtra", b"beam", b"CNOP", b"PANA", b"LEIC", b"thum"}
BRAND_EXT = {b"qt  ": ".mov", b"heic": ".heic", b"heix": ".heic", b"hevc": ".heic", b"heim": ".heic",
             b"heis": ".heic", b"mif1": ".heic", b"msf1": ".heic", b"avif": ".avif", b"avis": ".avif",
             b"crx ": ".cr3", b"M4A ": ".m4a", b"M4V ": ".m4v", b"3gp4": ".3gp", b"3gp5": ".3gp", b"3gp6": ".3gp",
             b"3g2a": ".3g2", b"XAVC": ".mp4", b"MSNV": ".mp4"}


def probe_isobmff(head: bytes) -> bool:
    if len(head) < 12 or head[4:8] != b"ftyp":
        return False
    size = struct.unpack(">I", head[:4])[0]
    return 12 <= size <= 512 and all(32 <= b < 127 for b in head[8:12])


def isobmff_ext(head: bytes) -> str:
    brand = head[8:12]
    if brand in BRAND_EXT:
        return BRAND_EXT[brand]
    if brand.startswith(b"3gp"):
        return ".3gp"
    return ".mp4"


def measure_isobmff(read, start: int, limit: int) -> Result:
    head = read(start, 32)
    ext = isobmff_ext(head)
    pos, seen = 0, []
    while pos < limit:
        h = read(start + pos, 16)
        if len(h) < 8:
            break
        size = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        if typ not in TOP_LEVEL:
            break
        if size == 1:
            if len(h) < 16:
                break
            size = struct.unpack(">Q", h[8:16])[0]
        elif size == 0:
            return Result(pos, False, ext, "box runs to end of disk")
        if size < 8 or pos + size > limit:
            break
        seen.append(typ)
        pos += size
    s = set(seen)
    if ext in (".heic", ".avif", ".cr3"):
        complete = b"meta" in s or b"moov" in s
    else:
        complete = b"moov" in s and (b"mdat" in s or b"moof" in s)
    return Result(pos, complete, ext, "ok" if complete else "missing %s" % ("moov" if b"moov" not in s else "mdat"))


def _boxes(read, off: int, end: int):
    while off + 8 <= end:
        h = read(off, 16)
        if len(h) < 8:
            return
        size = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", h[8:16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            return
        yield typ, off + hdr, off + size
        off += size


def validate_isobmff(read, length: int, refs: set | None = None) -> tuple[str, str, int | None]:
    """('ok'|'corrupt'|'unverified', why, bad_offset). Checks every video
    sample's position and its NAL length prefix, which catches clusters that
    belong to other files. If `refs` is given, the offsets of Motion-JPEG
    frames are added to it (those frames are part of this file)."""
    moov = None
    for typ, b, e in _boxes(read, 0, length):
        if typ == b"moov":
            moov = (b, e)
    if not moov:
        return "unverified", "no moov box", None
    checked = total = 0
    for typ, b, e in _boxes(read, *moov):
        if typ != b"trak":
            continue
        stbl = _find(read, b, e, [b"mdia", b"minf", b"stbl"])
        if not stbl:
            continue
        codec = track_codec(read, stbl)
        nal = codec in NAL_CODECS
        try:
            tables = sample_tables(read, stbl)
        except TooManySamples:
            if nal:
                return "unverified", "too many frames to check one by one", None
            continue  # e.g. hours of uncompressed audio: nothing worth checking per sample
        if tables is None:
            return "corrupt", "the video's index is damaged", None
        codec, offsets, sizes, stsc = tables
        if not offsets or not sizes:
            continue
        total += len(sizes)
        if total > MAX_SAMPLES:
            if nal:
                return "unverified", "too many frames to check one by one", None
            total -= len(sizes)
            continue
        hevc = codec in (b"hvc1", b"hev1")
        collect = refs is not None and codec in MJPEG_CODECS
        for si, (pos, sz) in enumerate(iter_samples(offsets, sizes, stsc)):
            if pos + sz > length:
                return "corrupt", "sample %d lies past the end of the file" % si, pos
            if collect:
                refs.add(pos)
            if nal and sz >= 5:
                h = read(pos, 5)
                if len(h) < 5:
                    return "corrupt", "video frame %d (byte %d) is missing" % (si, pos), pos
                ln = struct.unpack(">I", h[:4])[0]
                hdr = h[4]
                if ln == 0 or ln + 4 > sz or hdr & 0x80:
                    return "corrupt", "video frame %d (byte %d) is not where the index says" % (si, pos), pos
                t = (hdr >> 1) & 0x3F if hevc else hdr & 0x1F
                if (hevc and t > 40) or (not hevc and t in (0, 24, 25, 26, 27, 28, 29, 30, 31)):
                    return "corrupt", "video frame %d (byte %d) has an impossible unit type" % (si, pos), pos
                checked += 1
            elif codec in MJPEG_CODECS and sz >= 3:
                # every Motion-JPEG frame is a JPEG: it starts with FF D8 FF
                if read(pos, 3) != b"\xff\xd8\xff":
                    return "corrupt", "video frame %d (byte %d) is not where the index says" % (si, pos), pos
                checked += 1
            # other tracks (AAC audio, timecode...): the index can't be checked against their bytes
    if checked == 0:
        return "unverified", "its frames can't be checked one by one", None
    return "ok", "%d frames checked" % checked, None


MAX_SAMPLES = 2_000_000  # frames per file: a 9-hour 60 fps video
NAL_CODECS = (b"avc1", b"avc3", b"hvc1", b"hev1")
MJPEG_CODECS = (b"jpeg", b"mjpa", b"mjpb", b"AVDJ", b"dmb1")


class TooManySamples(Exception):
    """The index lists more frames than are worth checking one by one."""


def _be_array(code: str, raw: bytes) -> array:
    a = array(code)
    a.frombytes(raw)
    if sys.byteorder == "little":
        a.byteswap()
    return a


def _counted(body: bytes, at: int, width: int):
    """Read a count followed by `count` big-endian integers, refusing counts
    that don't fit in the box (damaged or hostile files)."""
    if len(body) < at + 4:
        return None
    n = struct.unpack(">I", body[at:at + 4])[0]
    if n > MAX_SAMPLES:
        raise TooManySamples(n)
    if len(body) < at + 4 + width * n:
        return None
    return _be_array("I" if width == 4 else "Q", body[at + 4:at + 4 + width * n])


class _Fixed:
    """Sample sizes when every sample has the same size (no per-sample list)."""
    __slots__ = ("size", "n")

    def __init__(self, size: int, n: int):
        self.size, self.n = size, n

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        if not -self.n <= i < self.n:
            raise IndexError(i)
        return self.size


def track_codec(read, stbl):
    """The four-letter codec of a track ('avc1', 'hvc1', 'mp4a', 'sowt'...)."""
    for t2, b2, e2 in _boxes(read, *stbl):
        if t2 == b"stsd":
            body = read(b2, min(e2 - b2, 64))
            return body[12:16] if len(body) >= 16 else None
    return None


def sample_tables(read, stbl):
    """(codec, chunk offsets, sample sizes, sample-to-chunk table) for one
    track, or None if the tables are inconsistent (TooManySamples if they are
    too big to check). The sample-to-chunk table is flat: first chunk,
    samples per chunk, description index, repeated."""
    codec, offsets, sizes, stsc = None, (), (), ()
    for t2, b2, e2 in _boxes(read, *stbl):
        if t2 not in (b"stsd", b"stco", b"co64", b"stsz", b"stsc"):
            continue
        if e2 - b2 > (256 << 20):
            return None
        body = read(b2, e2 - b2)
        if t2 == b"stsd":
            if len(body) >= 16:
                codec = body[12:16]
        elif t2 in (b"stco", b"co64"):
            offsets = _counted(body, 4, 4 if t2 == b"stco" else 8)
            if offsets is None:
                return None
        elif t2 == b"stsz":
            if len(body) < 12:
                return None
            fixed, n = struct.unpack(">II", body[4:12])
            if fixed:
                sizes = _Fixed(fixed, n)  # no list: the count alone costs nothing
            else:
                if n > MAX_SAMPLES:
                    raise TooManySamples(n)
                if len(body) < 12 + 4 * n:
                    return None
                sizes = _be_array("I", body[12:12 + 4 * n])
        elif t2 == b"stsc":
            if len(body) < 8:
                return None
            n = struct.unpack(">I", body[4:8])[0]
            if n > MAX_SAMPLES:
                raise TooManySamples(n)
            if len(body) < 8 + 12 * n:
                return None
            stsc = _be_array("I", body[8:8 + 12 * n])
            firsts = stsc[0::3]
            if any(x > y for x, y in zip(firsts, firsts[1:])):
                return None  # runs must be listed in order
    if isinstance(sizes, _Fixed) and codec not in NAL_CODECS + MJPEG_CODECS:
        return codec, (), (), stsc  # e.g. PCM audio: nothing to check per sample
    return codec, offsets, sizes, stsc


def iter_samples(offsets, sizes, stsc):
    """(file offset, size) of every sample in one track. Chunks and the
    sample-to-chunk runs are walked together, so this is linear in the size
    of the tables."""
    n, runs = len(sizes), len(stsc) // 3
    si, j, per = 0, 0, 1
    for ci, pos in enumerate(offsets, start=1):
        while j < runs and stsc[3 * j] <= ci:
            per = stsc[3 * j + 1]
            j += 1
        for _ in range(min(per, n - si)):
            sz = sizes[si]
            yield pos, sz
            pos += sz
            si += 1
        if si >= n:
            return


def _find(read, b, e, path):
    for name in path:
        for typ, b2, e2 in _boxes(read, b, e):
            if typ == name:
                b, e = b2, e2
                break
        else:
            return None
    return b, e


# ---------------------------------------------------------------------- PNG
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def probe_png(head: bytes) -> bool:
    return head[:8] == PNG_SIG


def measure_png(read, start: int, limit: int) -> Result:
    pos = 8
    while pos < limit:
        h = read(start + pos, 8)
        if len(h) < 8:
            return Result(pos, False, ".png", "truncated", break_pos=pos)
        ln = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        if ln > 0x7FFFFFFF or not typ.isalpha():
            return Result(pos, False, ".png", "bad chunk", break_pos=pos)
        if pos + 12 + ln > limit:
            return Result(pos, False, ".png", "chunk runs past the end", break_pos=pos)
        crc_run, got, at = 0, 0, start + pos + 4
        while got < 4 + ln:
            piece = read(at + got, min(1 << 20, 4 + ln - got))
            if not piece:
                break
            crc_run = zlib.crc32(piece, crc_run)
            got += len(piece)
        tail = read(start + pos + 8 + ln, 4)
        if got < 4 + ln or len(tail) < 4:
            return Result(pos, False, ".png", "truncated", break_pos=pos)
        if crc_run & 0xFFFFFFFF != struct.unpack(">I", tail)[0]:
            return Result(pos, False, ".png", "checksum mismatch", break_pos=pos)
        pos += 12 + ln
        if typ == b"IEND":
            return Result(pos, True, ".png")
    return Result(pos, False, ".png", "too long")


# ---------------------------------------------------------------------- GIF
def probe_gif(head: bytes) -> bool:
    return head[:6] in (b"GIF87a", b"GIF89a")


def measure_gif(read, start: int, limit: int) -> Result:
    data = read(start, min(limit, 64 * 1024 * 1024))
    if len(data) < 13:
        return Result(0, False, ".gif", "truncated")
    flags = data[10]
    pos = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    while pos < len(data):
        b = data[pos]
        if b == 0x3B:
            return Result(pos + 1, True, ".gif")
        if b == 0x21:
            pos += 2
        elif b == 0x2C:
            if pos + 10 > len(data):
                break
            lf = data[pos + 9]
            pos += 10 + (3 * (2 << (lf & 7)) if lf & 0x80 else 0) + 1
        else:
            return Result(pos, False, ".gif", "bad block", break_pos=pos)
        while pos < len(data):
            n = data[pos]
            pos += 1 + n
            if n == 0:
                break
    return Result(pos, False, ".gif", "truncated", break_pos=pos)


# --------------------------------------------------------------------- RIFF
RIFF_EXT = {b"AVI ": ".avi", b"WAVE": ".wav", b"WEBP": ".webp"}


def probe_riff(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] in RIFF_EXT


def measure_riff(read, start: int, limit: int, want_refs: bool = False) -> Result:
    h = read(start, 12)
    size = struct.unpack("<I", h[4:8])[0] + 8
    size += size & 1
    ext = RIFF_EXT.get(h[8:12], ".riff")
    if size > limit:
        return Result(0, False, ext, "too long")
    refs = set()
    # walk top-level chunks to make sure the size is believable
    pos = 12
    while pos + 8 <= size:
        c = read(start + pos, 12)
        if len(c) < 8 or not all(32 <= x < 127 for x in c[:4]):
            return Result(size, False, ext, "chunk structure broken at %d" % pos, break_pos=pos)
        n = struct.unpack("<I", c[4:8])[0]
        if want_refs and c[:4] == b"LIST" and c[8:12] == b"movi":
            _riff_frames(read, start, pos + 12, min(size, pos + 8 + n), refs)
        pos += 8 + n + (n & 1)
    return Result(size, pos == size or pos == size - 1, ext, refs=refs)


def _riff_frames(read, start: int, pos: int, end: int, refs: set, depth: int = 0) -> None:
    """Where each chunk's data starts inside an AVI 'movi' list (one level of
    'rec ' lists): a Motion-JPEG AVI stores every frame as a JPEG there."""
    while pos + 8 <= end and len(refs) < 1000000:
        c = read(start + pos, 12)
        if len(c) < 8:
            return
        n = struct.unpack("<I", c[4:8])[0]
        if c[:4] == b"LIST" and depth == 0:
            _riff_frames(read, start, pos + 12, min(end, pos + 8 + n), refs, 1)
        else:
            refs.add(pos + 8)
        pos += 8 + n + (n & 1)


# --------------------------------------------------------------------- TIFF
def probe_tiff(head: bytes) -> bool:
    return head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00")


_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}


def measure_tiff(read, start: int, limit: int) -> Result:
    """RAW files (CR2, NEF, ARW, DNG, ORF, RW2...) are TIFF containers. The file
    ends where the last thing any IFD points to ends."""
    head = read(start, 16)
    bo = "<" if head[:2] == b"II" else ">"
    ext = ".tif"
    if head[8:10] == b"CR":
        ext = ".cr2"
    elif head[:4] in (b"IIRO", b"IIRS"):
        ext = ".orf"
    elif head[:4] == b"IIU\x00":
        ext = ".rw2"
    end = 8
    make = b""
    seen = set()
    todo = [struct.unpack(bo + "I", head[4:8])[0]]
    strip_pairs = {(0x0111, 0x0117), (0x0144, 0x0145), (0x0201, 0x0202)}
    refs = set()
    while todo and len(seen) < 64:
        ifd = todo.pop()
        if ifd in seen or ifd < 8 or ifd > limit:
            continue
        seen.add(ifd)
        cnt_b = read(start + ifd, 2)
        if len(cnt_b) < 2:
            continue
        n = struct.unpack(bo + "H", cnt_b)[0]
        if n == 0 or n > 1000:
            continue
        raw = read(start + ifd + 2, 12 * n + 4)
        if len(raw) < 12 * n + 4:
            continue
        tags = {}
        for i in range(n):
            tag, typ, count, val = struct.unpack(bo + "HHII", raw[12 * i:12 * i + 12])
            ts = _TYPE_SIZE.get(typ, 1)
            tags[tag] = (typ, count, val)
            if ts * count > 4 and val + ts * count <= limit:
                end = max(end, val + ts * count)
            if tag in (0x8769, 0x014A, 0x8825, 0xA005) and typ in (4, 13):
                if count == 1:
                    todo.append(val)
                elif val + 4 * count <= limit:
                    todo.extend(struct.unpack(bo + "%dI" % count, read(start + val, 4 * count)))
            if tag == 0x010F:
                make = read(start + val, min(count, 32)) if count > 4 else raw[12 * i + 8:12 * i + 12]
            if tag == 0xC612:
                ext = ".dng"
        end = max(end, ifd + 2 + 12 * n + 4)
        nxt = struct.unpack(bo + "I", raw[12 * n:12 * n + 4])[0]
        if nxt:
            todo.append(nxt)
        for off_tag, len_tag in strip_pairs:
            if off_tag in tags and len_tag in tags:
                (to, co, vo), (tl, cl, vl) = tags[off_tag], tags[len_tag]
                offs = [vo] if co == 1 else list(_vals(read, start, bo, to, co, vo))
                lens = [vl] if cl == 1 else list(_vals(read, start, bo, tl, cl, vl))
                for o, ln in zip(offs, lens):
                    if o + ln <= limit:
                        end = max(end, o + ln)
                        if len(refs) < 100000:
                            refs.add(o)
    if ext == ".tif":
        m = make.upper()
        if m.startswith(b"NIKON"):
            ext = ".nef"
        elif m.startswith(b"SONY"):
            ext = ".arw"
        elif m.startswith(b"PENTAX") or m.startswith(b"RICOH"):
            ext = ".pef"
        elif m.startswith(b"CANON"):
            ext = ".cr2"
    return Result(end, end > 1024, ext, "ok" if end > 1024 else "too small", refs=refs)


def _vals(read, start, bo, typ, count, off):
    fmt = {3: "H", 4: "I", 13: "I"}.get(typ)
    if not fmt or count > 100000:
        return []
    size = struct.calcsize(fmt)
    raw = read(start + off, size * count)
    raw = raw[:len(raw) // size * size]  # a cut-off read: keep the whole values
    return struct.unpack(bo + "%d%s" % (len(raw) // size, fmt), raw)


# ---------------------------------------------------------------------- PDF
def probe_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


def measure_pdf(read, start: int, limit: int, stop_at=None) -> Result:
    pos, eofs, chunk = 0, [], 1 << 20
    tail = b""
    while pos < limit:
        data = read(start + pos, min(chunk, limit - pos))  # never past the next file's header
        if not data:
            break
        buf = tail + data
        base = pos - len(tail)
        i = buf.find(b"%%EOF")
        while i >= 0:
            if not eofs or base + i + 5 > eofs[-1]:
                eofs.append(base + i + 5)
            i = buf.find(b"%%EOF", i + 1)
        del eofs[:-4096]
        if stop_at and eofs and stop_at(pos + len(data)):
            break
        tail = buf[-5:]
        pos += len(data)
        if eofs and data.count(0) == len(data):
            break
    if not eofs:
        return Result(0, False, ".pdf", "no end marker")
    # The file ends at the last end marker that is its own: the startxref
    # before it points back to a cross-reference section inside the file. A
    # later one (a deleted PDF's leftover tail in the free space after this
    # one) points somewhere else. An update appended to the PDF has its own.
    end = eofs[-1]
    for e in reversed(eofs):
        lo = max(0, e - 5 - 48)
        m = re.search(rb"startxref\s+(\d+)\s*$", read(start + lo, e - 5 - lo))
        if not m or int(m.group(1)) >= e:
            continue
        h = read(start + int(m.group(1)), 32)
        if h.startswith(b"xref") or re.match(rb"\d+\s+\d+\s+obj", h):
            end = e
            break
    nl = read(start + end, 2)
    if nl[:2] == b"\r\n":
        end += 2
    elif nl[:1] in (b"\n", b"\r"):
        end += 1
    return Result(end, True, ".pdf")


# ---------------------------------------------------------------------- ZIP
def probe_zip(head: bytes) -> bool:
    return len(head) >= 6 and head[:4] == b"PK\x03\x04" and 10 <= struct.unpack("<H", head[4:6])[0] <= 63


def measure_zip(read, start: int, limit: int) -> Result:
    pos, chunk = 0, 1 << 20
    while pos < limit:
        data = read(start + pos, chunk + 22)
        if not data:
            break
        i = data.find(b"PK\x05\x06")
        while i >= 0:
            rec = data[i:i + 22]
            if len(rec) < 22:
                rec = read(start + pos + i, 22)
            if len(rec) < 22:
                break
            _, _, _, _, total, cd_size, cd_off, clen = struct.unpack("<IHHHHIIH", rec[:22])
            eocd = pos + i
            if cd_off + cd_size == eocd:
                cd = read(start + cd_off, min(cd_size, 1 << 16))
                ext = ".zip"
                if b"[Content_Types].xml" in cd or b"word/" in cd or b"xl/" in cd or b"ppt/" in cd:
                    ext = ".docx" if b"word/" in cd else ".xlsx" if b"xl/" in cd else ".pptx" if b"ppt/" in cd else ".zip"
                elif b"META-INF/container.xml" in cd:
                    ext = ".epub"
                elif b"AndroidManifest.xml" in cd:
                    ext = ".apk"
                return Result(eocd + 22 + clen, True, ext)
            i = data.find(b"PK\x05\x06", i + 1)
        pos += chunk
    return Result(0, False, ".zip", "no central directory")
