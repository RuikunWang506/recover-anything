"""Simulated exFAT card (SDXC: 64 GB and larger cards ship formatted exFAT).

Behaves like Windows/cameras: contiguous files get the NoFatChain flag and no
FAT entries; fragmented files get a FAT chain; delete clears the in-use bits and
the allocation bitmap; quick format rebuilds the system structures and leaves
the data alone.
"""
from __future__ import annotations

import datetime as _dt
import os
import random
import struct

SECTOR = 512


def _ts(t: _dt.datetime) -> int:
    return ((t.year - 1980) << 25) | (t.month << 21) | (t.day << 16) | (t.hour << 11) | (t.minute << 5) | (t.second // 2)


def _set_checksum(raw: bytes) -> int:
    ck = 0
    for i, b in enumerate(raw):
        if i in (2, 3):
            continue
        ck = ((((ck & 1) << 15) | (ck >> 1)) + b) & 0xFFFF
    return ck


def _name_hash(name: str) -> int:
    h = 0
    for ch in name.upper():
        c = ord(ch)
        for b in (c & 0xFF, c >> 8):
            h = ((((h & 1) << 15) | (h >> 1)) + b) & 0xFFFF
    return h


class _Dir:
    def __init__(self, cluster: int):
        self.cluster = cluster
        self.entries: list[bytearray] = []
        self.children: dict[str, tuple[int, int]] = {}  # name -> (first index, count)


class ExfatCard:
    def __init__(self, path: str, size: int = 4 * 1024**3, cluster_size: int = 32768, partitioned: bool = True,
                 allocator: str = "first_free", seed: int = 1, existing: bool = False, **_):
        self.path, self.size, self.cluster_size = path, size, cluster_size
        self.rng = random.Random(seed)
        self.part_offset = 4 * 1024 * 1024 if partitioned else 0
        if not existing:
            with open(path, "wb") as f:
                f.truncate(size)
        self.f = open(path, "r+b")
        self.spc_shift = (cluster_size // SECTOR).bit_length() - 1
        vol_sectors = (size - self.part_offset) // SECTOR
        self.vol_sectors = vol_sectors
        self.fat_offset = 2048
        spc = cluster_size // SECTOR
        n = (vol_sectors - self.fat_offset) // spc
        self.fat_length = ((n + 2) * 4 + SECTOR - 1) // SECTOR
        heap = self.fat_offset + self.fat_length
        heap = (heap + spc - 1) // spc * spc
        self.heap_offset = heap
        self.cluster_count = (vol_sectors - heap) // spc
        if partitioned:
            mbr = bytearray(512)
            mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 0x07, b"\xfe\xff\xff",
                                       self.part_offset // SECTOR, vol_sectors)
            mbr[510:512] = b"\x55\xaa"
            self._pw(0, bytes(mbr))
        self.format()

    def _pw(self, off, data):
        self.f.seek(off)
        self.f.write(data)

    def cluster_offset(self, c: int) -> int:
        return self.part_offset + self.heap_offset * SECTOR + (c - 2) * self.cluster_size

    # ---------------------------------------------------------- format
    def format(self, recreate_dirs=None):
        serial = self.rng.getrandbits(32)
        self.bitmap = bytearray((self.cluster_count + 7) // 8)
        self.fat_chain: dict[int, int] = {}
        bm_clusters = (len(self.bitmap) + self.cluster_size - 1) // self.cluster_size
        self.bitmap_first = 2
        self.upcase_first = 2 + bm_clusters
        self.root_cluster = self.upcase_first + 1
        for c in range(2, self.root_cluster + 1):
            self._mark(c, True)
        bs = bytearray(512)
        bs[0:3] = b"\xeb\x76\x90"
        bs[3:11] = b"EXFAT   "
        struct.pack_into("<QQIIIIIIHHBBBBB", bs, 64, self.part_offset // SECTOR, self.vol_sectors, self.fat_offset,
                         self.fat_length, self.heap_offset, self.cluster_count, self.root_cluster, serial, 0x0100, 0,
                         9, self.spc_shift, 1, 0x80, 0)
        bs[510:512] = b"\x55\xaa"
        region = [bytes(bs)]
        ext = bytearray(512)
        ext[508:512] = b"\x00\x00\x55\xaa"
        region += [bytes(ext)] * 8 + [bytes(512), bytes(512)]
        ck = 0
        for si, sec in enumerate(region):
            for i, b in enumerate(sec):
                if si == 0 and i in (106, 107, 112):
                    continue
                ck = ((0x80000000 if ck & 1 else 0) + (ck >> 1) + b) & 0xFFFFFFFF
        region.append(struct.pack("<I", ck) * 128)
        blob = b"".join(region)
        self._pw(self.part_offset, blob)
        self._pw(self.part_offset + 12 * SECTOR, blob)
        fat = bytearray(self.fat_length * SECTOR)
        struct.pack_into("<II", fat, 0, 0xFFFFFFF8, 0xFFFFFFFF)
        self._pw(self.part_offset + self.fat_offset * SECTOR, bytes(fat))
        upcase = b"".join(struct.pack("<H", ord(chr(i).upper()) if i < 128 else i) for i in range(128))
        self._pw(self.cluster_offset(self.upcase_first), upcase.ljust(self.cluster_size, b"\x00"))
        up_ck = 0
        for b in upcase:
            up_ck = ((0x80000000 if up_ck & 1 else 0) + (up_ck >> 1) + b) & 0xFFFFFFFF
        self.root = _Dir(self.root_cluster)
        e = bytearray(32)
        e[0] = 0x03  # empty volume label
        self.root.entries.append(e)
        e = bytearray(32)
        e[0] = 0x81
        struct.pack_into("<IQ", e, 20, self.bitmap_first, len(self.bitmap))
        self.root.entries.append(e)
        e = bytearray(32)
        e[0] = 0x82
        struct.pack_into("<I", e, 4, up_ck)
        struct.pack_into("<IQ", e, 20, self.upcase_first, len(upcase))
        self.root.entries.append(e)
        self.dirs = {"": self.root}
        self._write_dir(self.root)
        self._flush_bitmap()
        for d in recreate_dirs or []:
            self.mkdir(d)

    # ------------------------------------------------------- allocation
    def _mark(self, c, used):
        i = c - 2
        if used:
            self.bitmap[i >> 3] |= 1 << (i & 7)
        else:
            self.bitmap[i >> 3] &= ~(1 << (i & 7)) & 0xFF

    def _used(self, c):
        i = c - 2
        return bool(self.bitmap[i >> 3] & (1 << (i & 7)))

    def _flush_bitmap(self):
        self._pw(self.cluster_offset(self.bitmap_first), bytes(self.bitmap))

    def _alloc(self, n):
        out, c = [], 2
        while len(out) < n and c < self.cluster_count + 2:
            if not self._used(c):
                out.append(c)
            c += 1
        if len(out) < n:
            raise OSError("card full")
        for c in out:
            self._mark(c, True)
        contiguous = all(b == a + 1 for a, b in zip(out, out[1:]))
        if not contiguous:
            for a, b in zip(out, out[1:]):
                self._fat(a, b)
            self._fat(out[-1], 0xFFFFFFFF)
        self._flush_bitmap()
        return out, contiguous

    def _fat(self, c, v):
        self.fat_chain[c] = v
        self._pw(self.part_offset + self.fat_offset * SECTOR + 4 * c, struct.pack("<I", v))

    def allocated_clusters(self):
        return {c for c in range(2, self.cluster_count + 2) if self._used(c)}

    # ------------------------------------------------------- directories
    def _write_dir(self, d: _Dir):
        blob = b"".join(bytes(e) for e in d.entries)
        if len(blob) > self.cluster_size:
            raise OSError("directory full (simulator limit)")
        self._pw(self.cluster_offset(d.cluster), blob.ljust(self.cluster_size, b"\x00"))

    def _add_set(self, d: _Dir, name: str, attrs: int, first: int, length: int, contiguous: bool, t):
        u = name.encode("utf-16-le")
        nn = (len(name) + 14) // 15
        f = bytearray(32)
        f[0], f[1] = 0x85, 1 + nn
        struct.pack_into("<H", f, 4, attrs)
        ts = _ts(t)
        struct.pack_into("<III", f, 8, ts, ts, ts)
        s = bytearray(32)
        s[0], s[1], s[3] = 0xC0, 0x01 | (0x02 if contiguous else 0), len(name)
        struct.pack_into("<H", s, 4, _name_hash(name))
        struct.pack_into("<Q", s, 8, length)
        struct.pack_into("<IQ", s, 20, first, length)
        names = []
        for k in range(nn):
            e = bytearray(32)
            e[0] = 0xC1
            chunk = u[k * 30:(k + 1) * 30]
            e[2:2 + len(chunk)] = chunk
            names.append(e)
        ents = [f, s] + names
        raw = b"".join(bytes(e) for e in ents)
        struct.pack_into("<H", f, 2, _set_checksum(raw))
        d.children[name.lower()] = (len(d.entries), len(ents))
        d.entries.extend(ents)
        self._write_dir(d)

    def mkdir(self, path: str, t=None):
        path = path.strip("/")
        if path in self.dirs:
            return self.dirs[path]
        parent_path, _, name = path.rpartition("/")
        parent = self.mkdir(parent_path, t) if parent_path else self.root
        t = t or _dt.datetime(2026, 1, 1, 12, 0, 0)
        (c,), _ = self._alloc(1)
        d = _Dir(c)
        self._write_dir(d)
        self._add_set(parent, name, 0x10, c, self.cluster_size, True, t)
        self.dirs[path] = d
        return d

    def write_file(self, path: str, data: bytes, t=None):
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        d = self.mkdir(parent_path) if parent_path else self.root
        t = t or _dt.datetime(2026, 1, 1, 12, 0, 0)
        n = (len(data) + self.cluster_size - 1) // self.cluster_size
        clusters, contiguous = self._alloc(n) if n else ([], True)
        cs = self.cluster_size
        i = 0
        while i < len(clusters):
            j = i
            while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1:
                j += 1
            self._pw(self.cluster_offset(clusters[i]), data[i * cs:(j + 1) * cs])
            i = j + 1
        self._add_set(d, name, 0x20, clusters[0] if clusters else 0, len(data), contiguous, t)
        return clusters

    def delete(self, path: str, **_):
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        d = self.dirs[parent_path] if parent_path else self.root
        first, count = d.children.pop(name.lower())
        s = d.entries[first + 1]
        fc, length = struct.unpack_from("<IQ", s, 20)
        contiguous = bool(s[1] & 2)
        for k in range(count):
            d.entries[first + k][0] &= 0x7F
        n = (length + self.cluster_size - 1) // self.cluster_size
        if contiguous:
            clusters = list(range(fc, fc + n))
        else:
            clusters, c = [], fc
            while c in self.fat_chain and len(clusters) < n:
                clusters.append(c)
                nxt = self.fat_chain[c]
                if nxt >= 0xFFFFFFF8:
                    break
                c = nxt
            for c in clusters:
                self._fat(c, 0)
        for c in clusters:
            self._mark(c, False)
        self._flush_bitmap()
        self._write_dir(d)

    def move(self, path: str, new_path: str, t=None) -> None:
        """Move or rename like an OS: the old entry set is marked unused, a new
        one in the new folder points at the same clusters, the data stays put."""
        path, new_path = path.strip("/"), new_path.strip("/")
        parent_path, _, name = path.rpartition("/")
        d = self.dirs[parent_path] if parent_path else self.root
        first, count = d.children.pop(name.lower())
        s = d.entries[first + 1]
        fc, length = struct.unpack_from("<IQ", s, 20)
        contiguous = bool(s[1] & 2)
        for k in range(count):
            d.entries[first + k][0] &= 0x7F
        self._write_dir(d)
        new_parent, _, new_name = new_path.rpartition("/")
        d2 = self.mkdir(new_parent, t) if new_parent else self.root
        self._add_set(d2, new_name, 0x20, fc, length, contiguous, t or _dt.datetime(2026, 1, 1, 12, 0, 0))

    def close(self):
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
