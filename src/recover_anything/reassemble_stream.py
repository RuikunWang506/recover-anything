"""Put fragmented recordings and archives back together: MP3/MP2, MPEG-TS
(dashcams, camcorders), Ogg (voice memos, music), FLAC and gzip.

These files are made of small units checked one after another (frames,
packets, pages), so the same idea as for photos works without decoding:
walk the file unit by unit over the clusters it seems to use; when a unit
stops checking out, its data went on somewhere else. Try the clusters
nobody else owns, in order, as the next piece: the right one makes the next
units check out, the wrong ones almost never do.

Ogg pages, FLAC frames and gzip data carry checksums, so a rebuild that
reaches the end is certain. MP3 and transport streams don't, so a rebuilt one
is only called recovered when its end is proven too (a length in the file or
the folder entry); otherwise it is saved as "check that it plays".
"""
from __future__ import annotations

import struct
import zlib

from .formats import more

MAX_SCAN = 4000       # free clusters to try after a break
MAX_TRIALS = 6000     # candidate pieces tried per file, in all
MAX_PIECES = 64       # more breaks than this: give up on the file
SPAN = 8              # clusters a single unit (page, frame) may stretch over
KEEP_STATES = 256     # walker states kept for the latest clusters (see _Rebuild.run)


class _Path:
    """read(off, n) over a list of clusters."""

    def __init__(self, src, geom, clusters):
        self.src, self.g, self.cs, self.clusters = src, geom, geom.cluster_size, clusters

    def read(self, off: int, n: int) -> bytes:
        cs, out = self.cs, []
        while n > 0:
            i = off // cs
            if i >= len(self.clusters) or off < 0:
                break
            o = off % cs
            take = min(n, cs - o)
            out.append(self.src.read(self.g.offset(self.clusters[i]) + o, take))
            off += take
            n -= take
        return b"".join(out)


# ------------------------------------------------------------------ walkers
class _Walker:
    """Checks a file unit by unit and can be rewound. advance(limit) returns
    'need' (the next check needs bytes at or past `limit`), 'end' (the file is
    complete at self.end) or 'bad' (the unit starting at self.unit didn't
    check out). Everything before self.unit is verified."""

    strong = False  # a unit that checks out is the file's own beyond reasonable doubt
    grain = 1 << 16  # the cluster size, when a rebuild runs the walker (set by _Rebuild)
    need = 1        # bytes of the next unit needed to tell it isn't there
    checked = -1    # for a weak kind: the last position where something tied the data to what came before
    horizon = 0     # clusters from the failing unit to its next check, when that's further than usual
    first_choice = False  # a tie only counts where the join is the first cluster the file could go on in
    weak = ()       # (start, end) stretches nothing checks: a join inside one proves nothing

    def __init__(self, read, expected=None):
        self.read, self.expected = read, expected
        self.unit = 0
        self.end = None
        self.start_marked = True

    def tied(self, seams) -> bool:
        """Does something in the file tie the data after every join to what came before?"""
        return not seams or self.checked > max(seams)

    def snapshot(self):
        # sets, dicts and lists (open streams, packet counters) are copied:
        # the walker changes them in place as it goes
        return {k: (v.copy() if isinstance(v, (set, dict, list)) else v) for k, v in self.__dict__.items()}

    def restore(self, snap):
        self.__dict__.update({k: (v.copy() if isinstance(v, (set, dict, list)) else v) for k, v in snap.items()})


