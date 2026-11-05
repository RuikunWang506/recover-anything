"""exFAT reader (SDXC cards: 64 GB and up). Recovers live, deleted and
lost-folder entries.

Good news for recovery: exFAT stores each file's first cluster and exact length
in its directory entry, and files written in one piece carry a "NoFatChain"
flag meaning "the clusters are contiguous". Deleting a file only clears the
in-use bit of its entries, so the name, size, date and location all survive.
"""
from __future__ import annotations

import datetime as _dt
import re
import struct

from ..records import Found, Geometry

_DCF_DIR = re.compile(r"^\d{3}[A-Z0-9_]{5}$")


def _ts(v: int, ten_ms: int = 0):
    if not v:
        return None
    try:
        return _dt.datetime(1980 + (v >> 25), (v >> 21) & 15, (v >> 16) & 31, (v >> 11) & 31, (v >> 5) & 63,
                            min(59, (v & 31) * 2 + ten_ms // 100))
    except ValueError:
        return None


def entry_set_checksum(entries: bytes) -> int:
    ck = 0
    for i, b in enumerate(entries):
        if i in (2, 3):
            continue
        ck = (((ck & 1) << 15) | (ck >> 1)) + b
        ck &= 0xFFFF
    return ck


class ExfatVolume:
    def __init__(self, src, offset: int = 0):
        self.src, self.offset = src, offset
        bs = src.read(offset, 512)
        if bs[3:11] != b"EXFAT   ":
            raise ValueError("not exFAT")
        vol_len = struct.unpack_from("<Q", bs, 72)[0]
        (fat_off, fat_len, heap_off, self.cluster_count, self.root_cluster) = struct.unpack_from("<IIIII", bs, 80)
        bps_shift, spc_shift, self.nfats = bs[108], bs[109], bs[110]
        # Out-of-range shifts or offsets mean a damaged boot sector: refuse, so
        # the caller falls back to the backup copy or raw carving.
        if not (9 <= bps_shift <= 12 and spc_shift <= 25 - bps_shift and self.nfats in (1, 2)):
            raise ValueError("implausible exFAT geometry")
        self.bps = 1 << bps_shift
        self.cluster_size = 1 << (bps_shift + spc_shift)
        size = getattr(src, "size", 0) or 0
        vol_bytes = vol_len * self.bps
        if size and vol_bytes > (size - offset) * 1.05 + (1 << 20):
            vol_bytes = size - offset  # claims to be bigger than the device: trust the device
        if not (0 < fat_off < heap_off) or heap_off * self.bps >= max(vol_bytes, 1) or self.cluster_count < 1:
            raise ValueError("implausible exFAT layout")
        self.fat_offset = offset + fat_off * self.bps
        self.data_offset = offset + heap_off * self.bps
        fits = (offset + vol_bytes - self.data_offset) // self.cluster_size
        if size:
            fits = min(fits, (size - self.data_offset) // self.cluster_size)
        self.cluster_count = max(0, min(self.cluster_count, fits))
        if not 2 <= self.root_cluster < self.cluster_count + 2:
            raise ValueError("implausible exFAT root folder")
        self.kind = "exFAT"
        self.label = ""
        self.geometry = Geometry(self.data_offset, self.cluster_size, 2, self.cluster_count)
        self.bitmap = None
        self.visited_dirs: set[int] = set()

    def _fat(self, c: int) -> int:
        b = self.src.read(self.fat_offset + 4 * c, 4)
        return struct.unpack("<I", b)[0] if len(b) == 4 else 0

    def cluster_offset(self, c: int) -> int:
        return self.data_offset + (c - 2) * self.cluster_size

    def is_allocated(self, c: int) -> bool:
        if self.bitmap is None:
            return False
        i = c - 2
        return 0 <= i < len(self.bitmap) * 8 and bool(self.bitmap[i >> 3] & (1 << (i & 7)))

    def chain(self, start: int, limit: int = 1 << 22) -> list[int] | None:
        out, seen, c = [], set(), start
        while 2 <= c < self.cluster_count + 2:
            if c in seen or len(out) > limit:
                return None
            seen.add(c)
            out.append(c)
            nxt = self._fat(c)
            if nxt >= 0xFFFFFFF8:
                return out
            if nxt < 2 or nxt == 0xFFFFFFF7:
                return None
            c = nxt
        return None

    def extents(self, clusters: list[int], size: int):
        out, rem, i = [], size, 0
        while i < len(clusters) and rem > 0:
            j = i
            while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1:
                j += 1
            n = min(rem, (j - i + 1) * self.cluster_size)
            out.append((self.cluster_offset(clusters[i]), n))
            rem -= n
            i = j + 1
        return out

    def _data(self, first: int, length: int, no_fat_chain: bool, live: bool):
        if length <= 0 or first < 2:
            return [], "empty"
        n = (length + self.cluster_size - 1) // self.cluster_size
        if not no_fat_chain:
            ch = self.chain(first)
            if ch and len(ch) >= n:
                return self.extents(ch[:n], length), "fat"
        avail = (self.cluster_count + 2 - first) * self.cluster_size
        return [(self.cluster_offset(first), min(length, max(0, avail)))], (
            "contiguous" if no_fat_chain else "assumed-contiguous")

    def _dir_blob(self, first: int, length: int | None, no_fat_chain: bool, live: bool) -> bytes:
        if length:
            length = min(length, 256 << 20)  # the exFAT maximum for a directory
            ext, _ = self._data(first, length, no_fat_chain, live)
            return b"".join(self.src.read(o, n) for o, n in ext)
        ch = self.chain(first, limit=4096) if live else None
        if ch:
            return b"".join(self.src.read(self.cluster_offset(c), self.cluster_size) for c in ch[:4096])
        return self.src.read(self.cluster_offset(first), self.cluster_size)

    def parse_sets(self, blob: bytes):
        i, n = 0, len(blob) // 32
        while i < n:
            e = blob[i * 32:(i + 1) * 32]
            t = e[0]
            if t == 0x00:
                break
            if t == 0x81 and self.bitmap is None:
                first, length = struct.unpack_from("<IQ", e, 20)
                if 2 <= first < self.cluster_count + 2:
                    self.bitmap = self.src.read(self.cluster_offset(first),
                                                min(length, (self.cluster_count + 7) // 8, 1 << 26))
            if t in (0x85, 0x05):
                sc = e[1]
                if 2 <= sc <= 18 and i + sc < n:
                    raw = bytearray(blob[i * 32:(i + 1 + sc) * 32])
                    deleted = t == 0x05
                    for k in range(sc + 1):
                        raw[k * 32] |= 0x80
                    ok = entry_set_checksum(bytes(raw)) == struct.unpack_from("<H", raw, 2)[0]
                    s = raw[32:64]
                    if s[0] == 0xC0:
                        flags, name_len = s[1], s[3]
                        first, length = struct.unpack_from("<IQ", s, 20)
                        name = b"".join(bytes(raw[k * 32 + 2:k * 32 + 32]) for k in range(2, sc + 1)
                                        if raw[k * 32] == 0xC1)
                        name = name.decode("utf-16-le", "replace")[:name_len]
                        attrs = struct.unpack_from("<H", raw, 4)[0]
                        mod = struct.unpack_from("<I", raw, 12)[0]
                        yield {
                            "name": name, "deleted": deleted, "is_dir": bool(attrs & 0x10), "start": first,
                            "size": length, "no_fat_chain": bool(flags & 2), "mtime": _ts(mod, raw[21]),
                            "checksum_ok": ok,
                        }
                    i += 1 + sc
                    continue
            i += 1

    def walk(self):
        self.src.read(self.cluster_offset(self.root_cluster), 32)
        yield from self._walk(self.root_cluster, None, False, "", True, 0)

    def _walk(self, first, length, nfc, path, live, depth):
        if depth > 32 or first in self.visited_dirs:
            return
        self.visited_dirs.add(first)
        try:
            blob = self._dir_blob(first, length, nfc, live)
            ents = list(self.parse_sets(blob))
        except Exception:
            return  # one unreadable folder must not hide the others
        for c in range(first, first + max(1, len(blob) // self.cluster_size)):
            self.visited_dirs.add(c)
        for ent in ents:
            if ent["is_dir"]:
                if ent["start"] >= 2:
                    yield from self._walk(ent["start"], ent["size"], ent["no_fat_chain"], path + ent["name"] + "/",
                                          live and not ent["deleted"], depth + 1)
                continue
            rec = self._record(ent, path, live)
            if rec:
                yield rec

    def _record(self, ent, path, parent_live, origin="filesystem"):
        if ent["size"] <= 0 or ent["start"] < 2 or ent["start"] >= self.cluster_count + 2:
            return None
        deleted = ent["deleted"] or not parent_live
        ext = "." + ent["name"].rsplit(".", 1)[1].lower() if "." in ent["name"] else ""
        extents, how = self._data(ent["start"], ent["size"], ent["no_fat_chain"], not deleted)
        rec = Found(kind="?", ext=ext, extents=extents, origin=origin, name=ent["name"], path=path, deleted=deleted,
                    fs_time=ent["mtime"], expected_size=ent["size"])
        rec.meta["chain"] = how
        rec.meta["start_cluster"] = ent["start"]
        if deleted and self.bitmap is not None:
            n = (ent["size"] + self.cluster_size - 1) // self.cluster_size
            reused = sum(1 for c in range(ent["start"], min(ent["start"] + n, ent["start"] + 4096,
                                                             self.cluster_count + 2))
                         if self.is_allocated(c))
            if reused:
                rec.meta["reused_clusters"] = reused
                rec.note = "some of its space now belongs to another file"
        return rec

    def _looks_like_dir(self, blob: bytes, o: int) -> bool:
        if o + 96 > len(blob):
            return False
        t = blob[o]
        if t not in (0x85, 0x05):
            return False
        sc = blob[o + 1]
        if not 2 <= sc <= 18 or o + 32 * (sc + 1) > len(blob):
            return False
        raw = bytearray(blob[o:o + 32 * (sc + 1)])
        if raw[32] & 0x7F != 0x40 or raw[64] & 0x7F != 0x41:
            return False
        for k in range(sc + 1):
            raw[k * 32] |= 0x80
        return entry_set_checksum(bytes(raw)) == struct.unpack_from("<H", raw, 2)[0]

    def lost_folder_records(self, chunk: int = 8 << 20):
        cs = self.cluster_size
        per = max(1, chunk // cs)
        dirs = []
        c, last = 2, self.cluster_count + 1
        while c <= last:
            n = min(per, last - c + 1)
            blob = self.src.read_uncached(self.cluster_offset(c), n * cs)
            for k in range(n):
                if c + k not in self.visited_dirs and self._looks_like_dir(blob, k * cs):
                    dirs.append(c + k)
            c += n
        if not dirs:
            return
        dirset = set(dirs)
        parsed = {}
        for d in dirs:
            try:
                parsed[d] = list(self.parse_sets(self.src.read(self.cluster_offset(d), cs)))
            except Exception:
                continue
        names, parent = {}, {}
        for d, ents in parsed.items():
            for e in ents:
                if e["is_dir"] and e["start"] in dirset:
                    names.setdefault(e["start"], e["name"])
                    parent[e["start"]] = d
        for d, ents in parsed.items():
            if d not in names:
                kids = [e["name"] for e in ents if e["is_dir"]]
                names[d] = "DCIM" if kids and all(_DCF_DIR.match(k) for k in kids) else "Lost folder %d" % d

        def path_of(d, depth=0):
            p = parent.get(d)
            if p is None or depth > 16:
                return names[d] + "/"
            return path_of(p, depth + 1) + names[d] + "/"

        for d, ents in parsed.items():
            self.visited_dirs.add(d)
            base = path_of(d)
            for e in ents:
                if not e["is_dir"]:
                    rec = self._record(dict(e, deleted=True), base, False, origin="lost-folder")
                    if rec:
                        rec.meta["was_deleted"] = e["deleted"]
                        yield rec
