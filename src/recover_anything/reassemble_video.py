"""Put fragmented MP4/MOV videos back together.

A video file carries its own map: the 'moov' index lists the exact byte offset
and size of every frame. Each frame of H.264/HEVC video starts with a 4-byte
length followed by a header byte with strict rules, so every listed frame start
is a checkpoint: random data passes it about once in several thousand tries.

  1. Walk the boxes at the start of the file to find the media data ('mdat')
     and therefore where the index ('moov') must begin.
  2. If the index sits at the end (what cameras and phones write), search the
     free clusters for a 'moov' box at exactly that offset within a cluster.
  3. Lay the frames out cluster by cluster, skipping clusters that belong to
     other verified files. Whenever a checkpoint fails, try the free clusters
     that could start the next piece and keep the one that satisfies this
     checkpoint and the next few.
  4. Only a file whose every frame start checks out is marked recovered.
"""
from __future__ import annotations

import struct

from .formats import others

MAX_MOOV = 64 << 20
CANDIDATES = 4000
LOOKAHEAD = 3


def _nal_ok(h: bytes, size: int, hevc: bool) -> bool:
    if len(h) < 5:
        return False
    ln = struct.unpack(">I", h[:4])[0]
    hdr = h[4]
    if ln == 0 or ln + 4 > size or hdr & 0x80:
        return False
    if hevc:
        return ((hdr >> 1) & 0x3F) <= 40
    return (hdr & 0x1F) not in (0, 24, 25, 26, 27, 28, 29, 30, 31)


def _samples(moov: bytes, base: int):
    """[(file offset, size, hevc)] for every H.264/HEVC sample in a moov box,
    or None if the index is damaged or implausibly large."""
    read = lambda o, n: moov[max(0, o - base):max(0, o - base + n)]  # noqa: E731
    out = []
    end = base + len(moov)
    for typ, b, e in others._boxes(read, base + 8, end):
        if typ != b"trak":
            continue
        stbl = others._find(read, b, e, [b"mdia", b"minf", b"stbl"])
        if not stbl:
            continue
        if others.track_codec(read, stbl) not in others.NAL_CODECS:
            continue  # audio and other tracks have no checkpoints
        try:
            tables = others.sample_tables(read, stbl)
        except others.TooManySamples:
            return None
        if tables is None:
            return None
        codec, offsets, sizes, stsc = tables
        if not offsets or not sizes:
            continue
        if len(out) + len(sizes) > others.MAX_SAMPLES:
            return None
        hevc = codec in (b"hvc1", b"hev1")
        # A frame smaller than a length prefix plus a header byte can't be
        # checked, so it is no use as a checkpoint.
        out.extend((pos, sz, hevc) for pos, sz in others.iter_samples(offsets, sizes, stsc) if sz >= 5)
    out.sort()
    return out


