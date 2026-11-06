"""Content checks for PDF, AVI/WAV/WebP, TIFF-based RAW and GIF.

A structure walk proves little about a deleted file: its clusters may since
hold another file's data. These checks look at the contents wherever the
format makes that possible:

- PDF: every object sits where the cross-reference table says, and every
  compressed stream decompresses with a matching checksum (zlib's Adler-32);
  an embedded JPEG is decoded.
- AVI: the chunks of the movie list chain from one to the next, every video
  frame starts (and a Motion-JPEG one also ends) the way its codec says, and
  the index points at real chunks.
- TIFF/RAW: JPEG and zlib-compressed images inside are checked. A camera's
  sensor data (lossless JPEG, or a maker's own compression) can't be.
- GIF: the compressed picture data decodes to exactly the right number of
  pixels.

What can't be checked (WAV audio, WebP, RAW sensor data, uncompressed or
encrypted PDF streams) is only called recovered for a file that was never
deleted, like every other type without checksums.
"""
from __future__ import annotations

import re
import struct
import zlib

from . import jpeg
from .others import measure_riff, measure_tiff

UNPROVEN = ("its structure checks out, but parts of it can't be checked here, so it can't prove all of "
            "its contents; check that it opens")
MAX_PDF = 256 << 20       # bytes of PDF read into memory to check
PDF_INFLATE = 1 << 30     # bytes of streams decompressed per file
MAX_FRAMES = 200_000      # chunks walked per AVI
AVI_DECODE = 64 << 20     # bytes of Motion-JPEG frames decoded per AVI (beyond: start and end markers only)
GIF_CODES = 64_000_000    # LZW codes decoded per GIF


def _verdict(bad, unproven: int, layout_known: bool, ok_note: str):
    if bad:
        return "corrupt", bad[1], bad[0]
    if not unproven:
        return "ok", ok_note, None
    if layout_known:
        return "ok", "structure checked", None
    return "unverified", UNPROVEN, None


# ---------------------------------------------------------------------- PDF
_OBJ = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj\b")
_STREAM = re.compile(rb"\bstream(\r\n|\n|\r)")
_LENGTH = re.compile(rb"/Length\s+(\d+)(?:\s+(\d+)\s+R)?")
_FILTER = re.compile(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9]+)")
_PREV = re.compile(rb"/Prev\s+(\d+)")
_INT = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj\s*(\d+)\s*endobj")


def _xref(data: bytes):
    """{object number: offset} from every cross-reference section (tables and
    streams, following /Prev), whether the file is encrypted, or None."""
    i = data.rfind(b"startxref", max(0, len(data) - 4096))
    m = re.match(rb"startxref\s+(\d+)", data[i:i + 40]) if i >= 0 else None
    if not m or int(m.group(1)) >= i:
        return None
    offs, todo, seen, encrypted = {}, [int(m.group(1))], set(), False
    while todo and len(seen) < 64:
        at = todo.pop()
        if at in seen or at >= len(data):
            continue
        seen.add(at)
        if data.startswith(b"xref", at):
            pos = at + 4
            while True:
                hm = re.match(rb"\s*(\d+)\s+(\d+)[ \t]*(\r\n|\n|\r)", data[pos:pos + 40])
                if not hm:
                    break
                first, count = int(hm.group(1)), int(hm.group(2))
                pos += hm.end()
                for k in range(count):
                    e = data[pos + 20 * k:pos + 20 * k + 20]
                    if len(e) < 18 or e[17:18] not in (b"n", b"f"):
                        return None
                    if e[17:18] == b"n":
                        offs.setdefault(first + k, int(e[:10]))
                pos += 20 * count
            t = data.find(b"trailer", pos, pos + 64)
            if t < 0:
                return None
            sx = data.find(b"startxref", t)
            trailer = data[t:sx if sx > 0 else t + 4096]
        else:
            om = _OBJ.match(data, at)
            if not om:
                return None
            got = _stream_at(data, at, om.end())
            if got is None or isinstance(got[0], int):
                return None
            dic, raw, _s = got
            if b"/XRef" not in dic:
                return None
            try:
                body = zlib.decompress(raw) if b"FlateDecode" in dic else raw
                w = [int(x) for x in re.search(rb"/W\s*\[\s*([\d\s]+)\]", dic).group(1).split()]
                size = int(re.search(rb"/Size\s+(\d+)", dic).group(1))
            except (zlib.error, AttributeError, ValueError):
                return None
            if len(w) != 3 or sum(w) > 32:
                return None
            im = re.search(rb"/Index\s*\[\s*([\d\s]+)\]", dic)
            index = [int(x) for x in im.group(1).split()] if im else [0, size]
            pm = re.search(rb"/Predictor\s+(\d+)", dic)
            row = sum(w)
            if pm and int(pm.group(1)) >= 10:  # PNG 'up' rows
                rows, prev = [], bytes(row)
                for r in range(0, len(body) - row, row + 1):
                    cur = bytes((body[r + 1 + k] + prev[k]) & 255 for k in range(row)) if body[r] == 2 \
                        else body[r + 1:r + 1 + row]
                    rows.append(cur)
                    prev = cur
                body = b"".join(rows)
            p = 0
            for start, count in zip(index[::2], index[1::2]):
                for k in range(count):
                    ent = body[p:p + row]
                    p += row
                    if len(ent) < row:
                        return None
                    f, q = [], 0
                    for width in w:
                        f.append(int.from_bytes(ent[q:q + width], "big") if width else (1 if not f else 0))
                        q += width
                    if f[0] == 1:
                        offs.setdefault(start + k, f[1])
            trailer = dic
        encrypted |= b"/Encrypt" in trailer
        pm = _PREV.search(trailer)
        if pm:
            todo.append(int(pm.group(1)))
    return offs, encrypted


