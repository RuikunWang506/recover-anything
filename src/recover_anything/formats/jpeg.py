"""JPEG: header parsing, exact-length measurement, and a baseline Huffman decoder.

The decoder never produces pixels. It walks the entropy-coded data exactly the
way a real decoder must, which is enough to tell whether a sequence of bytes is
one coherent image: wrong data breaks Huffman codes, overflows coefficient
indexes, breaks restart-marker order, or ends with the wrong number of MCUs.
That is what makes fragment reassembly possible.
"""
from __future__ import annotations

import re
import struct
from array import array

SOF_BASELINE = {0xC0, 0xC1}
SOF_ALL = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
MAX_JPEG = 200 * 1024 * 1024
_MARKER_RE = re.compile(rb"\xff[^\x00]")
_ZERO_RUN = bytes(4096)


_FIRST_MARKERS = set(range(0xE0, 0xF0)) | {0xFE, 0xDB, 0xC0, 0xC1, 0xC2, 0xC4, 0xDD}


def probe(head: bytes) -> bool:
    return len(head) >= 4 and head[0] == 0xFF and head[1] == 0xD8 and head[2] == 0xFF and head[3] in _FIRST_MARKERS


class JpegError(Exception):
    pass


class Header:
    """Everything before the first entropy-coded segment."""

    def __init__(self):
        self.sof = None
        self.precision = 8
        self.width = 0
        self.height = 0
        self.components: list[tuple[int, int, int, int]] = []  # (id, h, v, tq)
        self.huff: dict[tuple[int, int], tuple[list[int], list[int]]] = {}  # (class, id) -> (counts, syms)
        self.restart = 0
        self.scan: list[tuple[int, int, int]] = []  # (comp id, dc table, ac table)
        self.entropy_start = 0  # offset (from SOI) of the first entropy byte
        self.mpf_end = None  # end offset implied by an MPF index (multi-picture files)
        self.mpf_starts: tuple = ()  # where the MPF index says the other pictures start (from SOI)
        self.main_end = None  # end of the main picture (its first EOI), set by measure()
        self.has_exif = False
        self.q0: dict[int, int] = {}  # quantization table id -> its DC step
        self.qt: dict[int, list] = {}  # quantization table id -> its 64 steps, in zigzag order
        self.thumb = None  # (offset from SOI, length) of the EXIF thumbnail

    @property
    def decodable(self) -> bool:
        return (self.sof in SOF_BASELINE and self.precision == 8 and self.width > 0 and self.height > 0
                and len(self.scan) == len(self.components) and len(self.components) in (1, 3, 4))


