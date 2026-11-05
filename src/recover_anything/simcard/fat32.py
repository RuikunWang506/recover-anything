"""Build simulated FAT32 SD-card images that behave like real cards.

This is a *test harness*, written independently of the recovery engine, so the
engine can be measured against ground truth. It mimics what cameras, phones and
operating systems actually do to a card:

* write files (8.3 names, long file names, timestamps, cluster chains)
* delete files (0xE5 marker, cluster chain freed, optional high-word wipe)
* quick format (new boot sector, FATs and root directory wiped, data left alone)
* first-fit allocation (camera-like), which fragments new files into old holes

Images are sparse files, so a 4 GiB card costs only the bytes actually written.
"""
from __future__ import annotations

import datetime as _dt
import os
import random
import struct

EOC = 0x0FFFFFFF
SECTOR = 512
_VALID_83 = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")


def _dos_time(t: _dt.datetime) -> tuple[int, int]:
    time = (t.hour << 11) | (t.minute << 5) | (t.second // 2)
    date = ((t.year - 1980) << 9) | (t.month << 5) | t.day
    return time, date


def lfn_checksum(name11: bytes) -> int:
    s = 0
    for b in name11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def _short_name(long_name: str, taken: set[bytes]) -> tuple[bytes, bool]:
    """Return (11-byte short name, needs_lfn)."""
    if "." in long_name and not long_name.startswith("."):
        base, ext = long_name.rsplit(".", 1)
    else:
        base, ext = long_name, ""
    raw_b = base.upper().encode("ascii", "replace")
    raw_e = ext.upper().encode("ascii", "replace")
    clean_b = bytes(c for c in raw_b if c in _VALID_83)
    clean_e = bytes(c for c in raw_e if c in _VALID_83)[:3]
    exact = (
        long_name == long_name.upper()
        and len(base) <= 8
        and len(ext) <= 3
        and clean_b == raw_b
        and bytes(c for c in raw_e if c in _VALID_83) == raw_e
        and len(clean_b) > 0
    )
    if exact:
        name = clean_b.ljust(8, b" ") + clean_e.ljust(3, b" ")
        if name not in taken:
            return name, False
    for n in range(1, 1000):
        tail = b"~%d" % n
        name = (clean_b[: 8 - len(tail)] + tail).ljust(8, b" ") + clean_e.ljust(3, b" ")
        if name not in taken:
            return name, True
    raise RuntimeError("too many similar names")


class _Dir:
    def __init__(self, clusters: list[int], parent: "_Dir | None"):
        self.clusters = clusters
        self.parent = parent
        self.entries: list[bytearray] = []  # raw 32-byte entries, in disk order
        self.children: dict[str, tuple[int, int]] = {}  # lower long name -> (first idx, short idx)


class Fat32Card:
    """A writable simulated FAT32 card backed by a sparse image file."""

    def __init__(
        self,
        path: str,
        size: int = 4 * 1024**3,
        cluster_size: int = 32768,
        partitioned: bool = True,
        allocator: str = "first_free",
        label: str = "NO NAME",
        seed: int = 1,
        strict: bool = True,
    ):
        self.path = path
        self.size = size
        self.cluster_size = cluster_size
        self.allocator = allocator
        self.label = label
        self.rng = random.Random(seed)
        self.part_offset = 4 * 1024 * 1024 if partitioned else 0
        with open(path, "wb") as f:
            f.truncate(size)
        self.f = open(path, "r+b")
        self._layout(strict)
        if partitioned:
            self._write_mbr()
        self.format()

    # ------------------------------------------------------------------ layout
    def _layout(self, strict: bool) -> None:
        spc = self.cluster_size // SECTOR
        total = (self.size - self.part_offset) // SECTOR
        reserved, nfats, fatsz = 32, 2, 1
        while True:
            clusters = (total - reserved - nfats * fatsz) // spc
            new = ((clusters + 2) * 4 + SECTOR - 1) // SECTOR
            if new <= fatsz:  # monotone: never oscillates
                break
            fatsz = new
        if strict and clusters < 65525:
            raise ValueError("FAT32 needs at least 65525 clusters: use a bigger card or smaller clusters")
        self.spc, self.reserved, self.nfats, self.fatsz = spc, reserved, nfats, fatsz
        self.total_sectors = total
        self.cluster_count = clusters
        self.fat_offset = self.part_offset + reserved * SECTOR
        self.data_offset = self.fat_offset + nfats * fatsz * SECTOR

    def cluster_offset(self, c: int) -> int:
        return self.data_offset + (c - 2) * self.cluster_size

    def _write_mbr(self) -> None:
        mbr = bytearray(512)
        start = self.part_offset // SECTOR
        entry = struct.pack("<B3sB3sII", 0x00, b"\xfe\xff\xff", 0x0C, b"\xfe\xff\xff", start, self.total_sectors)
        mbr[446:462] = entry
        mbr[510:512] = b"\x55\xaa"
        self._pwrite(0, bytes(mbr))

    def _pwrite(self, off: int, data: bytes) -> None:
        self.f.seek(off)
        self.f.write(data)

    # ------------------------------------------------------------------ format
    def format(self, recreate_dirs: list[str] | None = None) -> None:
        """Quick format: what Windows, macOS and cameras do by default."""
        vol_id = self.rng.getrandbits(32)
        bs = bytearray(512)
        bs[0:3] = b"\xeb\x58\x90"
        bs[3:11] = b"MSDOS5.0"
        struct.pack_into(
            "<HBHBHHBHHHII", bs, 11,
            SECTOR, self.spc, self.reserved, self.nfats, 0, 0, 0xF8, 0, 63, 255,
            self.part_offset // SECTOR, self.total_sectors,
        )
        struct.pack_into("<IHHIHH", bs, 36, self.fatsz, 0, 0, 2, 1, 6)
        struct.pack_into("<BBBI", bs, 64, 0x80, 0, 0x29, vol_id)
        bs[71:82] = self.label.encode("ascii")[:11].ljust(11, b" ")
        bs[82:90] = b"FAT32   "
        bs[510:512] = b"\x55\xaa"
        self._pwrite(self.part_offset, bytes(bs))
        self._pwrite(self.part_offset + 6 * SECTOR, bytes(bs))
        self.fat = [0] * (self.cluster_count + 2)
        self.fat[0], self.fat[1], self.fat[2] = 0x0FFFFFF8, 0x0FFFFFFF, EOC
        zero = bytes(self.fatsz * SECTOR)
        for i in range(self.nfats):
            self._pwrite(self.fat_offset + i * self.fatsz * SECTOR, zero)
        self._flush_fat([0, 1, 2])
        self._pwrite(self.cluster_offset(2), bytes(self.cluster_size))
        self.hint = 3
        self.root = _Dir([2], None)
        self.dirs: dict[str, _Dir] = {"": self.root}
        self._write_fsinfo()
        for d in recreate_dirs or []:
            self.mkdir(d)

    def _write_fsinfo(self) -> None:
        fi = bytearray(512)
        struct.pack_into("<I", fi, 0, 0x41615252)
        struct.pack_into("<III", fi, 484, 0x61417272, self.free_clusters(), self.hint)
        struct.pack_into("<I", fi, 508, 0xAA550000)
        self._pwrite(self.part_offset + SECTOR, bytes(fi))
        self._pwrite(self.part_offset + 7 * SECTOR, bytes(fi))

    def free_clusters(self) -> int:
        return sum(1 for v in self.fat[2:] if v == 0)

    def _flush_fat(self, idxs) -> None:
        for i in range(self.nfats):
            base = self.fat_offset + i * self.fatsz * SECTOR
            for c in idxs:
                self._pwrite(base + c * 4, struct.pack("<I", self.fat[c]))

    # -------------------------------------------------------------- allocation
    def _alloc(self, n: int) -> list[int]:
        out: list[int] = []
        last = self.cluster_count + 1
        if self.allocator == "first_free":
            c = 2
            while len(out) < n and c <= last:
                if self.fat[c] == 0:
                    out.append(c)
                c += 1
        else:  # next_free, like Windows: continue from the hint and wrap around
            c, scanned = self.hint, 0
            while len(out) < n and scanned < self.cluster_count:
                if c > last:
                    c = 2
                if self.fat[c] == 0:
                    out.append(c)
                c += 1
                scanned += 1
        if len(out) < n:
            raise OSError("card full")
        for a, b in zip(out, out[1:]):
            self.fat[a] = b
        self.fat[out[-1]] = EOC
        self.hint = out[-1] + 1
        self._flush_fat(out)
        return out

    # -------------------------------------------------------------- directories
    def _dir_capacity(self, d: _Dir) -> int:
        return len(d.clusters) * self.cluster_size // 32

    def _write_dir(self, d: _Dir) -> None:
        while len(d.entries) + 1 > self._dir_capacity(d):
            new = self._alloc(1)[0]
            self.fat[d.clusters[-1]] = new
            self._flush_fat([d.clusters[-1]])
            d.clusters.append(new)
            self._pwrite(self.cluster_offset(new), bytes(self.cluster_size))
        blob = b"".join(bytes(e) for e in d.entries)
        blob = blob.ljust(self._dir_capacity(d) * 32, b"\x00")
        for i, c in enumerate(d.clusters):
            self._pwrite(self.cluster_offset(c), blob[i * self.cluster_size:(i + 1) * self.cluster_size])

    def _add_entry(self, d: _Dir, long_name: str, attr: int, start: int, size: int, t: _dt.datetime) -> None:
        taken = {bytes(e[0:11]) for e in d.entries if e[0] not in (0x00, 0xE5) and e[11] != 0x0F}
        short, needs_lfn = _short_name(long_name, taken)
        first_idx = len(d.entries)
        if needs_lfn:
            u = long_name.encode("utf-16-le")
            chars = [u[i:i + 2] for i in range(0, len(u), 2)]
            if len(chars) % 13:
                chars.append(b"\x00\x00")
            while len(chars) % 13:
                chars.append(b"\xff\xff")
            parts = [chars[i:i + 13] for i in range(0, len(chars), 13)]
            ck = lfn_checksum(short)
            for seq in range(len(parts), 0, -1):
                p = parts[seq - 1]
                e = bytearray(32)
                e[0] = seq | (0x40 if seq == len(parts) else 0)
                e[1:11] = b"".join(p[0:5])
                e[11], e[12], e[13] = 0x0F, 0, ck
                e[14:26] = b"".join(p[5:11])
                e[28:32] = b"".join(p[11:13])
                d.entries.append(e)
        e = bytearray(32)
        e[0:11] = short
        e[11] = attr
        tm, dt = _dos_time(t)
        struct.pack_into("<BHHH", e, 13, 0, tm, dt, dt)
        struct.pack_into("<H", e, 20, start >> 16)
        struct.pack_into("<HHHI", e, 22, tm, dt, start & 0xFFFF, size)
        d.entries.append(e)
        d.children[long_name.lower()] = (first_idx, len(d.entries) - 1)
        self._write_dir(d)

    def mkdir(self, path: str, t: _dt.datetime | None = None) -> _Dir:
        path = path.strip("/")
        if path in self.dirs:
            return self.dirs[path]
        parent_path, _, name = path.rpartition("/")
        parent = self.mkdir(parent_path, t) if parent_path else self.root
        t = t or _dt.datetime(2026, 1, 1, 12, 0, 0)
        c = self._alloc(1)[0]
        d = _Dir([c], parent)
        dot = bytearray(32)
        dot[0:11] = b".          "
        dot[11] = 0x10
        dotdot = bytearray(32)
        dotdot[0:11] = b"..         "
        dotdot[11] = 0x10
        pc = 0 if parent is self.root else parent.clusters[0]
        for ent, start in ((dot, c), (dotdot, pc)):
            tm, dt = _dos_time(t)
            struct.pack_into("<BHHH", ent, 13, 0, tm, dt, dt)
            struct.pack_into("<H", ent, 20, start >> 16)
            struct.pack_into("<HHHI", ent, 22, tm, dt, start & 0xFFFF, 0)
        d.entries = [dot, dotdot]
        self._pwrite(self.cluster_offset(c), bytes(self.cluster_size))
        self._write_dir(d)
        self._add_entry(parent, name, 0x10, c, 0, t)
        self.dirs[path] = d
        return d

    # -------------------------------------------------------------------- files
    def write_file(self, path: str, data: bytes, t: _dt.datetime | None = None) -> list[int]:
        """Write a file and return the clusters it occupies."""
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        d = self.mkdir(parent_path) if parent_path else self.root
        t = t or _dt.datetime(2026, 1, 1, 12, 0, 0)
        n = (len(data) + self.cluster_size - 1) // self.cluster_size
        clusters = self._alloc(n) if n else []
        cs = self.cluster_size
        # write contiguous runs in one go
        i = 0
        while i < len(clusters):
            j = i
            while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1:
                j += 1
            self._pwrite(self.cluster_offset(clusters[i]), data[i * cs:(j + 1) * cs])
            i = j + 1
        self._add_entry(d, name, 0x20, clusters[0] if clusters else 0, len(data), t)
        self._write_fsinfo()
        return clusters

    def write_placeholder(self, path: str, size: int) -> list[int]:
        """Allocate space for a file without writing its bytes (fills a card fast)."""
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        d = self.mkdir(parent_path) if parent_path else self.root
        n = (size + self.cluster_size - 1) // self.cluster_size
        clusters = self._alloc(n)
        self._add_entry(d, name, 0x20, clusters[0], size, _dt.datetime(2026, 1, 1))
        return clusters

    def delete(self, path: str, clear_high_word: bool = False) -> None:
        """Delete like an OS or camera: mark entries 0xE5 and free the chain."""
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        d = self.dirs[parent_path] if parent_path else self.root
        first, short = d.children.pop(name.lower())
        e = d.entries[short]
        start = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
        for i in range(first, short + 1):
            d.entries[i][0] = 0xE5
        if clear_high_word:
            struct.pack_into("<H", e, 20, 0)
        freed = []
        c = start
        while 2 <= c < EOC - 7 and c < len(self.fat):
            nxt = self.fat[c]
            self.fat[c] = 0
            freed.append(c)
            c = nxt
        self._flush_fat(freed)
        self._write_dir(d)
        self._write_fsinfo()

    def allocated_clusters(self) -> set:
        return {c for c in range(2, len(self.fat)) if self.fat[c] != 0}

    def overwrite_clusters(self, clusters: list[int], byte: int = 0) -> None:
        for c in clusters:
            self._pwrite(self.cluster_offset(c), bytes([byte]) * self.cluster_size)

    def close(self) -> None:
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