class _Video:
    def __init__(self, src, geom, blocked: bytearray, start: int):
        self.src, self.g, self.blocked, self.start = src, geom, blocked, start
        self.cs = geom.cluster_size
        self.last = geom.last_cluster
        self._plaus: dict = {}
        self.file_len = 0  # set once the index says how long the file is
        self.ambiguous = False

    def next_free(self, c, skip, index=None, used=None):
        """The next unclaimed cluster after c that could hold this video's
        bytes at file cluster `index` (only the part the file uses there is
        judged: a last cluster is mostly leftover data from older files)."""
        if used is None and index is not None and self.file_len:
            used = self.file_len - index * self.cs
        while True:
            n = self.blocked.find(0, c + 1, self.last + 1)
            if n < 0:
                return None
            if n not in skip and self.plausible(n, used):
                return n
            c = n

    def plausible(self, c, used=None) -> bool:
        """Could this cluster be part of a video? Empty clusters can't, and
        neither can JPEG image data (every 0xFF in it is followed by 0x00 or a
        restart marker), which is what most leftover data on a camera card is."""
        used = self.cs if used is None or used >= self.cs else max(0, used)
        key = (c, used)
        v = self._plaus.get(key)
        if v is None:
            data = self.src.read(self.g.offset(c), used)
            if data.count(0) == len(data):
                v = used < self.cs  # a file's last few bytes may well be zeros
            elif used < 1024:
                v = True  # too little to judge
            else:
                ffs = data.count(b"\xff")
                ok_pairs = data.count(b"\xff\x00") + sum(data.count(bytes([0xFF, m])) for m in range(0xD0, 0xD8))
                v = not (ffs >= 6 and ok_pairs >= ffs - 1)
            self._plaus[key] = v
        return v

    def extend(self, path, upto, skip):
        while len(path) <= upto:
            c = self.next_free(path[-1], skip | set(path), index=len(path))
            if c is None:
                return False
            path.append(c)
        return True

    def vread(self, path, off, n):
        cs, out = self.cs, []
        while n > 0:
            i = off // cs
            if i >= len(path):
                break
            o = off % cs
            take = min(n, cs - o)
            out.append(self.src.read(self.g.offset(path[i]) + o, take))
            off += take
            n -= take
        return b"".join(out)

    def run(self, expected_size=None):
        cs = self.cs
        path = [self.start]
        off, mdat, moov_box = 0, None, None
        for _ in range(64):
            if not self.extend(path, (off + 16) // cs, set()):
                return None
            h = self.vread(path, off, 16)
            if len(h) < 8 or h[4:8] not in others.TOP_LEVEL:
                break
            size = struct.unpack(">I", h[:4])[0]
            if size == 1:
                size = struct.unpack(">Q", h[8:16])[0]
            if size < 8:
                return None
            if h[4:8] == b"moov":
                moov_box = (off, size)
            if h[4:8] == b"mdat":
                mdat = (off, size)
                break
            off += size
        if mdat is None:
            return None
        v_end = mdat[0] + mdat[1]
        fixed: dict[int, int] = {}
        if moov_box:
            if moov_box[1] > MAX_MOOV or not self.extend(path, (moov_box[0] + moov_box[1]) // cs, set()):
                return None
            moov = self.vread(path, *moov_box)
            samples = _samples(moov, moov_box[0])
            tail_start = v_end
            self.file_len = v_end
        else:
            found = self._find_moov(path, v_end)
            if not found:
                return None
            moov_path, moov, samples = found
            pv = v_end // cs
            for k, c in enumerate(moov_path):
                fixed[pv + k] = c
            moov_box = (v_end, len(moov))
            tail_start = v_end + len(moov)
            self.file_len = tail_start
        if not samples:
            return None
        checks = [s for s in samples if s[0] < v_end]
        path = self._lay_out(path, checks, fixed)
        if path is None:
            return None
        total = tail_start
        full = self._materialize(path, fixed, (total - 1) // cs)
        if full is None:
            return None
        # trailing boxes after the index ('free', 'udta', ...), if any
        for _ in range(8):
            self.file_len = 0  # whatever follows is judged as a whole cluster
            probe = self._materialize(full, fixed, (total + 7) // cs)
            if probe is None:
                break
            h = self.vread(probe, total, 8)
            if len(h) < 8 or h[4:8] not in others.TOP_LEVEL or h[4:8] in (b"mdat", b"ftyp"):
                break
            size = struct.unpack(">I", h[:4])[0]
            if size < 8 or size > MAX_MOOV:
                break
            self.file_len = total + size
            nxt = self._materialize(probe, fixed, (total + size - 1) // cs)
            if nxt is None:
                break
            total += size
            full = nxt
        n = (total + cs - 1) // cs
        full = full[:n]
        st, why, _ = others.validate_isobmff(lambda o, k: self.vread(full, o, k), total)
        if st != "ok":
            return None
        return full, total, self.ambiguous

    def _materialize(self, path, fixed, upto):
        full = list(path)
        while len(full) <= upto:
            i = len(full)
            if i in fixed:
                full.append(fixed[i])
                continue
            c = self.next_free(full[-1], set(full) | set(fixed.values()), index=i)
            if c is None:
                return None
            full.append(c)
        return full

    def _find_moov(self, path, v_end):
        cs = self.cs
        inoff = v_end % cs
        tried = set()
        cands = []
        probe = list(path)
        if self.extend(probe, v_end // cs, set()):
            cands.append(probe[v_end // cs])
        c, n = self.start, 0
        while n < 200000:
            c = self.next_free(c, set())
            if c is None:
                break
            n += 1
            h = self.src.read(self.g.offset(c) + inoff, 8)
            if h[4:8] == b"moov":
                cands.append(c)
        for c in cands:
            if c in tried:
                continue
            tried.add(c)
            h = self.src.read(self.g.offset(c) + inoff, 8)
            if h[4:8] != b"moov":
                continue
            size = struct.unpack(">I", h[:4])[0]
            if not 8 < size <= MAX_MOOV:
                continue
            mp = [c]
            need = (inoff + size - 1) // cs
            ok = True
            while len(mp) <= need:
                nxt = self.next_free(mp[-1], set(mp) | {self.start}, used=inoff + size - len(mp) * cs)
                if nxt is None:
                    ok = False
                    break
                mp.append(nxt)
            if not ok:
                continue
            data = b"".join(self.src.read(self.g.offset(x), cs) for x in mp)[inoff:inoff + size]
            samples = _samples(data, v_end)
            if samples and all(o + s <= v_end for o, s, _ in samples) and samples[0][0] < cs * 4 + v_end:
                return mp, data, samples
        return None

    def _check(self, path, fixed, sample):
        o, size, hevc = sample
        cs = self.cs
        need = (o + 5) // cs
        full = path
        if len(path) <= need:
            full = list(path)
            while len(full) <= need:
                i = len(full)
                if i in fixed:
                    full.append(fixed[i])
                else:
                    c = self.next_free(full[-1], set(full) | set(fixed.values()), index=i)
                    if c is None:
                        return False
                    full.append(c)
        return _nal_ok(self.vread(full, o, 5), size, hevc)

    def _lay_out(self, path, checks, fixed):
        cs = self.cs
        fixed_set = set(fixed.values())
        last_ok = 0
        i = 0
        repairs: dict[int, int] = {}
        budget = 20 * len(checks) + 100
        while i < len(checks):
            budget -= 1
            if budget < 0:
                return None  # not converging: give up rather than spin
            o = checks[i][0]
            pos = o // cs
            if pos in fixed:
                path = self._materialize(path, fixed, pos)
                if path is None:
                    return None
                path = path[:min(len(path), min(fixed))]
            if self._check(path, fixed, checks[i]):
                last_ok = max(last_ok, pos)
                i += 1
                continue
            repairs[i] = repairs.get(i, 0) + 1
            if repairs[i] > 3:
                return None  # the same checkpoint keeps failing: the evidence isn't there
            fixed_path = self._repair(path, fixed, fixed_set, checks, i, last_ok)
            if fixed_path is None:
                return None
            if fixed_path == path:
                return None
            path = fixed_path
        return path

    def _repair(self, path, fixed, fixed_set, checks, i, last_ok):
        """The checkpoint checks[i] failed: the next piece starts somewhere in
        positions (last_ok, pos]. Search break positions latest-first and
        earliest-first; if the two explanations disagree, the clusters between
        checkpoints can't be confirmed and the video is flagged."""
        cs = self.cs
        pos = (checks[i][0] + 4) // cs
        lo = max(1, last_ok + 1) if last_ok else 1
        late = self._search(path, fixed, fixed_set, checks, i, range(pos, lo - 1, -1))
        early = self._search(path, fixed, fixed_set, checks, i, range(lo, pos + 1))
        if late is None:
            return early
        if early is not None:
            a = self._materialize(late, fixed, pos)
            b = self._materialize(early, fixed, pos)
            if a is None or b is None or a[:pos + 1] != b[:pos + 1]:
                self.ambiguous = True
        return late

    def _search(self, path, fixed, fixed_set, checks, i, order):
        cs = self.cs
        for j in order:
            if j in fixed or j == 0:
                continue
            base = path[:j]
            if len(base) < j:
                base = list(path)
                if not self.extend(base, j - 1, fixed_set):
                    continue
                base = base[:j]
            skip = set(base) | fixed_set
            c, tried = base[-1], 0
            while tried < CANDIDATES:
                c = self.next_free(c, skip, index=j)
                if c is None:
                    break
                tried += 1
                trial = base + [c]
                good = True
                for k in range(i, min(len(checks), i + 1 + LOOKAHEAD)):
                    if checks[k][0] // cs in fixed and checks[k][0] // cs > j:
                        break
                    if not self._check(trial, fixed, checks[k]):
                        good = False
                        break
                if good:
                    return trial
        return None


def reassemble_videos(src, geom, blocked: bytearray, records, own, progress=None) -> int:
    done = 0
    for r in records:
        start = geom.cluster_at(r.start)
        blocked[start] = 0
        try:
            out = _Video(src, geom, blocked, start).run(r.expected_size)
        except Exception:
            out = None
        blocked[start] = 1
        if not out:
            continue
        clusters, length, ambiguous = out
        from .reassemble import _Assembler, _extents
        r.extents = _extents(geom, clusters, length)
        if ambiguous:
            r.status = "unverified"
            r.note = "rebuilt, but some pieces could not be confirmed; check that it plays"
        else:
            r.status = "ok"
            r.note = "every frame position checked"
        pieces = _Assembler._pieces(clusters)
        if pieces > 1:
            r.meta["reassembled_from"] = pieces
            if r.origin == "carved":
                r.origin = "reassembled"
        if r.status == "ok":
            own(r)
        done += 1
    return done
