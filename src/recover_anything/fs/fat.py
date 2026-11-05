"""FAT12/16/32 reader that also finds what the filesystem forgot.

Three sources of named files, in order of trust:
  1. live and deleted entries in the directory tree (deleted entries keep the
     name, size, date and first cluster; we assume contiguous clusters)
  2. "lost folders": directory clusters still sitting in the data area after a
     quick format. FAT subdirectories start with '.' and '..' entries whose
     '.' points at its own cluster, which makes them easy to recognize.
  3. everything else is left to the carver.
"""
from __future__ import annotations

import datetime as _dt
import re
import struct
from array import array

from ..records import Found, Geometry

_DCF_DIR = re.compile(r"^\d{3}[A-Z0-9_]{5}$")
_NAME_OK = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~ ") | set(range(0x80, 0x100))


def _dos_dt(date: int, time: int):
    if not date:
        return None
    try:
        return _dt.datetime(1980 + (date >> 9), (date >> 5) & 15, date & 31,
                            time >> 11, (time >> 5) & 63, min(59, (time & 31) * 2))
    except ValueError:
        return None


_FIRST_CHAR = [  # (what's left of a camera file name, the letter that was erased)
    ("MG_", "I"), ("VI_", "M"), ("SC_", "D"), ("SC0", "D"), ("SCF", "D"), ("SCN", "D"), ("JI_", "D"),
    ("OPR", "G"), ("X0", "G"), ("H0", "G"), ("AM_", "S"), ("AH0", "M"), ("ID_", "V"), ("XL_", "P"),
]


def _guess_first_char(rest: str) -> str:
    """Deleted 8.3 names lose their first character. Camera names are predictable."""
    up = rest.upper()
    for prefix, ch in _FIRST_CHAR:
        if up.startswith(prefix):
            return ch
    return "_"


def probe(bs: bytes) -> bool:
    if len(bs) < 512 or bs[510:512] != b"\x55\xaa":
        return False
    if bs[0] not in (0xEB, 0xE9):
        return False
    bps = struct.unpack_from("<H", bs, 11)[0]
    spc = bs[13]
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1):
        return False
    if bs[3:11] in (b"EXFAT   ", b"NTFS    "):
        return False
    nfats = bs[16]
    reserved = struct.unpack_from("<H", bs, 14)[0]
    return nfats in (1, 2) and reserved >= 1