def _parse_mpf(seg: bytes, seg_tiff_pos: int):
    """APP2 'MPF\\0' index -> (end of the last picture, starts of the other
    pictures), both relative to SOI; (None, ()) if the index is unreadable."""
    tiff = seg[4:]
    if len(tiff) < 8:
        return None, ()
    bo = "<" if tiff[:2] == b"II" else ">" if tiff[:2] == b"MM" else None
    if bo is None:
        return None, ()
    ifd = struct.unpack(bo + "I", tiff[4:8])[0]
    if ifd + 2 > len(tiff):
        return None, ()
    n = struct.unpack(bo + "H", tiff[ifd:ifd + 2])[0]
    for i in range(n):
        e = ifd + 2 + 12 * i
        if e + 12 > len(tiff):
            return None, ()
        tag, typ, count, val = struct.unpack(bo + "HHII", tiff[e:e + 12])
        if tag == 0xB002 and count >= 16:
            end, starts = 0, []
            for j in range(count // 16):
                p = val + 16 * j
                if p + 16 > len(tiff):
                    return None, ()
                _attr, size, off = struct.unpack(bo + "III", tiff[p:p + 12])
                base = 0 if off == 0 else seg_tiff_pos
                end = max(end, base + off + size)
                if off:
                    starts.append(base + off)
            return (end or None), tuple(starts)
    return None, ()


def _exif_thumb(tiff: bytes, tiff_pos: int):
    """(offset from SOI, length) of the thumbnail an EXIF block points to
    (IFD1's JPEGInterchangeFormat), or None."""
    try:
        bo = {b"II": "<", b"MM": ">"}[tiff[:2]]
        ifd0 = struct.unpack(bo + "I", tiff[4:8])[0]
        n = struct.unpack(bo + "H", tiff[ifd0:ifd0 + 2])[0]
        nxt = ifd0 + 2 + 12 * n
        ifd1 = struct.unpack(bo + "I", tiff[nxt:nxt + 4])[0]
        if not ifd1:
            return None
        n = struct.unpack(bo + "H", tiff[ifd1:ifd1 + 2])[0]
        tags = {}
        for i in range(min(n, 64)):
            e = ifd1 + 2 + 12 * i
            tag, typ = struct.unpack(bo + "HH", tiff[e:e + 4])
            tags[tag] = struct.unpack(bo + ("I" if typ == 4 else "H"), tiff[e + 8:e + (12 if typ == 4 else 10)])[0]
        off, size = tags.get(0x0201), tags.get(0x0202)
        if off is None or not size or off + size > len(tiff) or tiff[off:off + 2] != b"\xff\xd8":
            return None
        return tiff_pos + off, size
    except (KeyError, struct.error):
        return None


def parse_header(read, start: int, limit: int = 4 * 1024 * 1024) -> Header:
    """Parse markers from SOI to the first SOS. `read(off, n)` reads the medium."""
    h = Header()
    buf = read(start, min(limit, 256 * 1024))
    if len(buf) < 4 or buf[:2] != b"\xff\xd8":
        raise JpegError("no SOI")
    pos = 2

    def need(n):
        nonlocal buf
        while pos + n > len(buf):
            more = read(start + len(buf), 256 * 1024)
            if not more or len(buf) > limit:
                raise JpegError("header runs off the end")
            buf += more

    while True:
        need(4)
        if buf[pos] != 0xFF:
            raise JpegError("bad marker at %d" % pos)
        m = buf[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        if m == 0xD9:
            raise JpegError("EOI before SOS")
        seglen = struct.unpack(">H", buf[pos + 2:pos + 4])[0]
        if seglen < 2:
            raise JpegError("bad segment length")
        need(2 + seglen)
        seg = buf[pos + 4:pos + 2 + seglen]
        if m == 0xE1 and seg[:6] == b"Exif\x00\x00":
            if not h.has_exif:
                h.thumb = _exif_thumb(seg[6:], pos + 10)
            h.has_exif = True
        elif m == 0xDB:
            p = 0
            while p + 65 <= len(seg):
                pq, tq = seg[p] >> 4, seg[p] & 15
                h.q0[tq] = seg[p + 1] if pq == 0 else struct.unpack(">H", seg[p + 1:p + 3])[0]
                h.qt[tq] = list(seg[p + 1:p + 65]) if pq == 0 else list(struct.unpack(">64H", seg[p + 1:p + 129]))
                p += 65 if pq == 0 else 129
        elif m == 0xE2 and seg[:4] == b"MPF\x00":
            h.mpf_end, h.mpf_starts = _parse_mpf(seg, pos + 8)
        elif m in SOF_ALL:
            if len(seg) < 6:
                raise JpegError("short SOF")
            h.sof = m
            h.precision, h.height, h.width, nc = struct.unpack(">BHHB", seg[:6])
            if len(seg) < 6 + 3 * nc:
                raise JpegError("short SOF")
            for i in range(nc):
                cid, hv, tq = seg[6 + 3 * i:9 + 3 * i]
                hh, vv = hv >> 4, hv & 15
                if not (1 <= hh <= 4 and 1 <= vv <= 4):
                    raise JpegError("bad sampling")
                h.components.append((cid, hh, vv, tq))
        elif m == 0xC4:
            p = 0
            while p < len(seg):
                if p + 17 > len(seg):
                    raise JpegError("short DHT")
                tc_th = seg[p]
                counts = list(seg[p + 1:p + 17])
                total = sum(counts)
                syms = list(seg[p + 17:p + 17 + total])
                if len(syms) != total:
                    raise JpegError("short DHT")
                h.huff[(tc_th >> 4, tc_th & 15)] = (counts, syms)
                p += 17 + total
        elif m == 0xDD:
            if len(seg) >= 2:
                h.restart = struct.unpack(">H", seg[:2])[0]
        elif m == 0xDA:
            ns = seg[0] if seg else 0
            if len(seg) < 1 + 2 * ns:
                raise JpegError("short SOS")
            for i in range(ns):
                cid, t = seg[1 + 2 * i:3 + 2 * i]
                h.scan.append((cid, t >> 4, t & 15))
            h.entropy_start = pos + 2 + seglen
            if h.sof is None:
                raise JpegError("SOS before SOF")
            return h
        pos += 2 + seglen
        if pos > limit:
            raise JpegError("header too long")


class Measure:
    __slots__ = ("end", "complete", "reason", "break_pos", "header")

    def __init__(self, end, complete, reason, break_pos=None, header=None):
        self.end, self.complete, self.reason, self.break_pos, self.header = end, complete, reason, break_pos, header


def measure(read, start: int, limit: int = MAX_JPEG, stop_at=None) -> Measure:
    """Find where a JPEG that starts at `start` ends, assuming it is contiguous.

    Returns end (exclusive, relative to start), whether it looked complete, and
    if not, the first byte where the data stopped making sense (`break_pos`).
    `stop_at(rel_offset)` may return True to mark a boundary the file cannot
    cross (for example, the next cluster that starts another file).
    """
    try:
        h = parse_header(read, start)
    except JpegError as e:
        return Measure(0, False, "header: %s" % e)
    pos = h.entropy_start
    expect_rst = 0
    chunk = 1 << 18
    carry = b""
    carry_pos = pos
    scans = 1
    while pos < limit:
        data = carry + read(start + carry_pos + len(carry), chunk)
        base = carry_pos
        if len(data) <= len(carry):
            return Measure(pos, False, "ran out of data", pos, h)
        i = 0
        zero = data.find(_ZERO_RUN)
        while True:
            mo = _MARKER_RE.search(data, i)
            if zero >= 0 and (not mo or mo.start() > zero):
                return Measure(base + zero, False, "ran into empty space", base + zero, h)
            if not mo:
                break
            j = mo.start()
            if j + 1 >= len(data):
                break
            m = data[j + 1]
            absj = base + j
            if 0xD0 <= m <= 0xD7:
                if h.restart and (m - 0xD0) != expect_rst:
                    return Measure(absj, False, "restart marker out of order", absj, h)
                expect_rst = (expect_rst + 1) & 7
                i = j + 2
                continue
            if m == 0xFF:
                i = j + 1
                continue
            if m == 0xD9:
                end = absj + 2
                h.main_end = end
                if h.mpf_end and h.mpf_end > end and h.mpf_end - end < 64 * 1024 * 1024:
                    tail = read(start + end, 4)
                    if tail[:3] == b"\xff\xd8\xff":
                        end = h.mpf_end
                return Measure(end, True, "ok", None, h)
            if h.sof in (0xC2, 0xC6, 0xCA, 0xCE) or m in (0xC4, 0xDD) or (m == 0xDA and scans > 0):
                # progressive / multi-scan: tables and new scans may sit between segments
                if m in (0xC4, 0xDA, 0xDB, 0xDD, 0xFE) or 0xE0 <= m <= 0xEF:
                    if j + 4 > len(data):
                        carry, carry_pos = data[j:], base + j
                        pos = carry_pos
                        data = None
                        break
                    seglen = struct.unpack(">H", data[j + 2:j + 4])[0]
                    if seglen < 2:
                        return Measure(absj, False, "bad segment", absj, h)
                    if m == 0xDA:
                        scans += 1
                        expect_rst = 0
                    i = j + 2 + seglen
                    if i > len(data):
                        # segment straddles the chunk; restart scanning after it
                        nxt = base + i
                        carry, carry_pos = b"", nxt
                        pos = nxt
                        data = None
                        break
                    continue
            return Measure(absj, False, "unexpected marker 0x%02X" % m, absj, h)
        if data is None:
            continue
        if stop_at is not None and stop_at(base + len(data)):
            return Measure(base + len(data), False, "ran into another file", base + len(data), h)
        keep = 1 if data[-1:] == b"\xff" else 0
        carry = data[len(data) - keep:] if keep else b""
        carry_pos = base + len(data) - keep
        pos = carry_pos
    return Measure(pos, False, "too long", pos, h)


# --------------------------------------------------------------------- decoder
class _Huff:
    __slots__ = ("lookup",)

    def __init__(self, counts, syms):
        lookup = [0] * 65536
        code, k = 0, 0
        for length in range(1, 17):
            for _ in range(counts[length - 1]):
                if code >= (1 << length):
                    raise JpegError("bad huffman table")
                shift = 16 - length
                lo, hi = code << shift, (code + 1) << shift
                lookup[lo:hi] = [(length << 8) | syms[k]] * (hi - lo)
                code += 1
                k += 1
            code <<= 1
        self.lookup = lookup


_HUFF_CACHE: dict = {}


def _huff(counts, syms) -> _Huff:
    """A table's lookup, built once: a camera uses the same few tables for
    every photo and every video frame, and a rebuild makes many decoders."""
    key = (tuple(counts), tuple(syms))
    t = _HUFF_CACHE.get(key)
    if t is None:
        if len(_HUFF_CACHE) >= 32:
            _HUFF_CACHE.clear()  # (each table is 512 KB: keep only a few)
        t = _HUFF_CACHE[key] = _Huff(counts, syms)
    return t


# Standard tables (ITU T.81 Annex K) for "motion JPEG" files that omit DHT.
_STD = {
    (0, 0): ([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0], list(range(12))),
    (0, 1): ([0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0], list(range(12))),
}


class DecodeError(Exception):
    def __init__(self, msg, pos):
        super().__init__(msg)
        self.pos = pos


class Decoder:
    """Resumable baseline decoder over a growing byte buffer.

    `buf` holds the (virtual) file from SOI. `run(limit)` decodes whole MCUs
    and returns 'done' (all MCUs decoded and EOI found), 'more' (ran out of
    buffer at an MCU boundary) or raises DecodeError with the byte position.
    """

    def __init__(self, h: Header):
        if not h.decodable:
            raise JpegError("not a baseline sequential JPEG")
        self.h = h
        comps = {c[0]: c for c in h.components}
        hmax = max(c[1] for c in h.components)
        vmax = max(c[2] for c in h.components)
        tables = {}
        for k, v in h.huff.items():
            tables[k] = _huff(*v)
        for k, v in _STD.items():
            if k not in tables:
                tables[k] = _huff(*v)
        self.units = []  # per scan component: (dc lookup, ac lookup, h, v, is_luma)
        try:
            for i, (cid, td, ta) in enumerate(h.scan):
                c = comps[cid]
                if (0, td) not in tables or (1, ta) not in tables:
                    raise JpegError("missing huffman table")
                self.units.append((tables[(0, td)].lookup, tables[(1, ta)].lookup, c[1], c[2], i == 0))
        except KeyError:
            raise JpegError("scan references unknown component")
        if len(h.components) == 1:
            self.units = [(self.units[0][0], self.units[0][1], 1, 1, True)]
            self.mcu_w = (h.width + 7) // 8
            self.mcu_h = (h.height + 7) // 8
            self.bh, self.bv = 1, 1
        else:
            self.mcu_w = (h.width + 8 * hmax - 1) // (8 * hmax)
            self.mcu_h = (h.height + 8 * vmax - 1) // (8 * vmax)
            self.bh, self.bv = self.units[0][2], self.units[0][3]
        self.total = self.mcu_w * self.mcu_h
        self.bw = self.mcu_w * self.bh  # luma blocks per row
        if self.total > 50_000_000:
            raise JpegError("image too large to decode")
        self.dc = array("q", bytes(8 * self.bw * self.mcu_h * self.bv))
        self.buf = b""
        self.reset()

    def reset(self):
        self.pos = self.h.entropy_start
        self.acc = 0
        self.bits = 0
        self.marker = -1
        self.marker_pos = -1
        self.mcu = 0
        self.preds = [0] * len(self.units)
        self.next_rst = 0
        self.end = None
        self._rst_done_for = -1

    def save(self):
        return (self.pos, self.acc, self.bits, self.marker, self.marker_pos, self.mcu, tuple(self.preds),
                self.next_rst, self.end, self._rst_done_for)

    def restore(self, s):
        (self.pos, self.acc, self.bits, self.marker, self.marker_pos, self.mcu, preds, self.next_rst, self.end,
         self._rst_done_for) = s
        self.preds = list(preds)

    # -- bit reader --------------------------------------------------------
    def _fill(self) -> bool:
        """Top up the bit accumulator. Returns False at end of buffer."""
        buf, pos = self.buf, self.pos
        n = len(buf)
        acc, bits = self.acc & ((1 << self.bits) - 1), self.bits
        while bits <= 32 and self.marker < 0:
            if pos + 4 <= n and buf.find(b"\xff", pos, pos + 4) < 0:
                acc = (acc << 32) | int.from_bytes(buf[pos:pos + 4], "big")
                bits += 32
                pos += 4
                continue
            if pos >= n:
                self.acc, self.bits, self.pos = acc, bits, pos
                return False
            b = buf[pos]
            if b != 0xFF:
                acc = (acc << 8) | b
                bits += 8
                pos += 1
                continue
            if pos + 1 >= n:
                self.acc, self.bits, self.pos = acc, bits, pos
                return False
            nb = buf[pos + 1]
            if nb == 0:
                acc = (acc << 8) | 0xFF
                bits += 8
                pos += 2
            elif nb == 0xFF:
                pos += 1
            else:
                self.marker, self.marker_pos = nb, pos
                break
        self.acc, self.bits, self.pos = acc, bits, pos
        return True

    def run(self, final: bool = False) -> str:
        """Decode as far as the buffer allows. `final`: no more data will come."""
        units = self.units
        mcu_w, bh, bv, bw = self.mcu_w, self.bh, self.bv, self.bw
        dcarr = self.dc
        restart = self.h.restart
        total = self.total
        while self.mcu < total:
            if restart and self.mcu and self.mcu % restart == 0 and self._expect_rst_needed():
                st = self._restart()
                if st != "ok":
                    if st == "more" and not final:
                        return "more"
                    raise DecodeError("missing restart marker", self.pos)
            checkpoint = self.save()
            try:
                self._mcu(units, mcu_w, bh, bv, bw, dcarr)
            except _NeedMore:
                if final:
                    raise DecodeError("data ended mid-image", self.pos)
                self.restore(checkpoint)
                return "more"
            self.mcu += 1
        # all MCUs decoded: the next thing must be EOI (after <8 padding bits)
        if self.marker < 0:
            self._fill()
            if self.marker < 0:
                if self.pos >= len(self.buf) and not final:
                    return "more"
                raise DecodeError("image data continues after the last block", self.pos)
        if self.bits >= 8 and (self.acc >> (self.bits - 8)) & 0xFF != 0xFF:
            raise DecodeError("leftover bits after the last block", self.marker_pos)
        if self.marker != 0xD9:
            raise DecodeError("expected end of image, found marker 0x%02X" % self.marker, self.marker_pos)
        self.end = self.marker_pos + 2
        return "done"

    def _expect_rst_needed(self):
        return self._rst_done_for != self.mcu

    def _restart(self) -> str:
        if self.marker < 0:
            if not self._fill() and self.marker < 0:
                return "more"
        if self.marker < 0:
            # more entropy data where a restart marker must be
            raise DecodeError("restart marker missing", self.pos)
        if self.marker != 0xD0 + self.next_rst:
            raise DecodeError("restart marker out of order", self.marker_pos)
        self.pos = self.marker_pos + 2
        self.marker, self.marker_pos = -1, -1
        self.acc, self.bits = 0, 0
        self.preds = [0] * len(self.units)
        self.next_rst = (self.next_rst + 1) & 7
        self._rst_done_for = self.mcu
        return "ok"

    def _mcu(self, units, mcu_w, bh, bv, bw, dcarr):
        mx, my = self.mcu % mcu_w, self.mcu // mcu_w
        acc, bits = self.acc, self.bits
        for ui, (dct, act, hh, vv, luma) in enumerate(units):
            for v in range(vv):
                for hz in range(hh):
                    # ---- DC
                    if bits < 16:
                        self.acc, self.bits = acc, bits
                        self._fill()
                        acc, bits = self.acc, self.bits
                    if bits >= 16:
                        peek = (acc >> (bits - 16)) & 0xFFFF
                    else:
                        peek = ((acc << (16 - bits)) | ((1 << (16 - bits)) - 1)) & 0xFFFF
                    e = dct[peek]
                    ln = e >> 8
                    if not e or ln > bits:
                        self._fail(e, ln, bits, "bad DC code")
                    bits -= ln
                    t = e & 0xFF
                    diff = 0
                    if t:
                        if t > 11:
                            raise DecodeError("DC magnitude too large", self.pos)
                        if bits < t:
                            self.acc, self.bits = acc, bits
                            self._fill()
                            acc, bits = self.acc, self.bits
                            if bits < t:
                                self._fail(1, t, bits, "short DC")
                        diff = (acc >> (bits - t)) & ((1 << t) - 1)
                        bits -= t
                        if diff < (1 << (t - 1)):
                            diff -= (1 << t) - 1
                    # ---- AC
                    k = 1
                    while k < 64:
                        if bits < 16:
                            self.acc, self.bits = acc, bits
                            self._fill()
                            acc, bits = self.acc, self.bits
                        if bits >= 16:
                            peek = (acc >> (bits - 16)) & 0xFFFF
                        else:
                            peek = ((acc << (16 - bits)) | ((1 << (16 - bits)) - 1)) & 0xFFFF
                        e = act[peek]
                        ln = e >> 8
                        if not e or ln > bits:
                            self._fail(e, ln, bits, "bad AC code")
                        bits -= ln
                        rs = e & 0xFF
                        s = rs & 15
                        if s == 0:
                            if rs == 0xF0:
                                k += 16
                                continue
                            if rs:
                                raise DecodeError("invalid AC symbol", self.pos)
                            break
                        k += rs >> 4
                        if k > 63 or s > 10:
                            raise DecodeError("coefficient overflow", self.pos)
                        if bits < s:
                            self.acc, self.bits = acc, bits
                            self._fill()
                            acc, bits = self.acc, self.bits
                            if bits < s:
                                self._fail(1, s, bits, "short AC")
                        bits -= s
                        k += 1
                    if k > 64:
                        raise DecodeError("run past end of block", self.pos)
                    p = self.preds[ui] + diff
                    self.preds[ui] = p
                    if luma:
                        dcarr[(my * bv + v) * bw + mx * bh + hz] = p
        self.acc, self.bits = acc, bits

    def _fail(self, e, ln, bits, what):
        # Not enough bits: either the buffer ended (need more data) or a marker
        # interrupted the stream (corruption).
        if self.marker < 0 and self.pos >= len(self.buf) - 1:
            raise _NeedMore()
        if not e:
            raise DecodeError(what, self.pos)
        raise DecodeError("marker inside image data", self.marker_pos if self.marker_pos >= 0 else self.pos)

    # -- helpers for reassembly scoring --------------------------------------
    def block_index(self, mcu: int) -> int:
        return (mcu // self.mcu_w) * self.bv * self.bw + (mcu % self.mcu_w) * self.bh

    def dc_jump(self, mcu_from: int, mcu_to: int) -> float:
        """Mean |DC - DC of block above| for luma blocks in MCUs [from, to)."""
        bw = self.bw
        if mcu_to <= mcu_from:
            return 0.0
        dc = self.dc
        tot, n = 0, 0
        for m in range(mcu_from, mcu_to):
            my, mx = m // self.mcu_w, m % self.mcu_w
            if my == 0:
                continue
            for v in range(self.bv):
                for hz in range(self.bh):
                    i = (my * self.bv + v) * bw + mx * self.bh + hz
                    tot += abs(dc[i] - dc[i - bw])
                    n += 1
        return tot / n if n else 0.0


class _NeedMore(Exception):
    pass


ZIGZAG = (0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14,
          21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60,
          61, 54, 47, 55, 62, 63)
_COS = [[(0.5 ** 0.5 if u == 0 else 1.0) / 2 * __import__("math").cos((2 * x + 1) * u * __import__("math").pi / 16)
         for u in range(8)] for x in range(8)]


def luma_pixels(data: bytes, max_pixels: int = 1 << 20):
    """(width, height, brightness of every pixel, row by row) of a small
    baseline JPEG, such as a thumbnail. Simple rather than fast."""
    h = parse_header(lambda o, n: data[o:o + n], 0, len(data))
    if not h.decodable or h.width * h.height > max_pixels:
        raise JpegError("not a small baseline JPEG")
    q = h.qt.get(h.components[0][3])
    if not q:
        raise JpegError("no quantization table")
    tables = {k: _huff(*v) for k, v in h.huff.items()}
    comps = {c[0]: c for c in h.components}
    try:
        units = [(tables[(0, td)].lookup, tables[(1, ta)].lookup, comps[cid][1], comps[cid][2])
                 for cid, td, ta in h.scan]
    except KeyError:
        raise JpegError("missing table")
    if len(units) == 1:
        units = [(units[0][0], units[0][1], 1, 1)]
        mw, mh = (h.width + 7) // 8, (h.height + 7) // 8
    else:
        hm, vm = max(c[1] for c in h.components), max(c[2] for c in h.components)
        mw, mh = (h.width + 8 * hm - 1) // (8 * hm), (h.height + 8 * vm - 1) // (8 * vm)
    h0, v0 = units[0][2], units[0][3]
    bw = mw * h0
    pw = bw * 8
    pix = [128.0] * (pw * mh * v0 * 8)
    end = data.find(b"\xff\xd9", h.entropy_start)
    segs = re.split(rb"\xff[\xd0-\xd7]", data[h.entropy_start:end if end > 0 else len(data)])
    per = h.restart or mw * mh
    mcu = 0
    for seg in segs:
        seg = seg.replace(b"\xff\x00", b"\xff")
        bits = bin(int.from_bytes(b"\x01" + seg, "big"))[3:] + "1" * 16
        p, preds = 0, [0] * len(units)

        def get(n):
            nonlocal p
            v = int(bits[p:p + n], 2) if n else 0
            p += n
            return v - (1 << n) + 1 if n and v < 1 << (n - 1) else v

        for _ in range(per):
            if mcu >= mw * mh:
                break
            my, mx = divmod(mcu, mw)
            for ui, (dct, act, hh, vv) in enumerate(units):
                for v in range(vv):
                    for hz in range(hh):
                        e = dct[int(bits[p:p + 16], 2)]
                        if not e:
                            raise JpegError("bad code")
                        p += e >> 8
                        preds[ui] += get(e & 255)
                        coef = {0: preds[ui] * q[0]} if ui == 0 else None
                        k = 1
                        while k < 64:
                            e = act[int(bits[p:p + 16], 2)]
                            if not e:
                                raise JpegError("bad code")
                            p += e >> 8
                            r, sz = (e & 255) >> 4, e & 15
                            if sz == 0:
                                if r != 15:
                                    break
                                k += 16
                                continue
                            k += r
                            val = get(sz)
                            if coef is not None and k < 64:
                                coef[ZIGZAG[k]] = val * q[k]
                            k += 1
                        if coef is None:
                            continue
                        by, bx = my * v0 + v, mx * h0 + hz
                        block = [0.0] * 64
                        for pos, c in coef.items():
                            cu, cv = _COS, c
                            u, vv2 = pos & 7, pos >> 3
                            for y in range(8):
                                f = cv * cu[y][vv2]
                                row = cu
                                for x in range(8):
                                    block[y * 8 + x] += f * row[x][u]
                        base = by * 8 * pw + bx * 8
                        for y in range(8):
                            pix[base + y * pw:base + y * pw + 8] = [128.0 + t for t in block[y * 8:y * 8 + 8]]
            mcu += 1
    out = [pix[y * pw + x] for y in range(h.height) for x in range(h.width)]
    return h.width, h.height, out


THUMB_WINDOW = 32    # MCUs in a row of decoding order whose blocks are compared together
# Over a window: how far the blocks' brightness is from the thumbnail's range
# around each (of 255), and the mean signed difference where the thumbnail is
# flat (everything brighter or darker: a step in the blocks' DC values). On
# 443 correctly recovered photos (four encoders, black bars, hard edges)
# neither went above 6.4 and 12.6; a run of another photo's clusters gave 49
# and 63.
THUMB_DIFF = 20.0
THUMB_SHIFT = 25.0
THUMB_NOTE = ("every block decodes, but part of the picture doesn't match the small preview stored in it: "
              "part of it may be another photo's; check it")
THUMB_FLAT = 12.0    # where the thumbnail varies less than this around a block, it shows a step in DC


class Thumb:
    """Checks a decoded picture against its own EXIF thumbnail.

    Every 8x8 block of the picture has a mean brightness (its DC value), and
    the thumbnail shows the same spot at about one pixel per block. The
    differences are averaged over runs of MCUs in decoding order. A run of
    another photo's data doesn't match there; and since each block's DC is
    stored as a step from the block before, everything after it usually comes
    out brighter or darker too."""

    def __init__(self, h: Header, data: bytes):
        w, ht, pix = luma_pixels(data)
        q = h.q0.get(h.components[0][3])
        if not q:
            raise JpegError("no quantization table")
        W, H = h.width, h.height
        s = min(w / W, ht / H)
        if W * s * H * s < 0.6 * w * ht:
            raise JpegError("the thumbnail has a different shape")
        self.h, self.k, self.s = h, q / 8.0, s
        self.x0, self.y0 = (w - W * s) / 2, (ht - H * s) / 2  # black bars, if the shapes differ
        self.tw, self.th, self.pix = w, ht, pix
        self.expect = None

    def _build(self, dec):
        """For the middle of every block of the picture: the thumbnail's
        brightness there, and the lowest and highest values around it (a
        thumbnail is rarely aligned to the pixel, so at an edge any value in
        between is fine). Blocks near a black bar or the thumbnail's edge are
        left out."""
        W, H, s, tw, th, pix = self.h.width, self.h.height, self.s, self.tw, self.th, self.pix
        nan = float("nan")
        n = len(dec.dc)
        exp, lo, hi = [nan] * n, [nan] * n, [nan] * n
        cx0, cx1 = max(self.x0, 0) + 1.5, min(self.x0 + W * s, tw) - 2.5
        cy0, cy1 = max(self.y0, 0) + 1.5, min(self.y0 + H * s, th) - 2.5
        cols = []
        for x in range((W + 7) // 8):
            tx = (8 * x + 4) * s + self.x0 - 0.5
            if not cx0 <= tx <= cx1:
                cols.append(None)
                continue
            a = int(tx)
            cols.append((x, a, tx - a))
        for y in range((H + 7) // 8):
            ty = (8 * y + 4) * s + self.y0 - 0.5
            if not cy0 <= ty <= cy1:
                continue
            b = int(ty)
            fy = ty - b
            rows = [pix[(b + d) * tw:(b + d + 1) * tw] for d in (-1, 0, 1, 2)]
            base = y * dec.bw
            for c in cols:
                if c is None:
                    continue
                x, a, fx = c
                r0, r1 = rows[1], rows[2]
                top = r0[a] * (1 - fx) + r0[a + 1] * fx
                bot = r1[a] * (1 - fx) + r1[a + 1] * fx
                exp[base + x] = top * (1 - fy) + bot * fy
                near = [r[a + d] for r in rows for d in (-1, 0, 1, 2)]
                lo[base + x], hi[base + x] = min(near), max(near)
        self.expect, self.lo, self.hi = exp, lo, hi

    def errors(self, dec):
        """Per MCU decoded so far: (mean distance of its blocks' brightness
        from the thumbnail's range there, mean signed difference where the
        thumbnail is flat or None), or None."""
        if self.expect is None:
            self._build(dec)
        exp, lo, hi, dc, k = self.expect, self.lo, self.hi, dec.dc, self.k
        out = []
        for m in range(dec.mcu):
            i0 = dec.block_index(m)
            u = sg = n = nf = 0
            for v in range(dec.bv):
                for hz in range(dec.bh):
                    i = i0 + v * dec.bw + hz
                    e = exp[i]
                    if e == e:  # not NaN
                        got = 128.0 + k * dc[i]
                        u += max(0.0, got - hi[i], lo[i] - got)
                        n += 1
                        if hi[i] - lo[i] < THUMB_FLAT:
                            sg += got - e
                            nf += 1
            out.append((u / n, sg / nf if nf else None) if n else None)
        return out

    def _windows(self, dec):
        win = []
        for m, e in enumerate(self.errors(dec)):
            if e is None:
                continue
            win.append((m, e))
            if len(win) > THUMB_WINDOW:
                win.pop(0)
            if len(win) == THUMB_WINDOW:
                u = sum(x[0] for _, x in win) / THUMB_WINDOW
                flat = [x[1] for _, x in win if x[1] is not None]
                sg = abs(sum(flat)) / len(flat) if len(flat) >= THUMB_WINDOW // 4 else 0.0
                yield win[0][0], m, u, sg

    def unlike(self, dec):
        """(first MCU, last MCU) of the first run, in decoding order, that
        doesn't match the thumbnail; None if the picture matches."""
        for first, last, u, sg in self._windows(dec):
            if u > THUMB_DIFF or sg > THUMB_SHIFT:
                return first, last
        return None

    def worst(self, dec):
        """The largest window differences (for calibration): (mean, signed)."""
        u = sg = 0.0
        for _, _, a, b in self._windows(dec):
            u, sg = max(u, a), max(sg, b)
        return u, sg


def deep_check(data: bytes) -> tuple[bool, str]:
    """Decode a whole JPEG held in memory. (True, 'ok') if it is coherent."""
    try:
        h = parse_header(lambda o, n: data[o:o + n], 0)
    except JpegError as e:
        return False, str(e)
    if not h.decodable:
        return True, "not baseline; structure only"
    try:
        d = Decoder(h)
    except JpegError as e:
        return True, "decoder unsupported: %s" % e
    d.buf = data
    try:
        st = d.run(final=True)
    except DecodeError as e:
        return False, "%s at byte %d" % (e, e.pos)
    return st == "done", "ok" if st == "done" else st


def check(data: bytes, thumb: bool = False) -> tuple[str, str, int | None]:
    """Classify an in-memory JPEG: ('ok'|'partial'|'corrupt', message, bad byte).

    'partial' means the beginning decodes but the data stops early (the top
    of the picture is viewable). 'corrupt' means bytes from somewhere else got
    mixed in at the returned position.
    """
    try:
        h = parse_header(lambda o, n: data[o:o + n], 0)
    except JpegError as e:
        return "corrupt", "unreadable header (%s)" % e, 0
    if not h.decodable:
        m = measure(lambda o, n: data[o:o + n], 0)
        if m.complete:
            return "ok", "structure checked (progressive or unusual JPEG)", None
        return "partial", m.reason, m.break_pos
    try:
        d = Decoder(h)
    except JpegError as e:
        return "ok", "structure only (%s)" % e, None
    d.buf = data
    try:
        st = d.run(final=True)
    except DecodeError as e:
        msg = str(e)
        if msg == "data ended mid-image":
            return "partial", "the file stops partway through the picture", e.pos
        return "corrupt", msg, e.pos
    if st != "done":
        return "partial", "the file stops partway through the picture", len(data)
    if thumb and h.thumb:
        # The decoder falls back into step after damage, so a run of another
        # photo's data can decode to the right number of blocks. The thumbnail
        # shows what the picture should look like.
        try:
            t = Thumb(h, data[h.thumb[0]:h.thumb[0] + h.thumb[1]])
        except Exception:
            t = None  # no usable thumbnail
        if t is not None and t.unlike(d) is not None:
            return "unverified", THUMB_NOTE, None
    return "ok", "every block decoded", None


def check_fast(data: bytes) -> tuple[str, str, int | None]:
    """Structure-only check (markers, restart order, end of image). Misses one
    case the full check catches: a cluster of another photo's image data that
    happens to fit. Used by --fast on very large cards."""
    m = measure(lambda o, n: data[o:o + n], 0, len(data) + 1)
    if m.complete:
        return "ok", "structure checked (fast mode)", None
    if m.reason in ("ran out of data", "ran into empty space"):
        return "partial", "the file stops partway through the picture", m.break_pos
    return "corrupt", m.reason, m.break_pos
