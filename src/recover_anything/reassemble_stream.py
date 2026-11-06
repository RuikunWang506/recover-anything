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
    need = 1        # bytes of the next unit needed to tell it isn't there

    def __init__(self, read, expected=None):
        self.read, self.expected = read, expected
        self.unit = 0
        self.end = None
        self.start_marked = True

    def snapshot(self):
        # sets, dicts and lists (open streams, packet counters) are copied:
        # the walker changes them in place as it goes
        return {k: (v.copy() if isinstance(v, (set, dict, list)) else v) for k, v in self.__dict__.items()}

    def restore(self, snap):
        self.__dict__.update({k: (v.copy() if isinstance(v, (set, dict, list)) else v) for k, v in snap.items()})


class _Mp3(_Walker):
    need = 4

    def __init__(self, read, expected=None):
        super().__init__(read, expected)
        h = read(0, 10)
        self.pos = 10 + more._synchsafe(h[6:10]) + (10 if h[5] & 0x10 else 0) if h[:3] == b"ID3" else 0
        self.tagged = h[:3] == b"ID3"
        self.first_at, self.key, self.declared, self.last = self.pos, None, None, None
        self.start_marked = self.tagged

    def advance(self, limit):
        read = self.read
        while True:
            if self.expected is not None and self.pos in (self.expected, self.expected - 128):
                if self.pos == self.expected or read(self.pos, 3) == b"TAG":
                    self.end = self.expected
                    return "end"
            if self.declared is not None and self.pos - self.first_at == self.declared:
                self.end = self.pos + (128 if read(self.pos, 3) == b"TAG" and self.pos + 128 <= limit else 0)
                return "end"
            if self.pos + 4 > limit:
                return "need"
            f = more.mp3_frame(read(self.pos, 4))
            if f is None or (self.key is not None and f[1] != self.key):
                self.unit = self.last if self.last is not None else self.pos
                return "bad"
            n, k = f
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
            if bad:
                self.unit = max(0, self.pos - stride)
                return "bad"
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


WALKERS = {"mp3": _Mp3, "ts": _Ts, "ogg": _Ogg, "flac": _Flac, "gzip": _Gzip}


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
        while True:
            st = w.advance(self.limit())
            if st == "end":
                return self._prefix(w.end) + (True,)
            if st == "need" and self.expected and len(self.path) * self.cs >= self.expected:
                st = "bad"  # all the clusters its size calls for, and still not finished: one is wrong
            if st == "need":
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
            fix = self._search(first, u, snap)
            if fix is None:
                return self._prefix(w.unit) + (False,)
            self.path[:] = fix
            w.restore(snap)
            snap = w.snapshot()
            continue

    def _search(self, first: int, u: int, snap):
        """Try the next piece starting at each slot the failing unit covers."""
        slots = range(first, min(len(self.path), first + SPAN))
        for skip_soft in ((True, False) if self.soft is not None else (False,)):
            # Round robin over the slots: the k-th candidate for every slot
            # before the (k+1)-th for any, so a break in a later slot is found
            # without first exhausting the earlier ones.
            state = {j: self.path[j - 1] for j in slots}
            live = list(slots)
            for _k in range(MAX_SCAN):
                if not live or self.trials >= MAX_TRIALS:
                    break
                for j in list(live):
                    prefix = self.path[:j]
                    c = self.next_free(state[j], set(prefix), skip_soft)
                    while c is not None and (c == self.path[j] or (not skip_soft and self.soft is not None
                                                                     and not self.soft[c])):
                        c = self.next_free(c, set(prefix), skip_soft)  # what failed, or tried in round one
                    if c is None:
                        live.remove(j)
                        continue
                    state[j] = c
                    self.trials += 1
                    trial = prefix + [c]
                    if self._passes(trial, j, snap):
                        return trial
        return None

    def _passes(self, trial, j, snap) -> bool:
        """Does the file check out through cluster j of `trial` (adding the
        next free clusters if a unit runs on)?"""
        w = self.w
        saved = self.path[:]
        self.path[:] = trial
        try:
            w.restore(snap)
            goal = (j + 1) * self.cs
            taken = set(trial)
            for _ in range(SPAN + 1):
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
        if not out or not out[1]:
            blocked[start] = was
            continue
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
            continue
        r.extents = _extents(geom, clusters, length)
        w = WALKERS[r.kind]
        sized = r.expected_size is not None and length == r.expected_size
        cs = geom.cluster_size
        if w.strong:
            r.status, r.note = "ok", "every piece checked"
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
            done += 1
        else:
            blocked[start] = was
        if progress:
            progress(done, len(records))
    return done