class FatVolume:
    def __init__(self, src, offset: int = 0):
        self.src = src
        self.offset = offset
        bs = src.read(offset, 512)
        if not probe(bs):
            raise ValueError("not a FAT boot sector")
        (self.bps, self.spc, reserved, self.nfats, root_entries, tot16, _media, fatsz16) = struct.unpack_from(
            "<HBHBHHBH", bs, 11)
        tot32 = struct.unpack_from("<I", bs, 32)[0]
        fatsz32, = struct.unpack_from("<I", bs, 36)
        self.fatsz = fatsz16 or fatsz32
        total = tot16 or tot32
        self.cluster_size = self.bps * self.spc
        root_sectors = (root_entries * 32 + self.bps - 1) // self.bps
        self.fat_offset = offset + reserved * self.bps
        self.root_offset = self.fat_offset + self.nfats * self.fatsz * self.bps
        self.data_offset = self.root_offset + root_sectors * self.bps
        data_sectors = total - (reserved + self.nfats * self.fatsz + root_sectors)
        self.cluster_count = max(0, data_sectors // self.spc)
        if fatsz16 == 0:
            self.kind = "FAT32"
        elif self.cluster_count < 4085:
            self.kind = "FAT12"
        else:
            self.kind = "FAT16"
        self.root_cluster = struct.unpack_from("<I", bs, 44)[0] if self.kind == "FAT32" else 0
        self.root_size = root_sectors * self.bps
        self.label = (bs[71:82] if self.kind == "FAT32" else bs[43:54]).decode("latin-1").strip()
        self.geometry = Geometry(self.data_offset, self.cluster_size, 2, self.cluster_count)
        self.fat = self._read_fat()
        self.visited_dirs: set[int] = set()

    # ------------------------------------------------------------------ FAT
    def _read_fat(self):
        n = self.cluster_count + 2
        raw = self.src.read(self.fat_offset, self.fatsz * self.bps)
        if self.kind == "FAT32":
            a = array("I")
            a.frombytes(raw[: n * 4].ljust(n * 4, b"\x00"))
            if struct.pack("<I", 1) != b"\x01\x00\x00\x00":
                a.byteswap()
            return [v & 0x0FFFFFFF for v in a]
        if self.kind == "FAT16":
            a = array("H")
            a.frombytes(raw[: n * 2].ljust(n * 2, b"\x00"))
            if struct.pack("<H", 1) != b"\x01\x00":
                a.byteswap()
            return list(a)
        out = []
        for c in range(n):
            i = c * 3 // 2
            v = raw[i] | (raw[i + 1] << 8) if i + 1 < len(raw) else 0
            out.append(v >> 4 if c & 1 else v & 0xFFF)
        return out

    @property
    def eoc(self) -> int:
        return {"FAT12": 0xFF8, "FAT16": 0xFFF8, "FAT32": 0x0FFFFFF8}[self.kind]

    def is_allocated(self, c: int) -> bool:
        return 0 <= c < len(self.fat) and self.fat[c] != 0

    def chain(self, start: int, limit: int = 1 << 22) -> list[int] | None:
        """Follow a live cluster chain. None if it is broken."""
        out, seen = [], set()
        c = start
        while 2 <= c < self.cluster_count + 2:
            if c in seen or len(out) > limit:
                return None
            seen.add(c)
            out.append(c)
            nxt = self.fat[c]
            if nxt >= self.eoc:
                return out
            if nxt < 2:
                return None
            c = nxt
        return None

    def cluster_offset(self, c: int) -> int:
        return self.data_offset + (c - 2) * self.cluster_size

    def extents_for(self, clusters: list[int], size: int) -> list[tuple[int, int]]:
        out, remaining = [], size
        i = 0
        while i < len(clusters) and remaining > 0:
            j = i
            while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1:
                j += 1
            n = min(remaining, (j - i + 1) * self.cluster_size)
            out.append((self.cluster_offset(clusters[i]), n))
            remaining -= n
            i = j + 1
        return out

    def contiguous_extents(self, start: int, size: int) -> list[tuple[int, int]]:
        avail = (self.cluster_count + 2 - start) * self.cluster_size
        return [(self.cluster_offset(start), min(size, max(0, avail)))]

    # ----------------------------------------------------------- directories
    def _plausible_entry(self, e: bytes) -> bool:
        if e[0] == 0:
            return True
        attr = e[11]
        if attr == 0x0F:
            return (e[0] & 0x1F) <= 20 or e[0] == 0xE5
        if attr & 0xC0:
            return False
        if e[0] != 0xE5 and e[0] not in _NAME_OK:
            return False
        if any(b not in _NAME_OK for b in e[1:11]):
            return False
        start = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
        if start > self.cluster_count + 1:
            return False
        date = struct.unpack_from("<H", e, 24)[0]
        if date and not (1 <= (date >> 5) & 15 <= 12 and 1 <= date & 31 <= 31):
            return False
        return True

    def looks_like_dir_cluster(self, data: bytes) -> bool:
        good = 0
        for i in range(0, len(data), 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            if not self._plausible_entry(e):
                return False
            good += 1
        return good > 0

    def parse_entries(self, data: bytes):
        """Yield dicts for every short entry (live or deleted) in a directory blob."""
        lfn: list[tuple[int, bytes]] = []
        lfn_ck = None
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            first = e[0]
            if first == 0x00:
                break
            attr = e[11]
            if attr == 0x0F:
                seq = first & 0x1F
                if first != 0xE5 and first & 0x40:
                    lfn = []
                lfn.append((seq, e[1:11] + e[14:26] + e[28:32]))
                lfn_ck = e[13]
                continue
            if attr & 0x08 and not attr & 0x10:  # volume label
                lfn = []
                continue
            raw_name = e[0:11]
            if raw_name in (b".          ", b"..         "):
                lfn = []
                continue
            deleted = first == 0xE5
            base = raw_name[:8].decode("latin-1").rstrip()
            ext = raw_name[8:11].decode("latin-1").rstrip()
            long_name = None
            if lfn:
                parts = lfn if deleted else sorted(lfn, key=lambda t: t[0])
                if deleted:
                    parts = list(reversed(lfn))
                u = b"".join(p[1] for p in parts)
                txt = u.decode("utf-16-le", "replace")
                cut = txt.find("\x00")
                long_name = txt[:cut] if cut >= 0 else txt.rstrip("￿")
                long_name = long_name.rstrip("￿") or None
                if long_name and not deleted:
                    from ..simcard.fat32 import lfn_checksum
                    if lfn_checksum(raw_name) != lfn_ck:
                        long_name = None
            lfn = []
            if deleted:
                rest = base[1:]
                if long_name:
                    short = long_name
                else:
                    short = _guess_first_char(rest) + rest
                    short = short + ("." + ext if ext else "")
            else:
                short = base + ("." + ext if ext else "")
            name = long_name or short
            start = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            yield {
                "name": name,
                "deleted": deleted,
                "is_dir": bool(attr & 0x10),
                "start": start,
                "start_lo": struct.unpack_from("<H", e, 26)[0],
                "size": size,
                "mtime": _dos_dt(struct.unpack_from("<H", e, 24)[0], struct.unpack_from("<H", e, 22)[0]),
            }

    def _dir_blob(self, start: int, live: bool) -> bytes:
        if start == 0 and self.kind != "FAT32":
            return self.src.read(self.root_offset, self.root_size)
        clusters = self.chain(start) if live else None
        if clusters:
            return b"".join(self.src.read(self.cluster_offset(c), self.cluster_size) for c in clusters)
        # broken or deleted chain: read clusters while they still look like a directory
        out = []
        c = start
        while 2 <= c < self.cluster_count + 2 and len(out) < 64:
            data = self.src.read(self.cluster_offset(c), self.cluster_size)
            if out and (data[:11] == b".          " or not self.looks_like_dir_cluster(data)):
                break
            out.append(data)
            if b"\x00" * 32 in data[-32:] or data[-32] == 0:
                break
            c += 1
        return b"".join(out)

    def walk(self):
        """Yield Found records for the live tree (live + deleted entries)."""
        root = self.root_cluster if self.kind == "FAT32" else 0
        yield from self._walk_dir(root, "", live=True, depth=0)

    def _walk_dir(self, start: int, path: str, live: bool, depth: int):
        if depth > 32 or start in self.visited_dirs:
            return
        self.visited_dirs.add(start)
        blob = self._dir_blob(start, live)
        for ent in self.parse_entries(blob):
            if ent["is_dir"]:
                if ent["start"] >= 2:
                    yield from self._walk_dir(ent["start"], path + ent["name"] + "/", live and not ent["deleted"],
                                              depth + 1)
                continue
            rec = self._record(ent, path, live)
            if rec:
                yield rec

    def _record(self, ent: dict, path: str, parent_live: bool, origin: str = "filesystem") -> Found | None:
        size, start = ent["size"], ent["start"]
        if size == 0 or start < 2 or start >= self.cluster_count + 2:
            return None
        deleted = ent["deleted"] or not parent_live
        ext = "." + ent["name"].rsplit(".", 1)[1].lower() if "." in ent["name"] else ""
        rec = Found(kind="?", ext=ext, extents=[], origin=origin, name=ent["name"], path=path, deleted=deleted,
                    fs_time=ent["mtime"], expected_size=size)
        if not deleted:
            clusters = self.chain(start)
            if clusters and len(clusters) * self.cluster_size >= size:
                rec.extents = self.extents_for(clusters, size)
                rec.meta["chain"] = "fat"
                return rec
        hw0 = self.kind == "FAT32" and deleted and (start >> 16) == 0 and self.cluster_count + 2 > 0x10000
        if ent.get("deleted") and origin == "filesystem" and self.is_allocated(start) and not hw0:
            rec.extents = self.contiguous_extents(start, size)
            rec.status = "lost"
            rec.note = "a newer file now occupies its space"
            return rec
        rec.extents = self.contiguous_extents(start, size)
        rec.meta["chain"] = "assumed-contiguous"
        rec.meta["start_cluster"] = start
        n = (size + self.cluster_size - 1) // self.cluster_size
        reused = sum(1 for c in range(start, min(start + n, self.cluster_count + 2)) if self.is_allocated(c))
        if deleted and reused:
            rec.meta["reused_clusters"] = reused
            rec.note = "some of its space now belongs to another file"
        if hw0:
            rec.meta["high_word_zero"] = True
            rec.meta["start_allocated"] = self.is_allocated(start)
        return rec

    # ------------------------------------------------------- lost folders
    def find_lost_folders(self, chunk: int = 8 << 20):
        """Scan the data area for directory clusters the tree no longer reaches."""
        cs = self.cluster_size
        found: dict[int, int] = {}
        per = max(1, chunk // cs)
        c = 2
        last = self.cluster_count + 1
        while c <= last:
            n = min(per, last - c + 1)
            blob = self.src.read_uncached(self.cluster_offset(c), n * cs)
            for k in range(n):
                o = k * cs
                if blob[o:o + 11] == b".          " and blob[o + 11] & 0x10 and blob[o + 32:o + 43] == b"..         ":
                    me = (struct.unpack_from("<H", blob, o + 20)[0] << 16) | struct.unpack_from("<H", blob, o + 26)[0]
                    if me == c + k:
                        parent = (struct.unpack_from("<H", blob, o + 52)[0] << 16) | struct.unpack_from("<H", blob,
                                                                                                         o + 58)[0]
                        found[c + k] = parent
            c += n
        return found

    def lost_folder_records(self):
        """Yield Found records from directories that survived a format or deletion."""
        dirs = self.find_lost_folders()
        orphans = {c: p for c, p in dirs.items() if c not in self.visited_dirs}
        if not orphans:
            return
        names: dict[int, str] = {}
        parsed: dict[int, list] = {}
        for c in orphans:
            ents = list(self.parse_entries(self._dir_blob(c, live=False)))
            parsed[c] = ents
            for e in ents:
                if e["is_dir"] and e["start"] in orphans:
                    names.setdefault(e["start"], e["name"])
        for c, ents in parsed.items():
            if c not in names:
                kids = [e["name"] for e in ents if e["is_dir"]]
                if kids and all(_DCF_DIR.match(k) for k in kids):
                    names[c] = "DCIM"
                else:
                    names[c] = "Lost folder %d" % c

        def path_of(c, depth=0):
            p = orphans.get(c, 0)
            if depth > 16 or p not in orphans or p == c:
                return names[c] + "/"
            return path_of(p, depth + 1) + names[c] + "/"

        for c, ents in parsed.items():
            self.visited_dirs.add(c)
            base = path_of(c)
            for e in ents:
                if e["is_dir"]:
                    continue
                rec = self._record(dict(e, deleted=True), base, False, origin="lost-folder")
                if rec:
                    rec.meta["was_deleted"] = e["deleted"]
                    yield rec