def _stream_at(data: bytes, obj_at: int, body_at: int, limit: int | None = None):
    """(dictionary, stream bytes, where they start) of the object at obj_at, or None."""
    end = limit or len(data)
    sm = _STREAM.search(data, body_at, min(end, body_at + 65536))
    if not sm:
        return None
    dic = data[body_at:sm.start()]
    if b"endobj" in dic:
        return None  # the next object began: this one has no stream
    lm = _LENGTH.search(dic)
    if not lm:
        return None
    n = int(lm.group(1))
    if lm.group(2) is not None:
        return n, dic  # an indirect length: the caller resolves it
    s = sm.end()
    return dic, data[s:s + n], s


def validate_pdf(read, length: int, layout_known: bool = False, deep: bool = True):
    if length > MAX_PDF:
        return ("ok", "structure checked", None) if layout_known else ("unverified", UNPROVEN, None)
    data = read(0, length)
    xr = _xref(data)
    if xr is None:
        return ("ok", "structure checked", None) if layout_known else (
            "unverified", "its cross-reference table can't be read, so its contents can't be checked", None)
    offs, encrypted = xr
    if not offs:  # nothing listed: nothing to check against (an old file's leftover table, say)
        return ("ok", "structure checked", None) if layout_known else (
            "unverified", "its cross-reference table lists nothing here, so its contents can't be checked", None)
    bad, unproven, budget = None, 0, PDF_INFLATE
    order = sorted(set(offs.values()))
    nxt = {o: (order[i + 1] if i + 1 < len(order) else length) for i, o in enumerate(order)}
    for num, at in sorted(offs.items(), key=lambda kv: kv[1]):
        om = _OBJ.match(data, at) if at < length else None
        if not om or int(om.group(1)) != num:
            bad = (at, "object %d is not where the file's table says (byte %d)" % (num, at))
            break
        got = _stream_at(data, at, om.end(), nxt[at])
        if got is None:
            continue
        if isinstance(got[0], int):  # /Length n g R
            ref = re.search(rb"/Length\s+(\d+)\s+(\d+)\s+R", got[1])
            lo = offs.get(int(ref.group(1)))
            im = _INT.match(data, lo) if lo is not None else None
            if not im:
                unproven += 1
                continue
            sm = _STREAM.search(data, om.end())
            got = (got[1], data[sm.end():sm.end() + int(im.group(3))], sm.end())
        dic, raw, s = got
        tail = data[s + len(raw):s + len(raw) + 12].lstrip(b"\r\n")
        if not tail.startswith(b"endstream"):
            bad = (s + len(raw), "the data of object %d doesn't end where its length says" % num)
            break
        fm = _FILTER.search(dic)
        filters = re.findall(rb"/(\w+)", fm.group(1)) if fm else []
        if encrypted or not filters or filters[0] not in (b"FlateDecode", b"DCTDecode"):
            unproven += len(raw)
            continue
        if filters[0] == b"FlateDecode":
            d = zlib.decompressobj()
            try:
                out = d.decompress(raw, budget)
            except zlib.error:
                bad = (s, "compressed data of object %d is damaged (byte %d)" % (num, s))
                break
            budget -= len(out)
            if budget <= 0:
                unproven += len(raw)
                continue
            if not d.eof:
                bad = (s, "compressed data of object %d is cut short (byte %d)" % (num, s))
                break
            continue  # (a compressed picture too: the checksum covers its bytes)
        st = (jpeg.check if deep else jpeg.check_fast)(raw)
        if st[0] != "ok":
            bad = (s, "the picture in object %d is damaged (byte %d)" % (num, s))
            break
        if st[1].startswith("structure"):
            unproven += len(raw)  # progressive or unusual: only its markers were checked
    return _verdict(bad, unproven, layout_known, "every object and stream checked")


