"""NTFS reader (external USB drives, big USB sticks, Windows data drives).

Every file and folder has a record in the Master File Table (MFT). Deleting a
file only clears the record's "in use" flag and frees its clusters in $Bitmap:
the record keeps the file's name, its parent folder and the exact list of
where its pieces are (the runlist), until NTFS reuses the record for a new
file. So a deleted file comes back with its name and folder, even when it was
stored in several pieces, as long as its clusters weren't reused.

Folders are rebuilt from each record's parent link, not from the folder
indexes (a deleted file is removed from its folder's index at once).

Not read: compressed, encrypted and sparse files (their bytes on disk aren't
the file), and data stored inside a record across a sector's last two bytes
(NTFS swaps those for a check value on disk).
"""
from __future__ import annotations

import datetime as _dt
import struct

from ..records import Found, Geometry

ROOT = 5
FIRST_USER_RECORD = 16          # 0-15: $MFT ... $Extend; 16-23 reserved
END = 0xFFFFFFFF
T_STD, T_LIST, T_NAME, T_DATA, T_INDEX_ALLOC = 0x10, 0x20, 0x30, 0x80, 0xA0
F_IN_USE, F_DIR = 0x01, 0x02
A_COMPRESSED, A_ENCRYPTED, A_SPARSE = 0x0001, 0x4000, 0x8000
MAX_RECORDS = 1 << 24           # 16 million records: bounded work on hostile input
MAX_RUNS = 1 << 16
STRIDE = 512                    # update-sequence check values sit every 512 bytes, whatever the sector size


def probe(bs: bytes) -> bool:
    return len(bs) >= 512 and bs[3:11] == b"NTFS    " and bs[510:512] == b"\x55\xaa"


