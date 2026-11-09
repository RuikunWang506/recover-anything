"""Put fragmented JPEGs back together.

Why photos fragment: a camera deletes a photo, leaving a hole; the next photo is
bigger than the hole, so the camera fills the hole and continues in the next
free space. After a format the map of which clusters belong to which file is
gone, so ordinary carvers glue the first piece to whatever follows and get a
broken picture.

How we fix it, cluster by cluster:
  1. Skip clusters we already know belong to other verified files ("gap
     skipping"). The camera allocated free clusters in order, so the next
     piece is usually the next cluster nobody else owns.
  2. Decode as we go. When the decoder hits an impossibility (bad Huffman
     code, coefficient overflow, restart marker out of order, wrong number of
     blocks at the end), the bytes stopped belonging to this photo.
  3. At the suspected break, try candidate clusters in order and score each by
     how smoothly the picture continues across the seam (block brightness
     against the row above). The right piece continues the image; the wrong
     piece jumps.
  4. Repeat until the decoder reaches the end of the image with exactly the
     right number of blocks.
"""
from __future__ import annotations

from .formats import jpeg
from .records import Found

MAX_SCAN = 6000       # free clusters to look at after a break
MAX_QUICK = 400       # candidates that survive the first few KB
QUICK_BYTES = 4096
FULL_TRIALS = 16
ACCEPT_SCORE = 2.5     # seam roughness / normal roughness that is always fine
LONE_SCORE = 10.0      # a lone (or clearly best) clean candidate is accepted up to this
GOOD_ENOUGH = 1.4


def _row_jump(dec: "jpeg.Decoder", a: int, b: int) -> float:
    """Mean |DC - DC above| over the top luma block-row of MCUs [a, b)."""
    bw, bh, bv, mw = dec.bw, dec.bh, dec.bv, dec.mcu_w
    dc = dec.dc
    tot = n = 0
    for m in range(max(a, mw), b):
        my, mx = divmod(m, mw)
        base = (my * bv) * bw + mx * bh
        for hz in range(bh):
            i = base + hz
            tot += abs(dc[i] - dc[i - bw])
            n += 1
    return tot / n if n else 0.0


def seam_score(dec, m0: int, m1: int) -> float | None:
    """How rough the picture is right after MCU m0 compared with just before."""
    mw = dec.mcu_w
    if m0 < 2 * mw:
        return None
    after = _row_jump(dec, m0, min(m1, m0 + mw))
    before = _row_jump(dec, m0 - mw, m0)
    if after == 0 and before == 0:
        return 1.0
    return after / max(before, 1.0)