# ---------------------------------------------------------------------- RIFF
_CHUNK = re.compile(rb"\d\d(dc|db|wb|pc|tx)|ix\d\d|JUNK|LIST|idx1")
_ANNEX_B = (b"H264", b"h264", b"X264", b"x264", b"AVC1", b"avc1", b"DAVC", b"VSSH")
_MPEG4 = (b"XVID", b"xvid", b"DIVX", b"divx", b"DX50", b"dx50", b"FMP4", b"fmp4", b"MP4V", b"mp4v", b"3IV2", b"M4S2")
_MJPEG = (b"MJPG", b"mjpg", b"AVRn", b"jpeg", b"JPEG", b"dmb1", b"AVDJ")


def validate_riff(read, length: int, layout_known: bool = False, deep: bool = True):
    m = measure_riff(read, 0, length)
    if not m.complete or m.end != length:
        return "partial", m.reason if not m.complete else "the file is longer than its header says", m.break_pos
    if read(8, 4) != b"AVI ":
        # WAV audio, WebP pictures: nothing in them can be checked against their bytes
        return ("ok", "structure checked", None) if layout_known else ("unverified", UNPROVEN, None)
    return _validate_avi(read, length, layout_known, deep)


def _avi_streams(read, length: int):
    """{stream number: codec fourcc} for the video streams, and where 'movi' and 'idx1' are."""
    codecs, movi, idx1, pos, n = {}, None, None, 12, 0
    while pos + 8 <= length:
        h = read(pos, 12)
        size = struct.unpack("<I", h[4:8])[0]
        if h[:4] == b"LIST" and h[8:12] == b"hdrl":
            p, end = pos + 12, pos + 8 + size
            while p + 8 <= end:
                c = read(p, 12)
                cs = struct.unpack("<I", c[4:8])[0]
                if c[:4] == b"LIST" and c[8:12] == b"strl":
                    strh = read(p + 12, 64)
                    if strh[:4] == b"strh" and strh[8:12] == b"vids":
                        codecs[n] = strh[12:16]
                        strf = read(p + 12 + 8 + struct.unpack("<I", strh[4:8])[0], 28)
                        if strf[:4] == b"strf" and strf[24:28].strip(b"\0"):
                            codecs[n] = strf[24:28]  # biCompression
                    n += 1
                p += 8 + cs + (cs & 1)
        elif h[:4] == b"LIST" and h[8:12] == b"movi":
            movi = (pos + 8, pos + 8 + size)  # from the 'movi' fourcc
        elif h[:4] == b"idx1":
            idx1 = (pos + 8, pos + 8 + size)
        pos += 8 + size + (size & 1)
    return codecs, movi, idx1