def _filetime(v: int):
    if not v:
        return None
    try:
        return _dt.datetime(1601, 1, 1) + _dt.timedelta(microseconds=v // 10)
    except (OverflowError, ValueError):
        return None


def decode_runlist(data: bytes, pos: int, limit: int):
    """[(lcn or None for a sparse run, length in clusters)] from a runlist."""
    runs, lcn = [], 0
    while pos < limit and len(runs) < MAX_RUNS:
        head = data[pos]
        if head == 0:
            break
        ln, on = head & 0x0F, head >> 4
        if ln == 0 or ln > 8 or on > 8 or pos + 1 + ln + on > limit:
            raise ValueError("bad runlist")
        length = int.from_bytes(data[pos + 1:pos + 1 + ln], "little")
        if on:
            lcn += int.from_bytes(data[pos + 1 + ln:pos + 1 + ln + on], "little", signed=True)
            runs.append((lcn, length))
        else:
            runs.append((None, length))
        pos += 1 + ln + on
    return runs


def fixup(rec: bytearray, bps: int = STRIDE) -> bool:
    """Undo NTFS's update sequence: the last two bytes of each sector of a
    record hold a check value on disk; the real bytes are kept in a list at
    the start. False when a sector doesn't carry the check value (a torn or
    overwritten record)."""
    if len(rec) < 48:
        return False
    usa_off, usa_n = struct.unpack_from("<HH", rec, 4)
    if usa_n < 2 or usa_off + 2 * usa_n > len(rec) or (usa_n - 1) * bps > len(rec):
        return False
    usn = rec[usa_off:usa_off + 2]
    for i in range(1, usa_n):
        end = i * bps - 2
        if rec[end:end + 2] != usn:
            return False
        rec[end:end + 2] = rec[usa_off + 2 * i:usa_off + 2 * i + 2]
    return True


class _Rec:
    __slots__ = ("num", "seq", "in_use", "is_dir", "base", "names", "data", "data_flags", "si_mtime",
                 "abs_off", "fixed", "resident")

    def __init__(self, num):
        self.num, self.seq, self.in_use, self.is_dir, self.base = num, 0, False, False, 0
        self.names = []          # (namespace, name, parent number, parent sequence)
        self.data = []           # (start vcn, runs, real size or None) for the unnamed $DATA
        self.data_flags = 0
        self.si_mtime = None
        self.abs_off = 0
        self.resident = None     # (offset in record, length) of resident data


class NtfsVolume:
    def __init__(self, src, offset: int = 0):
        self.src, self.offset = src, offset
        bs = src.read(offset, 512)
        if not probe(bs):
            raise ValueError("not an NTFS boot sector")
        self.bps = struct.unpack_from("<H", bs, 0x0B)[0]
        spc = bs[0x0D]
        if self.bps not in (512, 1024, 2048, 4096) or spc == 0:
            raise ValueError("bad NTFS geometry")
        if spc > 0x80:                     # large clusters are written as a power of two
            spc = 1 << (256 - spc)
        self.cluster_size = self.bps * spc
        total = struct.unpack_from("<Q", bs, 0x28)[0]
        self.cluster_count = total * self.bps // self.cluster_size
        size = getattr(src, "size", 0) or 0
        if size:
            self.cluster_count = min(self.cluster_count, max(0, (size - offset) // self.cluster_size))
        self.mft_lcn = struct.unpack_from("<Q", bs, 0x30)[0]
        c = struct.unpack_from("<b", bs, 0x40)[0]
        self.rec_size = (1 << -c) if c < 0 else c * self.cluster_size
        if not 256 <= self.rec_size <= 65536 or not 0 < self.mft_lcn < self.cluster_count:
            raise ValueError("bad NTFS layout")
        self.data_offset = offset
        self.geometry = Geometry(offset, self.cluster_size, 0, self.cluster_count)
        self.label = ""
        self.dir_clusters = ()
        rec0 = self._read_record_at(self.cluster_offset(self.mft_lcn))
        if rec0 is None or not rec0.data:
            raise ValueError("the MFT's own record is unreadable")
        self.mft_extents = self._extents(rec0, limit=None)
        self._records = None
        self._bitmap = None

    # ------------------------------------------------------------ geometry
    def cluster_offset(self, c: int) -> int:
        return self.offset + c * self.cluster_size

    def contiguous_extents(self, c: int, size):
        return [(self.cluster_offset(c), size or self.cluster_size)]

    def _bitmap_bytes(self) -> bytes:
        if self._bitmap is None:
            self._bitmap = b""
            recs = self.records()
            r = recs.get(6)
            if r is not None and r.data:
                ext = self._extents(r)
                self._bitmap = b"".join(self.src.read(o, n) for o, n in ext)
        return self._bitmap

    def allocated_map(self) -> bytearray:
        """One byte per cluster number: 1 if $Bitmap says it is in use."""
        bm, n = self._bitmap_bytes(), self.cluster_count
        out = bytearray(n)
        for i in range(min(n, len(bm) * 8)):
            if bm[i >> 3] & (1 << (i & 7)):
                out[i] = 1
        return out

    def is_allocated(self, c: int) -> bool:
        bm = self._bitmap_bytes()
        return 0 <= c < len(bm) * 8 and bool(bm[c >> 3] & (1 << (c & 7)))

    # ------------------------------------------------------------ records
    def _read_record_at(self, off: int, num: int = 0):
        raw = bytearray(self.src.read(off, self.rec_size))
        if len(raw) < self.rec_size or raw[:4] != b"FILE":
            return None
        fixed = fixup(raw)
        r = _Rec(num)
        r.abs_off = off
        r.fixed = fixed
        try:
            self._parse(raw, r)
        except (struct.error, ValueError, IndexError):
            return None
        return r

    def _parse(self, raw: bytearray, r: _Rec) -> None:
        r.seq = struct.unpack_from("<H", raw, 16)[0]
        attr_off, flags = struct.unpack_from("<HH", raw, 20)
        used = struct.unpack_from("<I", raw, 24)[0]
        r.base = struct.unpack_from("<Q", raw, 32)[0] & 0xFFFFFFFFFFFF
        r.in_use, r.is_dir = bool(flags & F_IN_USE), bool(flags & F_DIR)
        limit = min(len(raw), max(used, attr_off))
        pos = attr_off
        while pos + 16 <= limit:
            atype, alen = struct.unpack_from("<II", raw, pos)
            if atype == END or alen < 16 or pos + alen > len(raw):
                break
            nonres, name_len = raw[pos + 8], raw[pos + 9]
            aflags = struct.unpack_from("<H", raw, pos + 12)[0]
            if not nonres:
                vlen, voff = struct.unpack_from("<IH", raw, pos + 16)
                value = bytes(raw[pos + voff:pos + voff + vlen])
                if atype == T_NAME and len(value) >= 66:
                    parent = struct.unpack_from("<Q", value, 0)[0]
                    n = value[64]
                    name = value[66:66 + 2 * n].decode("utf-16-le", "replace")
                    r.names.append((value[65], name, parent & 0xFFFFFFFFFFFF, parent >> 48))
                elif atype == T_STD and len(value) >= 16:
                    r.si_mtime = _filetime(struct.unpack_from("<Q", value, 8)[0])
                elif atype == T_DATA and name_len == 0:
                    r.data_flags = aflags
                    r.data.append((0, None, vlen))
                    r.resident = (pos + voff, vlen)
            else:
                start_vcn = struct.unpack_from("<Q", raw, pos + 16)[0]
                run_off = struct.unpack_from("<H", raw, pos + 32)[0]
                real = struct.unpack_from("<Q", raw, pos + 48)[0]
                if atype == T_DATA and name_len == 0:
                    r.data_flags |= aflags
                    runs = decode_runlist(raw, pos + run_off, pos + alen)
                    r.data.append((start_vcn, runs, real if start_vcn == 0 else None))
            pos += alen

    def records(self) -> dict:
        """Every readable MFT record, by number, with extension records'
        $DATA pieces merged into their base record."""
        if self._records is not None:
            return self._records
        out, rs = {}, self.rec_size
        num = 0
        for off, n in self.mft_extents:
            for k in range(n // rs):
                if num >= MAX_RECORDS:
                    break
                r = self._read_record_at(off + k * rs, num)
                if r is not None:
                    out[num] = r
                num += 1
        for r in list(out.values()):
            if r.base and r.base in out and r.data and r.base != r.num:
                out[r.base].data.extend(d for d in r.data if d[1] is not None)
        self._records = out
        return out

    def _extents(self, r: _Rec, limit="real"):
        """Absolute (offset, length) pieces of a record's unnamed $DATA, cut to
        its real size. Raises ValueError for a sparse run."""
        pieces = sorted((d for d in r.data if d[1] is not None), key=lambda d: d[0])
        real = next((d[2] for d in r.data if d[0] == 0 and d[2] is not None), None)
        out, vcn = [], 0
        for start, runs, _ in pieces:
            if start != vcn:
                raise ValueError("a piece of the runlist is missing")
            for lcn, length in runs:
                if lcn is None:
                    raise ValueError("sparse")
                if lcn < 0 or lcn + length > self.cluster_count:
                    raise ValueError("runlist points outside the volume")
                o = self.cluster_offset(lcn)
                if out and out[-1][0] + out[-1][1] == o:
                    out[-1] = (out[-1][0], out[-1][1] + length * self.cluster_size)
                else:
                    out.append((o, length * self.cluster_size))
                vcn += length
        if limit is None or real is None:
            return out
        cut, rem = [], real
        for o, n in out:
            if rem <= 0:
                break
            cut.append((o, min(n, rem)))
            rem -= n
        if rem > 0:
            raise ValueError("the runlist is shorter than the file")
        return cut

    # ------------------------------------------------------------ the tree
    @staticmethod
    def _best_name(r: _Rec):
        # Win32 (1), Win32+DOS (3), POSIX (0) names over the 8.3 DOS name (2)
        names = sorted(r.names, key=lambda x: (x[0] == 2, x[0] == 0))
        return names[0] if names else None

    def _path(self, recs: dict, r: _Rec):
        """Folder path of a record from its parent links, or None when a
        parent was reused for something else (the folder is unknown)."""
        parts, seen, cur = [], set(), r
        while True:
            best = self._best_name(cur)
            if best is None:
                return None
            _, _, pnum, pseq = best
            if pnum == ROOT:
                return "/".join(reversed(parts))
            if pnum in seen or len(parts) > 64:
                return None
            seen.add(pnum)
            p = recs.get(pnum)
            # a deleted folder's record gets a new sequence number when freed
            if p is None or not p.is_dir or p.seq not in (pseq, (pseq + 1) & 0xFFFF):
                return None
            pname = self._best_name(p)
            if pname is None:
                return None
            parts.append(pname[1])
            cur = p

    def _deleted_above(self, recs: dict, r: _Rec) -> bool:
        cur, seen = r, set()
        while True:
            best = self._best_name(cur)
            if best is None or best[2] == ROOT or best[2] in seen:
                return False
            seen.add(best[2])
            p = recs.get(best[2])
            if p is None:
                return False
            if not p.in_use:
                return True
            cur = p

    def walk(self):
        recs = self.records()
        for num in sorted(recs):
            r = recs[num]
            if num < FIRST_USER_RECORD or r.is_dir or r.base or not r.data:
                continue
            best = self._best_name(r)
            if best is None:
                continue
            path = self._path(recs, r)
            if path is not None and (path == "$Extend" or path.startswith("$Extend/")):
                continue
            rec = self._record(recs, r, best[1], path)
            if rec is not None:
                yield rec

    def _record(self, recs, r: _Rec, name: str, path):
        deleted = not r.in_use or self._deleted_above(recs, r)
        ext = "." + name.rsplit(".", 1)[1].lower() if "." in name else ""
        rec = Found(kind="?", ext=ext, extents=[], origin="filesystem", name=name, path=path or None,
                    deleted=deleted, fs_time=r.si_mtime)
        rec.meta["chain"] = "runlist"
        rec.meta["mft_record"] = r.num
        if path is None:
            rec.meta["folder_unknown"] = True
        if r.data_flags & (A_COMPRESSED | A_ENCRYPTED):
            rec.note = "compressed or encrypted by Windows; its bytes can't be read directly"
            rec.status = "lost"
            return rec
        if r.resident is not None and not any(d[1] is not None for d in r.data):
            off, n = r.resident
            if n == 0:
                return None
            # resident data across a sector's last two bytes holds a check value on disk
            if r.fixed and (off + n - 1) % STRIDE < STRIDE - 2 and off // STRIDE == (off + n - 1) // STRIDE:
                rec.extents = [(r.abs_off + off, n)]
                rec.expected_size = n
                rec.meta["resident"] = True
                return rec
            return None
        try:
            rec.extents = self._extents(r)
        except ValueError as e:
            rec.note = ("stored as a sparse file" if "sparse" in str(e)
                        else "its list of pieces is damaged (%s)" % e)
            rec.status = "lost"
            return rec
        rec.expected_size = sum(n for _, n in rec.extents)
        if not rec.extents:
            return None
        rec.meta["start_cluster"] = (rec.extents[0][0] - self.offset) // self.cluster_size
        if deleted:
            reused = 0
            for o, n in rec.extents:
                c0 = (o - self.offset) // self.cluster_size
                for c in range(c0, c0 + (n + self.cluster_size - 1) // self.cluster_size):
                    if self.is_allocated(c):
                        reused += 1
                if reused > 4096:
                    break
            if reused:
                rec.meta["reused_clusters"] = reused
                rec.note = "some of its space now belongs to another file"
        return rec

    def lost_folder_records(self):
        return iter(())         # NTFS keeps every record in the MFT: nothing to search for