class _Mp3(_Walker):
    need = 4
    first_choice = True

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        h = read(0, 10)
        self.pos = 10 + more._synchsafe(h[6:10]) + (10 if h[5] & 0x10 else 0) if h[:3] == b"ID3" else 0
        self.tagged = h[:3] == b"ID3"
        self.first_at, self.key, self.declared, self.last = self.pos, None, None, None
        self.start_marked = self.tagged
        self.room = None   # the bit reservoir: room the frames so far left (see more.Reservoir)
        self.tail = False  # past the end its Xing/Info header gives: only the song's tags may follow
        self.audit = None  # when set: (cluster -> (verdict, main_data_begin) of the first frame
        #                    starting in or after it, main_data_begin -> frames pointing there)

    def tied(self, seams) -> bool:
        # A join is tied when the first frame after it points exactly at the room
        # the frames before left in the reservoir, at a value this recording
        # seldom points at: 0 and an encoder's ceiling are where frames of any
        # recording point. That alone is weak (frames of another recording tie
        # about one time in 370), so _settle also asks that the join skips only
        # clusters known to be other files' (see _first_choice). One more walk
        # over the finished file counts what the frames point at (a search
        # keeping that up would copy it with every state it saves).
        a = _Mp3(self.read, self.expected)
        a.grain, a.audit = self.grain, ({}, {})
        a.advance(self.end if self.end is not None else self.pos)
        joins, hist = a.audit
        total = sum(hist.values())
        for s in seams:
            v = joins.get(s // a.grain)
            if v is None or v[0] != "tie" or not v[1] or hist.get(v[1], 0) * 32 > total:
                return False
        return True

    def advance(self, limit):
        read = self.read
        while True:
            if self.expected is not None and self.pos in (self.expected, self.expected - 128):
                if self.pos == self.expected or read(self.pos, 3) == b"TAG":
                    self.end = self.expected  # (the rest of a tag may be in a cluster not taken yet)
                    return "end"
            if self.declared is not None and self.pos - self.first_at == self.declared:
                self.tail, self.declared = True, None
            if self.pos + 4 > limit:
                return "need"
            head = read(self.pos, min(32, limit - self.pos))
            if self.last is not None:  # tags after the last frame are the song's
                if head[:3] == b"TAG":
                    self.end = self.pos + 128  # (its text may run into a cluster not taken yet)
                    return "end"
                if len(head) < 8 and b"APETAGEX".startswith(head):
                    return "need"
                if head[:8] == b"APETAGEX":
                    if len(head) < 32:
                        return "need"
                    self.pos += struct.unpack("<I", head[12:16])[0] + 32
                    continue
            if self.tail:
                self.end = self.pos
                return "end"
            f = more.mp3_frame(head[:4])
            if f is None or (self.key is not None and f[1] != self.key):
                self.unit = self.last if self.last is not None else self.pos
                return "bad"
            n, k = f
            if self.pos + 40 > limit:
                return "need"
            side = more.mp3_side(read(self.pos, 40), n)
            mdb = None
            if side is not None:
                res = more.Reservoir()
                res.room = self.room
                verdict = res.step(side)
                if verdict == "bad":
                    self.unit = self.last if self.last is not None else self.pos
                    return "bad"  # a frame of another recording: its data can't be where it points
                mdb = side[0]
            else:
                verdict, res = "gap", None
            if self.key is None:
                if self.pos + 200 > limit:
                    return "need"
                body = read(self.pos, min(n, 200))
                for tag in (b"Xing", b"Info"):
                    i = body.find(tag)
                    if 0 < i < 64 and len(body) >= i + 16:
                        flags = struct.unpack(">I", body[i + 4:i + 8])[0]
                        if flags & 2:
                            at = i + 8 + (4 if flags & 1 else 0)
                            self.declared = struct.unpack(">I", body[at:at + 4])[0]
                        break
                self.key = k
                self.start_marked = self.tagged or self.declared is not None
            if self.audit is not None:
                joins, hist = self.audit
                g = self.grain
                for ci in range((self.last // g + 1) if self.last is not None else 0, self.pos // g + 1):
                    joins[ci] = (verdict, mdb)
                if mdb is not None:
                    hist[mdb] = hist.get(mdb, 0) + 1
            if res is not None:
                self.room = res.room
            self.last = self.pos
            self.unit = self.pos
            self.pos += n

    def proven_end(self):
        return self.end is not None


class _Ts(_Walker):
    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        lay = more._ts_layout(read(0, 1024)) or (188, 0)
        self.stride, self.off = lay
        self.need = self.off + 4
        self.pos, self.cc, self.firsts = 0, {}, []
        self.start_marked = False
        self.clock = more.TsClock()
        self.n = 0

    def snapshot(self):
        snap = super().snapshot()
        snap["clock"] = dict(self.clock.__dict__)
        return snap

    def restore(self, snap):
        clock = snap.get("clock")
        super().restore({k: v for k, v in snap.items() if k != "clock"})
        self.clock = more.TsClock()
        self.clock.__dict__.update(clock)

    @property
    def checked(self):
        return self.clock.checked

    def advance(self, limit):
        read, stride, off = self.read, self.stride, self.off
        while True:
            if self.expected is not None and self.pos == self.expected:
                self.end = self.pos
                return "end"
            if self.pos + stride > limit:
                return "need"
            p = read(self.pos + off, 6)
            pid = ((p[1] & 0x1F) << 8) | p[2] if len(p) >= 4 else 0
            afc, c = ((p[3] >> 4) & 3, p[3] & 15) if len(p) >= 4 else (0, 0)
            bad = len(p) < 4 or p[0] != 0x47 or p[1] & 0x80 or afc == 0
            if not bad and pid != 0x1FFF and afc & 1:
                prev = self.cc.get(pid)
                disc = afc & 2 and len(p) >= 6 and p[4] > 0 and p[5] & 0x80
                if prev is None and len(self.firsts) < 64 and self.pos < 256 * stride:
                    self.firsts.append(c)
                if prev is not None and not disc and c != (prev + 1) & 15 and c != prev:
                    bad = True
            if not bad and not self.clock.packet(read(self.pos, stride), off, self.n, self.pos):
                bad = True
            if bad:
                self.unit = max(0, self.pos - stride)
                return "bad"
            self.n += 1
            if pid != 0x1FFF and afc & 1:
                self.cc[pid] = c
            self.start_marked = bool(self.firsts) and not any(self.firsts)
            self.unit = self.pos
            self.pos += stride

    def proven_end(self):
        return self.end is not None


class _Ogg(_Walker):
    strong = True

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        self.pos, self.live, self.began = 0, set(), False

    def advance(self, limit):
        read = self.read
        while True:
            self.unit = self.pos
            if self.pos + 27 > limit:
                return "need"
            h = read(self.pos, 27)
            if h[:5] != b"OggS\x00":
                return "bad"
            nseg = h[26]
            if self.pos + 27 + nseg > limit:
                return "need"
            plen = 27 + nseg + sum(read(self.pos + 27, nseg))
            if self.pos + plen > limit:
                return "need"
            page = read(self.pos, plen)
            if more.crc32_msb(page[:22] + b"\0\0\0\0" + page[26:]) != struct.unpack("<I", page[22:26])[0]:
                return "bad"
            flags, serial = page[5], page[14:18]
            if flags & 2:
                if serial in self.live:
                    return "bad"
                self.live.add(serial)
            elif serial not in self.live:
                return "bad"
            if flags & 4:
                self.live.discard(serial)
            self.pos += plen
            self.began = True
            if not self.live:
                if self.pos + 6 > limit and self.expected != self.pos:
                    nxt = b""
                    if self.expected is None:
                        return "need"
                else:
                    nxt = read(self.pos, 6)
                if not (nxt[:5] == b"OggS\x00" and nxt[5] & 2):
                    self.end = self.pos
                    return "end"

    def proven_end(self):
        return self.end is not None


class _Flac(_Walker):
    strong = True

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        pos, info = 4, None
        for _ in range(4096):
            h = read(pos, 4)
            if len(h) < 4:
                break
            if (h[0] & 0x7F) == 0:
                info = read(pos + 4, 34)
            pos += 4 + int.from_bytes(h[1:4], "big")
            if h[0] & 0x80:
                break
        if not info or len(info) < 34:
            raise ValueError("no STREAMINFO")
        self.min_fs = int.from_bytes(info[4:7], "big")
        self.max_fs = int.from_bytes(info[7:10], "big") or (1 << 20)
        self.total = ((info[13] & 15) << 32) | int.from_bytes(info[14:18], "big")
        self.pos, self.samples, self.index = pos, 0, 0
        self.hdr = None  # (block size, header length, variable) of the frame at pos
        self.crc, self.i, self.seek = 0, 0, 0  # checksum so far, how far it got, where to look for the next header

    def advance(self, limit):
        read, tab = self.read, more._CRC16
        while True:
            self.unit = self.pos
            if self.hdr is None:
                if self.pos + 16 > limit:
                    return "need"
                h16 = read(self.pos, 16)
                hd = more._flac_header(h16)
                var = h16[1:2] == b"\xf9"
                if hd is None or hd[1] != (self.samples if var else self.index):
                    return "bad"
                self.hdr, self.crc, self.i, self.seek = (hd[0], hd[2], var), 0, 0, hd[2] + 2
            bs, hlen, var = self.hdr
            last = self.total and self.samples + bs >= self.total
            avail = min(limit - self.pos, self.max_fs + 2 + 16)
            data = read(self.pos, avail)
            if last:
                # nothing follows the last frame: find where its checksum closes
                crc, i = self.crc, self.i
                lo = max(hlen + 3, self.min_fs)
                for b in data[i:]:
                    crc = ((crc << 8) & 0xFFFF) ^ tab[(crc >> 8) ^ b]
                    i += 1
                    if crc == 0 and i >= lo:
                        self.end = self.pos + i
                        return "end"
                self.crc, self.i = crc, i
                return "bad" if i >= self.max_fs + 2 else "need"
            # Otherwise the frame ends right before the next frame's header:
            # look for that first (fast), check the checksum only there.
            sync = b"\xff\xf9" if var else b"\xff\xf8"
            want = self.samples + bs if var else self.index + 1
            k = self.seek
            while True:
                j = data.find(sync, k)
                if j < 0 or j + 16 > len(data):
                    if avail >= self.max_fs + 2 + 16 or (j >= 0 and j > self.max_fs + 2):
                        return "bad"
                    self.seek = max(k, len(data) - 16)
                    return "need"
                if j > self.max_fs + 2:
                    return "bad"
                nxt = more._flac_header(data[j:j + 16])
                if nxt and nxt[1] == want:
                    crc = self.crc
                    for b in data[self.i:j]:
                        crc = ((crc << 8) & 0xFFFF) ^ tab[(crc >> 8) ^ b]
                    self.crc, self.i = crc, j
                    if crc == 0:
                        self.pos += j
                        self.samples += bs
                        self.index += 1
                        self.hdr = None
                        break
                k = j + 1

_TEXT = bytes(range(32, 127)) + b"\t\n\r\x0c" + bytes(range(128, 256))


class _Gzip(_Walker):
    strong = True  # the checksum over all of it is checked at the end
    grain = 1 << 16  # set to the cluster size: data is fed one cluster at a time

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        self.member_start = 0
        self._new_member(0)

    def _new_member(self, at):
        h = self.read(at, 10)
        flg = h[3] if len(h) >= 4 else 0
        p = at + 10
        if flg & 4:
            p += 2 + struct.unpack("<H", self.read(p, 2))[0]
        for bit in (8, 16):
            if flg & bit:
                chunk = self.read(p, 65536)
                i = chunk.find(b"\0")
                if i < 0:
                    raise ValueError("header name too long")
                p += i + 1
        if flg & 2:
            p += 2
        self.pos, self.d, self.crc, self.size = p, zlib.decompressobj(-15), 0, 0
        self.text = None  # does it hold text? Then binary junk in the output means a wrong cluster
        self.sample = b""

    def snapshot(self):
        snap = dict(self.__dict__)
        snap["d"] = self.d.copy()
        return snap

    def restore(self, snap):
        self.__dict__.update(snap)
        self.d = snap["d"].copy()

    def advance(self, limit):
        while True:
            self.unit = self.pos
            if not self.d.eof:
                if self.pos >= limit:
                    return "need"
                g = self.grain
                data = self.read(self.pos, min(limit - self.pos, g - self.pos % g))
                buf = data
                junk = made = 0
                try:
                    while buf and not self.d.eof:
                        out = self.d.decompress(buf, 1 << 20)
                        self.crc = zlib.crc32(out, self.crc)
                        self.size += len(out)
                        buf = self.d.unconsumed_tail
                        if self.text is None:
                            self.sample += out[:8192 - len(self.sample)]
                            if len(self.sample) >= 8192:
                                self.text = len(self.sample.translate(None, _TEXT)) <= 8
                        elif self.text:
                            junk += len(out.translate(None, _TEXT))
                            made += len(out)
                except zlib.error:
                    return "bad"
                if self.text and made >= 256 and junk * 50 > made:
                    return "bad"  # text turned into binary: these bytes aren't this file's
                left = max(len(self.d.unused_data), len(buf)) if self.d.eof else len(buf)
                self.pos += len(data) - left
                continue
            if self.pos + 8 > limit:
                return "need"
            tr = self.read(self.pos, 8)
            if struct.unpack("<II", tr) != (self.crc & 0xFFFFFFFF, self.size & 0xFFFFFFFF):
                return "bad"
            end = self.pos + 8
            if self.expected is None and end + 10 > limit:
                self.pos = end - 8
                return "need" if end + 10 <= limit + (1 << 16) else "bad"
            if (self.expected is None or end < self.expected) and more.probe_gzip(self.read(end, 10)):
                self.member_start = end
                self._new_member(end)
                continue
            self.end = end
            return "end"

    def proven_end(self):
        return self.end is not None


class _Zip(_Walker):
    """ZIP and what is built on it (Word, Excel, PowerPoint, EPUB): every
    member's data is checked as it goes (compressed data by decompressing it)
    and against its CRC at its end, and the central directory at the end of
    the archive must list exactly the members walked."""

    strong = True
    grain = 1 << 16
    any_cluster = True  # members are often pictures stored as they are: JPEG data belongs here

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        if read(0, 4) != b"PK\x03\x04":
            raise ValueError("not a ZIP")
        self.pos = 0
        self.members: dict = {}   # local header offset -> CRC
        self.member = None        # (data start, method, flags, crc, compressed size, size)
        self.d = None
        self.crc = self.size = 0
        self.central = 0
        self.cd_start = None

    def snapshot(self):
        snap = super().snapshot()
        snap["d"] = self.d.copy() if self.d is not None else None
        return snap

    def restore(self, snap):
        super().restore(snap)
        self.d = snap["d"].copy() if snap["d"] is not None else None

    def advance(self, limit):
        read = self.read
        while True:
            if self.member is not None:
                st = self._data(limit)
                if st != "done":
                    return st
                continue
            self.unit = self.pos
            if self.pos + 4 > limit:
                return "need"
            sig = read(self.pos, 4)
            if sig == b"PK\x03\x04":
                if self.cd_start is not None or self.pos + 30 > limit:
                    return "bad" if self.cd_start is not None else "need"
                (_v, flags, method, _t, _d, crc, csize, usize, nlen, xlen) = struct.unpack("<HHHHHIIIHH",
                                                                                         read(self.pos + 4, 26))
                if method not in (0, 8) or flags & 1 or not 0 < nlen <= 1024 or 0xFFFFFFFF in (csize, usize):
                    return "bad"  # encrypted, another method, ZIP64: nothing here can check it
                if method == 0 and flags & 8:
                    return "bad"  # stored with its size only after the data: its end can't be found
                if self.pos + 30 + nlen + xlen > limit:
                    return "need"
                if b"\0" in read(self.pos + 30, nlen):
                    return "bad"
                self.members[self.pos] = crc
                start = self.pos + 30 + nlen + xlen
                self.member = (start, method, flags, crc, csize, usize)
                self.d = zlib.decompressobj(-15) if method == 8 else None
                self.crc = self.size = 0
                self.pos = start
                continue
            if sig == b"PK\x01\x02":
                if self.cd_start is None:
                    self.cd_start = self.pos
                if self.pos + 46 > limit:
                    return "need"
                h = read(self.pos, 46)
                crc, nlen, xlen, clen, lho = (struct.unpack("<I", h[16:20])[0], *struct.unpack("<HHH", h[28:34]),
                                              struct.unpack("<I", h[42:46])[0])
                if self.members.get(lho) != crc:
                    return "bad"  # the directory lists a member that isn't where it says
                self.central += 1
                self.pos += 46 + nlen + xlen + clen
                continue
            if sig == b"PK\x05\x06":
                if self.pos + 22 > limit:
                    return "need"
                _d0, _d1, _n0, total, cd_size, cd_off, clen = struct.unpack("<HHHHIIH", read(self.pos + 4, 18))
                cd = self.cd_start if self.cd_start is not None else self.pos
                if not (total == self.central == len(self.members) and cd_off == cd and cd_size == self.pos - cd):
                    return "bad"
                if self.pos + 22 + clen > limit:
                    return "need"
                self.end = self.pos + 22 + clen
                return "end"
            return "bad"

    def _data(self, limit):
        start, method, flags, crc, csize, usize = self.member
        g = self.grain
        if method == 8:
            while not self.d.eof:
                if self.pos >= limit:
                    return "need"
                at = self.pos
                data = self.read(at, min(limit - at, g - at % g))
                buf = data
                try:
                    while buf and not self.d.eof:
                        out = self.d.decompress(buf, 1 << 20)
                        self.crc = zlib.crc32(out, self.crc)
                        self.size += len(out)
                        buf = self.d.unconsumed_tail
                except zlib.error:
                    # Data zlib couldn't shrink is stored in blocks of up to 64 KB that
                    # nothing checks before the end: the break may be anywhere back to
                    # the member's start.
                    self.unit, self.horizon = start, (at + len(data) - start) // max(1, g) + 2
                    return "bad"
                self.pos = at + len(data) - (len(self.d.unused_data) if self.d.eof else len(buf))
            if not flags & 8 and self.pos - start != csize:
                self.unit, self.horizon = start, csize // max(1, g) + 2
                return "bad"
        else:
            end = start + csize
            self.horizon = (end - start) // max(1, g) + 2  # nothing checks stored data before its CRC
            while self.pos < end:
                if self.pos >= limit:
                    return "need"
                at = self.pos
                n = min(end, limit) - at
                n = min(n, g - at % g)
                self.crc = zlib.crc32(self.read(at, n), self.crc)
                self.size += n
                self.pos = at + n
        if flags & 8:  # the CRC and sizes come after the data
            if self.pos + 16 > limit:
                return "need"
            dd = self.read(self.pos, 16)
            skip = 4 if dd[:4] == b"PK\x07\x08" else 0
            crc, csize, usize = struct.unpack("<III", dd[skip:skip + 12])
            if self.pos - start != csize:
                self.unit = start
                return "bad"
            self.pos += skip + 12
            self.members[max(self.members)] = crc
        if self.crc & 0xFFFFFFFF != crc or self.size != usize:
            # the data doesn't add up: the wrong piece is somewhere in it
            self.unit, self.horizon = start, (self.pos - start) // max(1, g) + 2
            return "bad"
        self.member, self.d, self.horizon = None, None, 0
        return "done"

    def proven_end(self):
        return self.end is not None


_OBJ_AT = __import__("re").compile(rb"(\d{1,10})\s+(\d{1,5})\s+obj\b")
_PDF_STREAM = __import__("re").compile(rb"\bstream(\r\n|\n|\r)|\bendobj\b")
_PDF_LENGTH = __import__("re").compile(rb"/Length\s+(\d+)(\s+\d+\s+R)?")
_PDF_FILTER = __import__("re").compile(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9]+)")
_PDF_END = __import__("re").compile(rb"startxref\s+(\d+)\s*%%EOF(\r\n|\n|\r)?")


class _Pdf(_Walker):
    """PDF: object by object in file order. Each object must start where the
    last one ended ('N G obj'), each compressed stream must decompress (zlib's
    checksum at its end proves its bytes), and every cross-reference table must
    list the objects at the offsets they were found. The rebuilt file is then
    checked once more as a whole (`final`)."""

    strong = True
    grain = 1 << 16
    any_cluster = True  # pictures are stored in it as JPEG data
    WINDOW = 1 << 16    # an object's dictionary is looked for within this many bytes

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        h = read(0, 1024)
        if not h.startswith(b"%PDF-"):
            raise ValueError("not a PDF")
        i = h.find(b"\n", 5)
        self.pos = i + 1 if i > 0 else 9
        self.seen = set()       # (object number, offset)
        self.pending = set()    # listed by a table before they were reached
        self.stream = None      # (data start, length or None, flate)
        self.d = None

    def snapshot(self):
        snap = super().snapshot()
        snap["d"] = self.d.copy() if self.d is not None else None
        return snap

    def restore(self, snap):
        super().restore(snap)
        self.d = snap["d"].copy() if snap["d"] is not None else None

    def _skip_space(self, limit):
        """Move past whitespace and comments; False if more bytes are needed."""
        while True:
            if self.pos >= limit:
                return False
            chunk = self.read(self.pos, min(256, limit - self.pos))
            stripped = chunk.lstrip(b" \t\r\n\x0c\x00")
            self.pos += len(chunk) - len(stripped)
            if not stripped:
                continue
            if stripped[:1] == b"%" and not stripped.startswith(b"%%EOF"):
                i = min([k for k in (stripped.find(b"\n"), stripped.find(b"\r")) if k >= 0] or [-1])
                if i < 0:
                    if self.pos + len(stripped) >= limit:
                        return False
                    self.pos += len(stripped)
                    continue
                self.pos += i
                continue
            return True

    def advance(self, limit):
        read = self.read
        while True:
            if self.stream is not None:
                st = self._stream(limit)
                if st != "done":
                    return st
                continue
            self.unit = self.pos
            if not self._skip_space(limit):
                return "need"
            self.unit = self.pos
            head = read(self.pos, min(64, limit - self.pos))
            m = _OBJ_AT.match(head)
            if m:
                if len(head) == 64 or m.end() < len(head):
                    num, at = int(m.group(1)), self.pos
                    self.seen.add((num, at))
                    self.pending.discard((num, at))
                    end = min(limit, at + self.WINDOW)
                    body = read(at, end - at)
                    sm = _PDF_STREAM.search(body, m.end())
                    if sm is None:
                        if end >= limit and limit < at + self.WINDOW:
                            return "need"
                        return "bad"
                    if sm.group(0).startswith(b"endobj"):
                        self.pos = at + sm.end()
                        continue
                    dic = body[m.end():sm.start()]
                    lm = _PDF_LENGTH.search(dic)
                    fm = _PDF_FILTER.search(dic)
                    flt = __import__("re").findall(rb"/(\w+)", fm.group(1)) if fm else []
                    flate = bool(flt) and flt[0] == b"FlateDecode"
                    length = int(lm.group(1)) if lm and not lm.group(2) else None
                    if length is None and not flate:
                        return "bad"  # no stated length and nothing to find its end by
                    self.stream = (at + sm.end(), length, flate)
                    self.d = zlib.decompressobj() if flate else None
                    self.pos = at + sm.end()
                    continue
                return "need"
            if head.startswith(b"xref"):
                return self._xref(limit)
            if head.startswith(b"trailer") or head.startswith(b"startxref"):
                return self._tail(limit)
            if len(head) < 9 and self.pos + len(head) >= limit:
                return "need"
            return "bad"

    def _stream(self, limit):
        start, length, flate = self.stream
        g = self.grain
        if flate:
            while not self.d.eof:
                if self.pos >= limit:
                    return "need"
                at = self.pos
                data = self.read(at, min(limit - at, g - at % g))
                buf = data
                try:
                    while buf and not self.d.eof:
                        self.d.decompress(buf, 1 << 20)
                        buf = self.d.unconsumed_tail
                except zlib.error:
                    # (stored blocks of data zlib couldn't shrink: nothing checks them
                    # before the end, so the break may be anywhere back to the start)
                    self.unit, self.horizon = start, (at + len(data) - start) // max(1, g) + 2
                    return "bad"
                self.pos = at + len(data) - (len(self.d.unused_data) if self.d.eof else len(buf))
            if length is not None and self.pos - start != length:
                self.unit, self.horizon = start, length // max(1, g) + 2
                return "bad"
        else:
            end = start + length
            self.horizon = (end - start) // max(1, g) + 2  # nothing checks it before the end
            if end > limit:
                self.pos = limit
                return "need"
            self.pos = end
        tail = self.read(self.pos, 32)
        stripped = tail.lstrip(b"\r\n")
        if not stripped.startswith(b"endstream"):
            self.unit = start
            return "bad"
        rest = stripped[9:].lstrip(b" \t\r\n")
        if not rest.startswith(b"endobj") and len(tail) == 32:
            self.unit = start
            return "bad"
        self.pos += len(tail) - len(stripped) + 9 + (len(stripped) - 9 - len(rest)) + 6
        self.stream, self.d, self.horizon = None, None, 0
        return "done"

    def _xref(self, limit):
        at = self.pos
        pos = at + 4
        while True:
            hm = __import__("re").match(rb"\s*(\d+)\s+(\d+)[ \t]*(\r\n|\n|\r)", self.read(pos, 40))
            if not hm:
                break
            first, count = int(hm.group(1)), int(hm.group(2))
            pos += hm.end()
            if pos + 20 * count > limit:
                return "need"
            raw = self.read(pos, 20 * count)
            for k in range(count):
                e = raw[20 * k:20 * k + 20]
                if e[17:18] == b"n":
                    ent = (first + k, int(e[:10]))
                    if ent not in self.seen:
                        if ent[1] < at:
                            return "bad"  # the table says an object is somewhere it isn't
                        self.pending.add(ent)  # (a linearized file lists objects before they come)
                elif e[17:18] != b"f":
                    return "bad"
            pos += 20 * count
        self.pos = pos
        self.xref_at = at
        return self._tail(limit)

    def _tail(self, limit):
        """'trailer << ... >> startxref N %%EOF': the end, or an update follows."""
        start = self.pos
        window = self.read(start, min(limit - start, 1 << 16))
        m = _PDF_END.search(window)
        if not m:
            return "need" if start + len(window) >= limit and len(window) < 1 << 16 else "bad"
        end = start + m.end()
        if m.group(2) is None and end + 2 > limit:
            return "need"
        self.pos = end
        if self.expected is not None and end < self.expected:
            return self.advance(limit)  # an update appended to it follows
        nxt = self.read(end, 64).lstrip(b" \t\r\n\x00")
        if _OBJ_AT.match(nxt):
            return self.advance(limit)
        if self.pending:
            return "bad"
        self.end = end
        return "end"

    def proven_end(self):
        return self.end is not None

    @staticmethod
    def final(read, length):
        from .formats.checks import validate_pdf
        return validate_pdf(read, length)


class _Avi(_Walker):
    """AVI, chunk by chunk: every chunk header must follow the last chunk
    exactly, every Motion-JPEG frame must decode as a whole picture, and the
    index at the end must list the frames where they were found. Audio and
    other video can't be checked inside, so those stretches are `weak`."""

    strong = True
    grain = 1 << 16
    any_cluster = True
    MJPEG = (b"MJPG", b"mjpg", b"AVRn", b"jpeg", b"JPEG", b"dmb1", b"AVDJ")

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        h = read(0, 12)
        if h[:4] != b"RIFF" or h[8:12] != b"AVI ":
            raise ValueError("not an AVI")  # (a WAV or WebP: nothing in it to rebuild by)
        size = struct.unpack("<I", h[4:8])[0]
        self.riff_end = 8 + size + (size & 1)
        self.pos = 12
        self.codecs: dict = {}
        self.chunks: dict = {}
        self.movi = None
        self.weak = []
        self.after_weak = None  # where the unchecked chunk just walked began

    def _bad(self):
        """A check failed: if it came right after a stretch nothing checks, the
        wrong piece may be anywhere in that stretch."""
        if self.after_weak is not None:
            self.unit = self.after_weak
            self.horizon = (self.pos - self.after_weak) // max(1, self.grain) + 2
        return "bad"

    def advance(self, limit):
        read, g = self.read, self.grain
        while True:
            self.unit = self.pos
            if self.pos >= self.riff_end:
                h = read(self.pos, 12) if self.pos + 12 <= limit else b""
                if h[:4] == b"RIFF" and h[8:12] == b"AVIX":  # (a long recording goes on in another RIFF)
                    size = struct.unpack("<I", h[4:8])[0]
                    self.riff_end = self.pos + 8 + size + (size & 1)
                    self.pos += 12
                    continue
                if h == b"" and self.riff_end > 1 << 29 and (self.expected is None or self.pos < self.expected):
                    return "need"  # a part this big may go on in another one: look at what follows
                self.end = self.riff_end
                return "end"
            if self.pos + 12 > limit:
                return "need"
            h = read(self.pos, 12)
            fourcc, size = h[:4], struct.unpack("<I", h[4:8])[0]
            body = self.pos + 8
            if not all(32 <= b < 127 for b in fourcc) or body + size > self.riff_end:
                return self._bad()
            if fourcc == b"LIST":
                if h[8:12] == b"hdrl":
                    if body + size > limit:
                        return "need"
                    self._streams(read(body + 4, size - 4))
                elif h[8:12] == b"movi":
                    self.movi = body
                self.pos = body + 4  # walk into the list
                continue
            kind = fourcc[2:4]
            if fourcc[:2].isdigit() and kind in (b"dc", b"db") and size:
                codec = self.codecs.get(int(fourcc[:2]))
                if body + size > limit:
                    self.horizon = size // g + 2  # the frame is checked as a whole
                    return "need"
                frame = read(body, 4)
                if codec in self.MJPEG or (codec is None and frame[:3] == b"\xff\xd8\xff"):
                    from .formats import jpeg
                    data = read(body, size)
                    # markers first (most wrong clusters fail there), then the full decode
                    if jpeg.check_fast(data)[0] != "ok" or jpeg.check(data)[0] != "ok":
                        return "bad"
                    self.after_weak = None
                else:
                    if not (frame[:3] == b"\x00\x00\x01" or frame == b"\x00\x00\x00\x01"):
                        return self._bad()
                    self.weak.append((body + 4, body + size))  # only its start is checked
                    self.after_weak = self.pos
                self.chunks[self.pos] = (fourcc, size)
            elif fourcc == b"idx1":
                if body + size > limit:
                    return "need"
                if not self._index(read(body, size)):
                    return self._bad()
            else:
                if fourcc[:2].isdigit():
                    self.chunks[self.pos] = (fourcc, size)
                self.weak.append((body, body + size))  # audio, padding: nothing checks it
                if self.after_weak is None:
                    self.after_weak = self.pos
                if body + size > limit:
                    return "need"
            self.horizon = 0
            self.pos = body + size + (size & 1)

    def _streams(self, hdrl: bytes):
        n, p = 0, 0
        while p + 8 <= len(hdrl):
            fourcc, size = hdrl[p:p + 4], struct.unpack("<I", hdrl[p + 4:p + 8])[0]
            if fourcc == b"LIST" and hdrl[p + 8:p + 12] == b"strl":
                strh = hdrl[p + 12:p + 12 + 64]
                if strh[:4] == b"strh" and strh[8:12] == b"vids":
                    self.codecs[n] = strh[12:16]
                    q = p + 12 + 8 + struct.unpack("<I", strh[4:8])[0]
                    strf = hdrl[q:q + 28]
                    if strf[:4] == b"strf" and strf[24:28].strip(b"\0"):
                        self.codecs[n] = strf[24:28]
                n += 1
            p += 8 + size + (size & 1)

    def _index(self, idx: bytes) -> bool:
        base = None
        for k in range(0, len(idx) - 15, 16):
            ck, _flags, off, size = struct.unpack("<4sIII", idx[k:k + 16])
            if base is None:  # offsets count from the 'movi' fourcc (or just after it), or from the start
                base = next((b for b in (self.movi or 0, (self.movi or 0) + 4, 0) if b + off in self.chunks), None)
                if base is None:
                    return False
            if self.chunks.get(base + off, (ck, size)) != (ck, size):
                return False
        return True

    def proven_end(self):
        return self.end is not None


class _Lzw:
    """GIF's LZW, fed in pieces: counts the pixels, fails on a code that can't be."""

    def __init__(self, min_size: int):
        if not 2 <= min_size <= 8:
            raise ValueError("bad LZW code size")
        self.min = min_size
        self.clear, self.eoi = 1 << min_size, (1 << min_size) + 1
        self.lens = [1] * self.clear + [0] * (4096 - self.clear)
        self.size, self.nxt, self.prev = min_size + 1, self.eoi + 1, -1
        self.count = self.acc = self.nacc = 0
        self.ended = False

    def copy(self):
        c = _Lzw.__new__(_Lzw)
        c.__dict__.update(self.__dict__)
        c.lens = list(self.lens)
        return c

    def feed(self, data: bytes) -> bool:
        acc, nacc, size = self.acc, self.nacc, self.size
        lens, count, prev, nxt = self.lens, self.count, self.prev, self.nxt
        clear, eoi = self.clear, self.eoi
        ok = True
        for b in data:
            acc |= b << nacc
            nacc += 8
            while nacc >= size and not self.ended:
                code = acc & ((1 << size) - 1)
                acc >>= size
                nacc -= size
                if code == clear:
                    size, nxt, prev = self.min + 1, eoi + 1, -1
                    continue
                if code == eoi:
                    self.ended = True
                    break
                if prev < 0:
                    if code >= clear:
                        ok = False
                        break
                    count += 1
                elif code < nxt:
                    count += lens[code]
                elif code == nxt:
                    count += lens[prev] + 1
                else:
                    ok = False
                    break
                if prev >= 0 and nxt < 4096:
                    lens[nxt] = lens[prev] + 1
                    nxt += 1
                    if nxt == 1 << size and size < 12:
                        size += 1
                prev = code
            if not ok:
                break
        self.acc, self.nacc, self.size, self.count, self.prev, self.nxt = acc, nacc, size, count, prev, nxt
        return ok


class _Gif(_Walker):
    """GIF, block by block: each picture's compressed data must decode to
    exactly its number of pixels, every code valid where it stands. Comment
    and application blocks can't be checked (`weak`)."""

    strong = True
    grain = 1 << 16
    any_cluster = True

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        h = read(0, 13)
        if h[:6] not in (b"GIF87a", b"GIF89a"):
            raise ValueError("not a GIF")
        self.pos = 13 + (3 * (2 << (h[10] & 7)) if h[10] & 0x80 else 0)
        self.lzw = None
        self.want = 0
        self.pic = 0
        self.weak = []
        self.ext = False

    def snapshot(self):
        snap = super().snapshot()
        snap["lzw"] = self.lzw.copy() if self.lzw is not None else None
        return snap

    def restore(self, snap):
        super().restore(snap)
        self.lzw = snap["lzw"].copy() if snap["lzw"] is not None else None

    def advance(self, limit):
        read = self.read
        while True:
            if self.lzw is not None or self.ext:
                # sub-blocks: a length byte, then that many bytes; 0 ends them
                if self.pos >= limit:
                    return "need"
                n = read(self.pos, 1)[0]
                if n == 0:
                    self.pos += 1
                    if self.lzw is not None and self.lzw.count != self.want:
                        self.unit = self.pic
                        return "bad"
                    self.lzw, self.ext = None, False
                    continue
                if self.pos + 1 + n > limit:
                    return "need"
                if self.lzw is not None:
                    if not self.lzw.feed(read(self.pos + 1, n)) or self.lzw.count > self.want:
                        self.unit = max(self.pic, self.pos - self.grain)
                        return "bad"
                else:
                    self.weak.append((self.pos + 1, self.pos + 1 + n))
                self.pos += 1 + n
                continue
            self.unit = self.pos
            if self.pos >= limit:
                return "need"
            b = read(self.pos, 1)[0]
            if b == 0x3B:
                self.end = self.pos + 1
                return "end"
            if b == 0x21:
                self.pos += 2
                self.ext = True
                continue
            if b != 0x2C:
                return "bad"
            if self.pos + 11 > limit:
                return "need"
            d = read(self.pos, 11)
            w, h = struct.unpack("<HH", d[5:9])
            lf = d[9]
            self.pic = self.pos
            self.pos += 10 + (3 * (2 << (lf & 7)) if lf & 0x80 else 0)
            if self.pos >= limit:
                return "need"
            try:
                self.lzw = _Lzw(read(self.pos, 1)[0])
            except ValueError:
                return "bad"
            self.want = w * h
            self.pos += 1

    def proven_end(self):
        return self.end is not None


WALKERS = {"mp3": _Mp3, "ts": _Ts, "ogg": _Ogg, "flac": _Flac, "gzip": _Gzip, "zip": _Zip, "pdf": _Pdf,
           "riff": _Avi, "gif": _Gif}


# ------------------------------------------------------------------ the search
class _Rebuild:
    def __init__(self, src, geom, blocked: bytearray, kind: str, start: int, expected=None, soft=None):
        self.src, self.g, self.blocked, self.kind = src, geom, blocked, kind
        self.soft = soft  # clusters of files saved but not proven: tried only after the free ones
        self.trials = 0
        self.cs, self.lastc = geom.cluster_size, geom.last_cluster
        self.path = [start]
        self.vf = _Path(src, geom, self.path)
        self.expected = expected
        self.w = WALKERS[kind](self.vf.read, expected)
        self.w.grain = self.cs
        self.pieces_tried = 0
        self.ambiguous = False
        self._plaus: dict = {}

    def limit(self):
        n = len(self.path) * self.cs
        return min(n, self.expected) if self.expected else n

    def next_free(self, c, taken, skip_soft=False):
        while True:
            n = self.blocked.find(0, c + 1, self.lastc + 1)
            if n < 0:
                return None
            if n not in taken and self.plausible(n) and not (skip_soft and self.soft is not None and self.soft[n]):
                return n
            c = n

    def plausible(self, c) -> bool:
        """Could this cluster be part of a recording or archive? An empty one
        can't, and neither can JPEG picture data (every 0xFF in it is followed
        by 0x00 or a restart marker), the most common leftover on a card."""
        v = self._plaus.get(c)
        if v is None:
            data = self.src.read(self.g.offset(c), self.cs)
            if data.count(0) == len(data):
                v = False
            elif getattr(self.w, "any_cluster", False):
                v = True
            else:
                ffs = data.count(b"\xff")
                ok_pairs = data.count(b"\xff\x00") + sum(data.count(bytes([0xFF, m])) for m in range(0xD0, 0xD8))
                v = not (ffs >= 6 and ok_pairs >= ffs - 1)
            self._plaus[c] = v
        return v

    def _prefix(self, n):
        return self.path[:(n + self.cs - 1) // self.cs], n

    def run(self):
        """(clusters, length, complete) of the rebuilt file, or None. Not
        complete: the part that checked out before the trail went cold."""
        w = self.w
        snap = w.snapshot()
        # The walker's state each time it needed more than the first L clusters:
        # a trial for slot j >= L starts from there instead of re-checking
        # everything since the last fix (the state depends only on those L).
        self.snaps: dict = {}
        while True:
            st = w.advance(self.limit())
            if st == "end":
                self._reach(w.end)
                return self._prefix(w.end) + (True,)
            if st == "need" and self.expected and len(self.path) * self.cs >= self.expected:
                st = "bad"  # all the clusters its size calls for, and still not finished: one is wrong
            if st == "need":
                self.snaps[len(self.path)] = w.snapshot()
                self.snaps.pop(len(self.path) - KEEP_STATES, None)
                c = self.next_free(self.path[-1], set(self.path))
                if c is None:
                    return self._prefix(w.unit) + (False,)
                self.path.append(c)
                continue
            # 'bad'. For a stream without an end marker, stopping inside the
            # cluster that holds the last good header is the real end: the
            # file can't go on elsewhere from the middle of a cluster.
            if not w.strong and self.expected is None and w.pos > w.unit and \
                    w.unit // self.cs == (w.pos + w.need - 1) // self.cs:
                w.end = w.pos
                return self._prefix(w.pos) + (True,)
            self.pieces_tried += 1
            if self.pieces_tried > MAX_PIECES:
                return self._prefix(w.unit) + (False,)
            u = w.unit
            first = u // self.cs if u % self.cs == 0 else u // self.cs + 1
            first = max(first, 1)
            fix = self._search(first, u, snap, max(SPAN, w.horizon))
            if fix is None:
                return self._prefix(w.unit) + (False,)
            # the slot that changed (the trial may have run on past it): later states are stale
            j = next((i for i, (a, b) in enumerate(zip(fix, self.path)) if a != b), min(len(fix), len(self.path)))
            for L in [L for L in self.snaps if L > j]:
                del self.snaps[L]
            self.path[:] = fix
            w.restore(self._base(j, snap))
            snap = w.snapshot()
            continue

    def _reach(self, end: int):
        """Take clusters up to `end` when the file ends past the ones checked:
        an MP3's tag can run into the next cluster, and its text (zeros, often)
        can't be checked, so the next free cluster is taken even if empty."""
        taken = set(self.path)
        while len(self.path) * self.cs < end:
            c = self.path[-1]
            while True:
                c = self.blocked.find(0, c + 1, self.lastc + 1)
                if c < 0 or c not in taken:
                    break
            if c < 0:
                return
            self.path.append(c)
            taken.add(c)

    def _base(self, j: int, snap):
        """The latest saved state that only depends on the clusters before slot j."""
        L = max((L for L in self.snaps if L <= j), default=None)
        return self.snaps[L] if L is not None else snap

    def _search(self, first: int, u: int, snap, span: int = SPAN):
        """Try the next piece starting at each slot the failing unit covers."""
        slots = range(first, min(len(self.path), first + span))
        for skip_soft in ((True, False) if self.soft is not None else (False,)):
            # Round robin over the slots: the k-th candidate for every slot
            # before the (k+1)-th for any, so a break in a later slot is found
            # without first exhausting the earlier ones.
            state = {j: self.path[j - 1] for j in slots}
            taken = {j: set(self.path[:j]) for j in slots}
            live = list(slots)
            for _k in range(MAX_SCAN):
                if not live or self.trials >= MAX_TRIALS:
                    break
                for j in list(live):
                    prefix = self.path[:j]
                    c = self.next_free(state[j], taken[j], skip_soft)
                    while c is not None and (c == self.path[j] or (not skip_soft and self.soft is not None
                                                                     and not self.soft[c])):
                        c = self.next_free(c, taken[j], skip_soft)  # what failed, or tried in round one
                    if c is None:
                        live.remove(j)
                        continue
                    state[j] = c
                    self.trials += 1
                    trial = prefix + [c]
                    if self._passes(trial, j, snap, span):
                        return trial
        return None

    def _passes(self, trial, j, snap, span: int = SPAN) -> bool:
        """Does the file check out through cluster j of `trial` (adding the
        next free clusters if a unit runs on, up to `span` of them)?"""
        w = self.w
        saved = self.path[:]
        self.path[:] = trial
        try:
            w.restore(self._base(j, snap))
            goal = (j + 1) * self.cs
            taken = set(trial)
            for _ in range(span + 1):
                st = w.advance(self.limit())
                if st == "bad":
                    return False
                if st == "end":
                    return w.end > j * self.cs
                if w.unit >= goal or (w.unit > j * self.cs and not w.strong and w.unit + 8 >= goal):
                    return True
                if self.expected and len(self.path) * self.cs >= self.expected:
                    return False
                c = self.next_free(self.path[-1], taken)
                if c is None:
                    return False
                self.path.append(c)
                taken.add(c)
            return False
        finally:
            trial[:] = self.path[:]
            self.path[:] = saved


def rb_start_marked(src, geom, clusters, length, kind) -> bool:
    """Does the rebuilt file mark its own beginning (an MP3 tag or length
    header, a transport stream whose counters start at 0)?"""
    w = WALKERS[kind](_Path(src, geom, clusters).read)
    w.advance(min(length, 256 * 192))
    return w.start_marked


def reassemble_streams(src, geom, blocked: bytearray, records, own, progress=None, soft=None) -> int:
    from .reassemble import own_clusters
    done = 0
    for r in records:
        start = geom.cluster_at(r.start)
        was = blocked[start]
        blocked[start] = 1
        rb = None
        try:
            with own_clusters(geom, soft, r):
                rb = _Rebuild(src, geom, blocked, r.kind, start, r.expected_size, soft)
                out = rb.run()
        except Exception:
            out = None
        saved = (r.extents, r.status, r.note, r.origin, dict(r.meta))
        try:
            ok = _settle(src, geom, blocked, r, rb, out, start, was, own)
        except Exception:
            # one file's trouble never stops the others' rebuilds: put it back as it was
            r.extents, r.status, r.note, r.origin = saved[:4]
            r.meta.clear()
            r.meta.update(saved[4])
            blocked[start] = was
            ok = False
        if ok:
            done += 1
        if progress:
            progress(done, len(records))
    return done


def _first_choice(rb, clusters) -> bool:
    """Does every join go on in the first cluster the file could: the next one
    after the piece before that isn't in use, another file's or empty? Then no
    search chose it among others that also fit."""
    for i in range(1, len(clusters)):
        if clusters[i] != clusters[i - 1] + 1 and clusters[i] != rb.next_free(clusters[i - 1], set(clusters[:i])):
            return False
    return True


def _settle(src, geom, blocked, r, rb, out, start, was, own) -> bool:
    """Apply a finished rebuild to the record; True if it is now recovered."""
    if not out or not out[1]:
        blocked[start] = was
        return False
    clusters, length, complete = out
    from .reassemble import _Assembler, _extents
    pieces = _Assembler._pieces(clusters)
    if not complete:
        # the trail went cold: keep it only if more of the file checked out than before
        if r.status in ("partial", "corrupt") and length > r.size:
            r.extents = _extents(geom, clusters, length)
            r.status, r.note = "partial", "the rest of it could not be found"
            if pieces > 1:
                r.meta["reassembled_from"] = pieces
        blocked[start] = was
        return False
    w = WALKERS[r.kind]
    final = getattr(w, "final", None)
    if final is not None:
        # a last look at the whole file (a PDF's table and every stream's checksum)
        st, why, _bad = final(_Path(src, geom, clusters).read, length)
        if st == "corrupt":
            blocked[start] = was
            return False
    r.extents = _extents(geom, clusters, length)
    sized = r.expected_size is not None and length == r.expected_size
    cs = geom.cluster_size
    seams = [i * cs for i in range(1, len(clusters)) if clusters[i] != clusters[i - 1] + 1]
    weak = [(a, b) for a, b in getattr(rb.w, "weak", ()) if any(a < x < b for x in seams)]
    if w.strong and weak:
        r.status = "unverified"
        r.note = ("rebuilt from %d pieces, but a join falls in data nothing in it can check; "
                  "check that it plays" % pieces)
    elif w.strong and final is not None and st != "ok":
        r.status, r.note = "unverified", why  # (parts of it can't be checked: say which)
    elif w.strong:
        r.status, r.note = "ok", "every piece checked"
    elif seams and not (rb.w.tied(seams) and (not rb.w.first_choice or _first_choice(rb, clusters))):
        # Frames of another recording of the same kind pass these checks, so a
        # join is only certain once the recording's own clock (a transport
        # stream) carries on across it, or for MP3, its bit reservoir does and
        # the join skips nothing but clusters known to be other files'.
        r.status = "unverified"
        r.note = ("rebuilt from %d pieces, but nothing in this file type ties every piece to it; "
                  "check that it plays" % pieces)
    elif rb.w.unit // cs != (length - 1) // cs:
        # the last frame's body runs into a cluster nothing else confirms
        r.status = "unverified"
        r.note = "rebuilt from %d pieces, but its last frame can't be confirmed; check that it plays" % pieces
    elif sized:
        r.status, r.note = "ok", "every piece checked"
    elif r.origin == "carved" and not rb_start_marked(src, geom, clusters, length, r.kind):
        r.status, r.note = "unverified", more.NO_START
    else:
        r.status, r.note = "ok", "every frame checked; it ends inside a cluster, so nothing is missing"
    if pieces > 1:
        r.meta["reassembled_from"] = pieces
        if r.origin == "carved":
            r.origin = "reassembled"
    if r.status == "ok":
        own(r)
        return True
    blocked[start] = was
    return False