def _validate_avi(read, length: int, layout_known: bool, deep: bool = True):
    codecs, movi, idx1 = _avi_streams(read, length)
    if movi is None:
        return "partial", "it has no movie data", None
    checked = unproven = decoded = 0
    budget = AVI_DECODE if deep else 0
    chunks = {}
    pos, end, depth_end = movi[0] + 4, movi[1], []
    while pos + 8 <= end and len(chunks) < MAX_FRAMES:
        h = read(pos, 8)
        size = struct.unpack("<I", h[4:8])[0]
        if not _CHUNK.fullmatch(h[:4]) or pos + 8 + size > end:
            return "corrupt", "the movie data breaks at byte %d" % pos, pos
        chunks[pos] = (h[:4], size)
        if h[:4] == b"LIST":
            pos += 12  # 'rec ' groups: walk into them
            continue
        kind = h[2:4]
        if kind in (b"dc", b"db") and size:
            codec = codecs.get(int(h[:2]))
            data = read(pos + 8, 4)
            if codec in _MJPEG or (codec not in _ANNEX_B and codec not in _MPEG4 and data[:3] == b"\xff\xd8\xff"):
                # (a frame that starts like a JPEG is one, whatever codec name the header gives)
                tail = read(pos + 8 + max(0, size - 64), min(size, 64)).rstrip(b"\0")
                if data[:3] != b"\xff\xd8\xff" or not tail.endswith(b"\xff\xd9"):
                    return "corrupt", "video frame at byte %d isn't a whole picture" % pos, pos
                if budget >= size:
                    # every frame is a JPEG: decoding it catches a cluster of another
                    # video inside it, which its start and end markers can't
                    budget -= size
                    if jpeg.check(read(pos + 8, size))[0] != "ok":
                        return "corrupt", "video frame at byte %d doesn't decode" % pos, pos
                    decoded += 1
                checked += 1
            elif codec in _ANNEX_B or codec in _MPEG4:
                if not (data[:3] == b"\x00\x00\x01" or data == b"\x00\x00\x00\x01"):
                    return "corrupt", "video frame at byte %d doesn't start like one" % pos, pos
                checked += 1
            else:
                unproven += 1
        pos += 8 + size + (size & 1)
    if idx1:
        raw = read(idx1[0], min(idx1[1] - idx1[0], 16 * MAX_FRAMES))
        base = None
        for k in range(0, len(raw) - 15, 16):
            ck, _flags, off, size = struct.unpack("<4sIII", raw[k:k + 16])
            if base is None:  # offsets count from the 'movi' fourcc (or just after it), or from the file's start
                base = next((b for b in (movi[0], movi[0] + 4, 0) if b + off in chunks), 0)
            got = chunks.get(base + off)
            if got and got != (ck, size):
                return "corrupt", "the index doesn't match the movie data at byte %d" % (base + off), base + off
    if checked and not unproven:
        if decoded:
            return "ok", "%d of %d frames decoded, the rest checked at both ends" % (decoded, checked) \
                if decoded < checked else "every frame decoded (%d)" % decoded, None
        return "ok", "%d frames checked" % checked, None
    return ("ok", "structure checked", None) if layout_known else ("unverified", UNPROVEN, None)


# ---------------------------------------------------------------------- TIFF
def _ifd_images(read, length: int):
    """(compression, [(offset, length)]) for every image a TIFF's IFDs point to."""
    head = read(0, 16)
    bo = "<" if head[:2] == b"II" else ">"
    todo, seen, out = [struct.unpack(bo + "I", head[4:8])[0]], set(), []
    while todo and len(seen) < 64:
        ifd = todo.pop()
        if ifd in seen or ifd < 8 or ifd + 2 > length:
            continue
        seen.add(ifd)
        n = struct.unpack(bo + "H", read(ifd, 2))[0]
        if not 0 < n <= 1000:
            continue
        raw = read(ifd + 2, 12 * n + 4)
        if len(raw) < 12 * n + 4:
            continue
        tags = {}
        for i in range(n):
            tag, typ, count, val = struct.unpack(bo + "HHII", raw[12 * i:12 * i + 12])
            if typ == 3 and count == 1:
                val = struct.unpack(bo + "H", raw[12 * i + 8:12 * i + 10])[0]
            tags[tag] = (typ, count, val)
            if tag in (0x8769, 0x014A) and typ in (4, 13):
                todo.extend([val] if count == 1 else _vals(read, bo, typ, count, val))
        nxt = struct.unpack(bo + "I", raw[12 * n:12 * n + 4])[0]
        if nxt:
            todo.append(nxt)
        comp = tags.get(0x103, (0, 0, 1))[2]
        for off_tag, len_tag in ((0x111, 0x117), (0x144, 0x145), (0x201, 0x202)):
            if off_tag in tags and len_tag in tags:
                offs = _vals(read, bo, *tags[off_tag])
                lens = _vals(read, bo, *tags[len_tag])
                c = 6 if off_tag == 0x201 else comp
                out.append((c, [(o, n) for o, n in zip(offs, lens) if o + n <= length]))
    return out