class _Assembler:
    def __init__(self, src, geom, blocked: bytearray):
        self.src, self.g, self.blocked = src, geom, blocked
        self.cs = geom.cluster_size
        self.last = geom.last_cluster

    def cluster(self, c: int) -> bytes:
        return self.src.read(self.g.offset(c), self.cs)

    def next_free(self, c: int, skip=()) -> int | None:
        while True:
            n = self.blocked.find(0, c + 1, self.last + 1)
            if n < 0:
                return None
            if n not in skip:
                return n
            c = n

    def run(self, start: int, expected_size: int | None = None):
        """Returns (clusters, length, status, note)."""
        cs = self.cs
        try:
            h = jpeg.parse_header(self.src.read, self.g.offset(start))
        except jpeg.JpegError:
            return None
        if not h.decodable:
            return None
        try:
            dec = jpeg.Decoder(h)
        except jpeg.JpegError:
            return None
        thumb = None
        if h.thumb:
            try:
                thumb = jpeg.Thumb(h, self.src.read(self.g.offset(start) + h.thumb[0], h.thumb[1]))
            except Exception:
                thumb = None  # no usable thumbnail: the decoder's checks are all there is
        cap = None  # set when the picture stops matching its thumbnail at this MCU
        max_clusters = (expected_size + cs - 1) // cs + 2 if expected_size else (200 << 20) // cs
        path = [start]
        buf = bytearray(self.cluster(start))
        dec.buf = bytes(buf)
        states: dict[int, tuple] = {}
        tried: dict[int, set] = {}
        mine = {start}
        breaks = 0
        while True:
            try:
                st = dec.run()
            except jpeg.DecodeError as e:
                breaks += 1
                if breaks > 64:
                    break
                fixed = self._fix(dec, e.pos, path, buf, states, tried, mine)
                if fixed is None:
                    break
                path, buf = fixed
                continue
            if st == "done":
                bad = self._unlike_thumb(thumb, dec, path)
                if bad is None:
                    return path, dec.end, "ok", "%d pieces" % self._pieces(path)
                # Every block decodes, but an area of the picture isn't what its
                # own thumbnail shows: a piece of another photo got in, somewhere
                # in the clusters that area was decoded from.
                breaks += 1
                cap = bad[0] if cap is None else min(cap, bad[0])
                if breaks > 64:
                    break
                lo = next((j for j in sorted(states) if states[j][5] > bad[0]), len(path) - 1)
                k = next((j for j in sorted(states) if states[j][5] > bad[1]), len(path) - 1)
                seams = [j for j in range(k, 0, -1) if path[j] != path[j - 1] + 1]
                extra = list(range(k, max(0, lo - 1), -1))[:12] + seams[:8]
                fixed = self._fix(dec, k * cs, path, buf, states, tried, mine, extra=extra)
                if fixed is None:
                    break
                path, buf = fixed
                continue
            states[len(path) - 1] = dec.save()
            if len(path) >= max_clusters:
                break
            nxt = self.next_free(path[-1], skip=mine | tried.get(len(path), set()))
            if nxt is None:
                break
            path.append(nxt)
            mine.add(nxt)
            buf += self.cluster(nxt)
            dec.buf = bytes(buf)
        # could not finish: keep what decodes (the top of the picture), and
        # nothing from where it stopped matching its thumbnail
        good = max((k for k in states if k < len(path) and (cap is None or states[k][5] <= cap)), default=None)
        if good is None:
            return None
        dec.restore(states[good])
        end = dec.pos
        return path[:good + 1], end, "partial", "only the top part of the picture could be recovered"

    def _unlike_thumb(self, thumb, dec, path):
        """The MCUs of the first area where a rebuilt picture doesn't match its
        thumbnail, or None."""
        if thumb is None or self._pieces(path) == 1:
            return None
        try:
            return thumb.unlike(dec)
        except Exception:
            return None

    @staticmethod
    def _pieces(path):
        return 1 + sum(1 for a, b in zip(path, path[1:]) if b != a + 1)

    def _suspects(self, dec, path, states, k):
        """Seams before k ranked by how badly the picture jumps there."""
        scored = []
        for j in range(1, k + 1):
            if j - 1 not in states:
                continue
            m0 = states[j - 1][5]
            m1 = states[j][5] if j in states else dec.mcu
            s = seam_score(dec, m0, m1)
            if s is not None:
                scored.append((s, j))
        scored.sort(reverse=True)
        return [j for s, j in scored if s > ACCEPT_SCORE][:4]

    def _fix(self, dec, err_pos, path, buf, states, tried, mine, extra=()):
        cs = self.cs
        k = min(len(path) - 1, max(1, err_pos // cs))
        order = []
        for j in self._suspects(dec, path, states, k) + [k, k - 1, k - 2] + list(extra):
            if 1 <= j < len(path) and j not in order:
                order.append(j)
        for j in order:
            if j - 1 not in states:
                continue
            tried.setdefault(j, set()).add(path[j])
            got = self._search(dec, path, buf, states, j, tried[j], mine)
            if got is not None:
                c = got
                for x in path[j:]:
                    mine.discard(x)
                new_path = path[:j] + [c]
                mine.add(c)
                new_buf = bytearray(buf[:j * cs])
                new_buf += self.cluster(c)
                for s in [s for s in states if s >= j]:
                    del states[s]
                for t in [t for t in tried if t > j]:
                    del tried[t]
                dec.restore(states[j - 1])
                dec.buf = bytes(new_buf)
                return new_path, new_buf
        return None

    def _trial(self, dec, base, prefix, data, m0, nbytes):
        """Decode `nbytes` of a candidate after the prefix. Returns
        (score, finished) or None if the candidate is impossible."""
        dec.restore(base)
        dec.buf = prefix + data[:nbytes]
        try:
            st = dec.run()
        except jpeg.DecodeError as e:
            if e.pos < len(prefix) + min(nbytes, len(data)) - 64:
                return None
            st = "more"
        if st == "done":
            return 0.0, True
        if dec.mcu <= m0:
            return 1.0, False
        s = seam_score(dec, m0, dec.mcu)
        return (1.0 if s is None else s), False

    def _search(self, dec, path, buf, states, j, rejected, mine):
        """Find the cluster that continues the picture after path[j-1].

        Stage 1 tries a few KB of every free cluster in order (cheap: most
        wrong clusters fail within bytes). Stage 2 fully decodes the most
        promising ones and keeps the one with the smoothest seam."""
        base = states[j - 1]
        # Trials decode into the shared block table; put it back afterwards so a
        # failed search leaves no other photo's blocks behind (seam scores and
        # the thumbnail check read it).
        i0 = dec.block_index(base[5])
        saved = dec.dc[i0:]
        try:
            return self._search_from(dec, path, buf, base, j, rejected, mine)
        finally:
            dec.dc[i0:] = saved

    def _search_from(self, dec, path, buf, base, j, rejected, mine):
        cs = self.cs
        prefix = bytes(buf[:j * cs])
        m0 = base[5]
        c = path[j - 1]
        skip = mine | rejected | set(path[:j])
        quick = []
        scanned = 0
        while scanned < MAX_SCAN and len(quick) < MAX_QUICK:
            c = self.next_free(c, skip=skip)
            if c is None:
                break
            scanned += 1
            data = self.cluster(c)
            if data.count(0) == len(data):
                continue
            r = self._trial(dec, base, prefix, data, m0, QUICK_BYTES)
            if r is None:
                continue
            if r[1]:
                dec.restore(base)
                return c
            quick.append((r[0], c, data))
            if r[0] <= GOOD_ENOUGH and len(quick) <= 3:
                full = self._trial(dec, base, prefix, data, m0, cs)
                if full is not None and (full[1] or full[0] <= GOOD_ENOUGH):
                    dec.restore(base)
                    return c
        quick.sort(key=lambda t: t[0])
        survivors = []
        for score, c, data in quick[:FULL_TRIALS]:
            full = self._trial(dec, base, prefix, data, m0, cs)
            if full is None:
                continue
            if full[1]:
                dec.restore(base)
                return c  # reached the end of the image with exactly the right number of blocks
            survivors.append((full[0], c))
        dec.restore(base)
        return _decide(survivors)


def _decide(survivors):
    """Pick a continuation, or refuse when the evidence is ambiguous.

    A wrong cluster usually breaks the decoder outright. When several decode
    cleanly (same encoder settings, so data from another photo is valid
    JPEG data too), the seam decides: the right piece continues the picture.
    The final end-of-image check catches any mistake that slips through.
    """
    if not survivors:
        return None
    survivors.sort()
    best, c = survivors[0]
    if len(survivors) == 1:
        return c if best <= LONE_SCORE else None
    second = survivors[1][0]
    if best <= ACCEPT_SCORE:
        return c
    if best <= LONE_SCORE and second >= 2 * best:
        return c
    return None


def _extents(geom, clusters, length):
    out, rem, i = [], length, 0
    cs = geom.cluster_size
    while i < len(clusters) and rem > 0:
        j = i
        while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1:
            j += 1
        n = min(rem, (j - i + 1) * cs)
        out.append((geom.offset(clusters[i]), n))
        rem -= n
        i = j + 1
    return out


def _or(a: bytearray, b: bytearray) -> bytearray:
    """Byte-wise OR of two 0/1 cluster maps of the same length."""
    n = len(a)
    return bytearray((int.from_bytes(a, "little") | int.from_bytes(b[:n], "little")).to_bytes(n, "little"))


def block_fs_clusters(fsr, blocked: bytearray) -> None:
    """Clusters that can't hold a deleted file's data: the filesystem says
    they are in use right now, or they hold a folder's entries."""
    try:
        alloc = fsr.allocated_map()
        n = min(len(alloc), len(blocked))
        blocked[:n] = _or(blocked[:n], alloc[:n])
    except Exception:
        pass  # a damaged allocation table: rely on what the files themselves prove
    for c in getattr(fsr, "dir_clusters", ()):
        if 0 <= c < len(blocked):
            blocked[c] = 1


def verified_spans(r, cs: int):
    """The parts of a found file that are certainly its own. A length measured
    past a break may cover someone else's data, so a broken file counts only
    up to where its check failed."""
    from .reassemble_stream import WALKERS
    if r.status in ("ok", "unverified"):
        return list(r.extents)
    if r.status in ("partial", "corrupt"):
        if r.kind in WALKERS:
            return list(r.extents)  # a recording's checked prefix
        bad = r.meta.get("bad_at")
        if r.kind in ("jpeg", "isobmff") and bad:
            return _trim(r.extents, max(cs, (int(bad) // cs) * cs))  # the part that decoded or checked out
        return [(r.start, min(cs, r.size))]  # at least its header is its own
    return []


def soft_map(g, recs, size: int) -> bytearray:
    """Clusters that probably belong to other files: ones saved but not proven
    (a HEIC, a WebM), and the verified start of broken ones. Rebuilders try
    them only after every unclaimed cluster."""
    soft = bytearray(size)
    for o in recs:
        if not o.extents or o.status == "ok" or o.meta.get("unsure_layout") or o.start < g.data_offset:
            continue
        for off, n in verified_spans(o, g.cluster_size):
            if n <= 0:
                continue
            c0, c1 = g.cluster_at(off), g.cluster_at(off + n - 1)
            if 0 <= c0 <= c1 < size:
                soft[c0:c1 + 1] = b"\x01" * (c1 - c0 + 1)
    return soft


def _trim(extents, length):
    out, rem = [], length
    for o, n in extents:
        if rem <= 0:
            break
        out.append((o, min(n, rem)))
        rem -= n
    return out


class own_clusters:
    """While a file is being rebuilt, its own clusters are not 'soft'."""

    def __init__(self, g, soft, r):
        self.g, self.soft, self.r, self.saved = g, soft, r, []

    def __enter__(self):
        if self.soft is None:
            return self
        for off, n in verified_spans(self.r, self.g.cluster_size):
            c0, c1 = self.g.cluster_at(off), self.g.cluster_at(off + max(1, n) - 1)
            if 0 <= c0 <= c1 < len(self.soft):
                self.saved.append((c0, bytes(self.soft[c0:c1 + 1])))
                self.soft[c0:c1 + 1] = bytes(c1 - c0 + 1)
        return self

    def __exit__(self, *exc):
        for c0, b in self.saved:
            self.soft[c0:c0 + len(b)] = b
        return False


def reassemble_all(src, res, found: list[Found], progress=None, passes: int = 3):
    for vr in res.volumes:
        g = getattr(vr, "geometry", None)
        if g is None:
            continue
        mine = [r for r in found if getattr(r, "volume", None) is vr]
        cs = g.cluster_size

        def aligned(r):  # files from an earlier format with another layout can't use this geometry
            return r.extents and r.start >= g.data_offset and (r.start - g.data_offset) % cs == 0
        broken = [r for r in mine if r.kind == "jpeg" and r.status in ("corrupt", "partial") and aligned(r)]
        videos = [r for r in mine if r.kind == "isobmff" and r.status in ("corrupt", "partial") and aligned(r)
                  and r.ext not in (".avif", ".cr3")]
        from .formats.checks import EMPTY_TABLE, NO_TABLE
        from .formats.more import NOT_PROVEN
        from .reassemble_stream import WALKERS
        # (a PDF whose table can't be read was most likely split: its table is in a later piece)
        streams = [r for r in mine if r.kind in WALKERS and aligned(r)
                   and (r.status in ("corrupt", "partial")
                        or (r.status == "unverified" and r.note in (NOT_PROVEN, NO_TABLE, EMPTY_TABLE)))]
        if not broken and not videos and not streams:
            continue
        blocked = bytearray(g.last_cluster + 2)
        blocked[0:2] = b"\x01\x01"

        def own(r):
            for o, n in r.extents:
                c0 = g.cluster_at(o)
                c1 = g.cluster_at(o + n - 1)
                blocked[c0:c1 + 1] = b"\x01" * (c1 - c0 + 1)

        for r in mine:
            if r.status == "ok" or (r.meta.get("live") and r.meta.get("chain") == "fat"):
                own(r)
        for off, _kind in getattr(vr, "headers", []):
            if off < g.data_offset or (off - g.data_offset) % cs:
                continue  # only a header at a cluster start means "another file starts here"
            c = g.cluster_at(off)
            if 0 <= c < len(blocked):
                blocked[c] = 1
        fsr = getattr(vr, "fs_reader", None)
        if fsr is not None:
            block_fs_clusters(fsr, blocked)  # each rebuilder unblocks the start of the file it works on
        # The first piece of every broken photo is that photo's: the clusters
        # from its header up to where its picture stops decoding. Claiming them
        # up front keeps other photos from wandering into them (the camera's
        # next free cluster is often just past another photo's first piece).
        heads: dict[int, list[int]] = {}
        for r in broken:
            start = g.cluster_at(r.start)
            bad = r.meta.get("bad_at")
            n = max(1, int(bad) // cs) if bad else 1
            head = []
            for c in range(start, min(start + n, g.last_cluster + 1)):
                if c != start and blocked[c]:
                    break  # ran into a file that checked out: the first piece ended there
                head.append(c)
            heads[id(r)] = [c for c in head if not blocked[c] or c == start]
        owned = bytearray(blocked)  # clusters of files that checked out, and header clusters
        claimed: dict[int, int] = {}  # first-piece clusters (not the header cluster) -> whose
        for r in broken:
            for c in heads[id(r)][1:]:
                claimed[c] = id(r)
        asm = _Assembler(src, g, blocked)
        todo = sorted(broken, key=lambda r: r.start)
        done = [0]

        def jpeg_round() -> bool:
            """Alternate between respecting the first-piece claims and ignoring
            them: a claim can be wrong (another photo's data can decode as the
            end of a first piece), and a finished photo is checked to the last
            block, so trying both ways only adds correct rebuilds."""
            idle, any_progress = 0, False
            for p in range(max(passes, 4)):
                strict = p % 2 == 0
                for c in claimed:
                    blocked[c] = 1 if strict else 0
                progress_made = False
                for r in list(todo):
                    head = heads[id(r)]
                    for c in head:
                        if c == head[0] or not owned[c]:
                            blocked[c] = 0  # its own first piece, minus what other photos have since proved theirs
                    try:
                        out = asm.run(head[0], r.expected_size)
                    except Exception:
                        out = None  # never let one file stop the run
                    for c in head:
                        blocked[c] = 1 if owned[c] or (strict and c in claimed) else 0
                    if out is None:
                        continue
                    clusters, length, status, note = out
                    if status == "ok" or (status == "partial" and r.status == "partial" and length > r.size):
                        r.extents = _extents(g, clusters, length)
                        r.status = status
                        r.note = note
                        if status == "ok":
                            if _Assembler._pieces(clusters) > 1:
                                r.meta["reassembled_from"] = _Assembler._pieces(clusters)
                                if r.origin == "carved":
                                    r.origin = "reassembled"
                            used = set(clusters)
                            for c in head[1:] + list(clusters):
                                claimed.pop(c, None)  # settled: this photo's, or not a first piece at all
                            for c in head[1:]:
                                if c not in used and not owned[c]:
                                    blocked[c] = 0  # claimed by this photo, but it turned out not to be its
                            own(r)
                            for c in clusters:
                                owned[c] = 1
                            todo.remove(r)
                            progress_made = any_progress = True
                            done[0] += 1
                            if progress:
                                progress(done[0], len(broken))
                idle = 0 if progress_made else idle + 1
                if not todo or idle >= 2:
                    break
            for c in claimed:
                blocked[c] = 1  # leftovers of photos that couldn't be finished: not other files' data
            return any_progress

        jpeg_round()
        # Videos and recordings, in rounds: every file that checks out takes
        # its clusters out of the others' way, so try again while that helps.
        pending_v = sorted(videos, key=lambda r: r.start)
        pending_s = sorted(streams, key=lambda r: r.start)
        for _round in range(3):
            soft = soft_map(g, mine, len(blocked))
            gained = 0
            if pending_v:
                from .reassemble_video import reassemble_videos
                gained += reassemble_videos(src, g, blocked, pending_v, own, soft=soft)
                pending_v = [r for r in pending_v if r.status != "ok"]
            if pending_s:
                from .reassemble_stream import reassemble_streams
                gained += reassemble_streams(src, g, blocked, pending_s, own, soft=soft)
                pending_s = [r for r in pending_s if r.status in ("partial", "corrupt")
                             or (r.status == "unverified" and r.note == NOT_PROVEN)]
            if gained and todo:
                jpeg_round()
            if not gained or not (pending_v or pending_s or todo):
                break
        for r in todo:
            if r.status == "corrupt":
                # keep the readable top of the picture rather than nothing
                start = g.cluster_at(r.start)
                bad = r.meta.get("bad_at")
                if bad:
                    r.extents = _extents(g, list(range(start, start + bad // g.cluster_size + 1)),
                                         (bad // g.cluster_size) * g.cluster_size or bad)
                    r.status = "partial"
                    r.note = "only the top part of the picture could be recovered"