def _vals(read, bo, typ, count, val):
    if count == 1:
        return [val]
    fmt = {3: "H", 4: "I", 13: "I"}.get(typ)
    if not fmt or count > 100000:
        return []
    size = struct.calcsize(fmt)
    raw = read(val, size * count)
    return list(struct.unpack(bo + "%d%s" % (len(raw) // size, fmt), raw[:len(raw) // size * size]))


def validate_tiff(read, length: int, layout_known: bool = False, deep: bool = True):
    m = measure_tiff(read, 0, length)
    if not m.complete or m.end > length:
        return "partial", m.reason, m.break_pos
    unproven, checked = 0, 0
    for comp, blocks in _ifd_images(read, length):
        for off, n in blocks:
            if not n:
                continue
            data = read(off, n)
            if comp in (6, 7) and data[:2] == b"\xff\xd8":
                h = None
                try:
                    h = jpeg.parse_header(lambda o, k: data[o:o + k], 0)
                except jpeg.JpegError:
                    pass
                if h is None or not h.decodable:
                    unproven += n  # lossless (a camera's sensor data) or unusual: can't be decoded here
                    continue
                st = (jpeg.check if deep else jpeg.check_fast)(data)
                if st[0] != "ok":
                    return "corrupt", "the picture at byte %d inside it is damaged" % off, off
                checked += 1
            elif comp in (8, 32946):
                try:
                    zlib.decompress(data)
                except zlib.error:
                    return "corrupt", "compressed picture data at byte %d is damaged" % off, off
                checked += 1
            else:
                unproven += n
    if checked and not unproven:
        return "ok", "every picture in it checked", None
    if layout_known:
        return "ok", "structure checked", None
    return "unverified", ("its previews check out, but a camera's sensor data can't be checked here, so it can't "
                          "prove all of its contents; check that it opens") if checked else UNPROVEN, None


# ---------------------------------------------------------------------- GIF
def _lzw_pixels(min_size: int, data: bytes, budget: int):
    """How many pixels GIF LZW data decodes to; None if it's not valid LZW."""
    if not 2 <= min_size <= 8:
        return None
    clear, eoi = 1 << min_size, (1 << min_size) + 1
    lens = [1] * clear + [0] * (4096 - clear)
    size, nxt, prev, count = min_size + 1, eoi + 1, -1, 0
    acc = nacc = i = 0
    n = len(data)
    mask = (1 << size) - 1
    while budget > 0:
        while nacc < size:
            if i >= n:
                return count  # data ended without the end code: most decoders accept that
            acc |= data[i] << nacc
            nacc += 8
            i += 1
        code = acc & mask
        acc >>= size
        nacc -= size
        budget -= 1
        if code == clear:
            size, nxt, prev = min_size + 1, eoi + 1, -1
            mask = (1 << size) - 1
            continue
        if code == eoi:
            return count
        if prev < 0:
            if code >= clear:
                return None
            count += 1
            prev = code
            continue
        if code < nxt:
            ln = lens[code]
        elif code == nxt:
            ln = lens[prev] + 1
        else:
            return None
        count += ln
        if nxt < 4096:
            lens[nxt] = lens[prev] + 1
            nxt += 1
            if nxt == 1 << size and size < 12:
                size += 1
                mask = (1 << size) - 1
        prev = code
    return count


def validate_gif(read, length: int, layout_known: bool = False, deep: bool = True):
    data = read(0, length)
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return "corrupt", "unreadable header", 0
    flags = data[10]
    pos = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    frames, budget = 0, GIF_CODES
    while pos < len(data):
        b = data[pos]
        if b == 0x3B:
            if pos + 1 != length:
                return "partial", "the picture ends before the file does", pos + 1
            return "ok", "every frame decoded (%d)" % frames, None
        if b == 0x21:
            pos += 2
            while pos < len(data) and data[pos]:
                pos += 1 + data[pos]
            pos += 1
            continue
        if b != 0x2C or pos + 11 > len(data):
            return "corrupt", "broken at byte %d" % pos, pos
        w, h = struct.unpack("<HH", data[pos + 5:pos + 9])
        lf = data[pos + 9]
        at = pos
        pos += 10 + (3 * (2 << (lf & 7)) if lf & 0x80 else 0)
        min_size = data[pos] if pos < len(data) else 0
        pos += 1
        parts = []
        while pos < len(data) and data[pos]:
            parts.append(data[pos + 1:pos + 1 + data[pos]])
            pos += 1 + data[pos]
        pos += 1
        got = _lzw_pixels(min_size, b"".join(parts), budget)
        budget -= w * h
        if got is None or got != w * h:
            return "corrupt", "frame %d (byte %d) doesn't decode to a whole picture" % (frames + 1, at), at
        frames += 1
    return "partial", "the file stops partway through", pos
