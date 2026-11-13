"""A small NTFS volume writer for tests, following the on-disk format: boot
sector (and its backup in the last sector), $MFT with 1 KB records whose
sectors end in update-sequence check values, runlists for non-resident data,
and the $Bitmap file. Deleting works like Windows: the record's "in use" flag
is cleared and its sequence number bumped, its clusters are freed, and the
record is reused (lowest free number first) by the next new file.

Folders get an empty $INDEX_ROOT: the reader rebuilds folders from each
record's parent link and never needs the indexes.
"""
from __future__ import annotations

import datetime as _dt
import os
import struct

REC = 1024
BPS = 512
N_RECORDS = 1024
FIRST_USER = 24


def _filetime(t: _dt.datetime) -> int:
    return int((t - _dt.datetime(1601, 1, 1)).total_seconds() * 10_000_000)


def encode_runlist(runs) -> bytes:
    out, prev = bytearray(), 0
    for lcn, length in runs:
        lb = length.to_bytes((length.bit_length() + 7) // 8 or 1, "little")
        delta = lcn - prev
        n = 1
        while not -(1 << (8 * n - 1)) <= delta < (1 << (8 * n - 1)):
            n += 1
        ob = delta.to_bytes(n, "little", signed=True)
        out.append(len(lb) | (len(ob) << 4))
        out += lb + ob
        prev = lcn
    out.append(0)
    return bytes(out)


def _attr(atype: int, value: bytes = b"", runs=None, real: int = 0, alloc: int = 0, flags: int = 0,
          attr_id: int = 0, name: str = "") -> bytes:
    nm = name.encode("utf-16-le")
    if runs is None:                                  # resident
        voff = (24 + len(nm) + 7) & ~7
        length = (voff + len(value) + 7) & ~7
        a = bytearray(length)
        struct.pack_into("<IIBBHHH", a, 0, atype, length, 0, len(name), 24 if nm else 0, flags, attr_id)
        struct.pack_into("<IH", a, 16, len(value), voff)
        a[24:24 + len(nm)] = nm
        a[voff:voff + len(value)] = value
        return bytes(a)
    rl = encode_runlist(runs)
    roff = (64 + len(nm) + 7) & ~7
    length = (roff + len(rl) + 7) & ~7
    a = bytearray(length)
    last_vcn = sum(n for _, n in runs) - 1
    struct.pack_into("<IIBBHHH", a, 0, atype, length, 1, len(name), 64 if nm else 0, flags, attr_id)
    struct.pack_into("<QQHH", a, 16, 0, max(last_vcn, 0), roff, 0)
    struct.pack_into("<QQQ", a, 40, alloc, real, real)
    a[64:64 + len(nm)] = nm
    a[roff:roff + len(rl)] = rl
    return bytes(a)


def _std(t: _dt.datetime) -> bytes:
    ft = _filetime(t)
    return struct.pack("<QQQQI", ft, ft, ft, ft, 0x20) + bytes(28)


def _fname(parent: int, pseq: int, name: str, t: _dt.datetime, size: int, is_dir: bool) -> bytes:
    ft = _filetime(t)
    u = name.encode("utf-16-le")
    return (struct.pack("<QQQQQQQII", parent | (pseq << 48), ft, ft, ft, ft, size, size,
                        0x10000000 if is_dir else 0x20, 0)
            + bytes([len(name), 1]) + u)


def _index_root() -> bytes:
    root = struct.pack("<IIIB3x", 0x30, 1, 4096, 1)
    header = struct.pack("<IIIB3x", 16, 32, 32, 0)
    end = struct.pack("<QHHI", 0, 16, 0, 2)
    return root + header + end


class NtfsCard:
    def __init__(self, path: str, size: int = 64 * 1024**2, cluster_size: int = 4096, mft_pieces: int = 1):
        self.path, self.size, self.cs = path, size, cluster_size
        self.spc = cluster_size // BPS
        self.total_sectors = size // BPS - 1                     # the last sector: backup boot sector
        self.clusters = self.total_sectors // self.spc
        self.f = open(path, "w+b")
        self.f.truncate(size)
        self.bitmap = bytearray((self.clusters + 7) // 8)
        self.records: dict[int, dict] = {}                        # num -> {seq, flags, attrs builder data}
        self.by_path: dict[str, int] = {"": 5}
        self.mft_lcn = self.clusters // 4
        self.mft_clusters = N_RECORDS * REC // cluster_size
        boot_clusters = max(1, 8192 // cluster_size)
        self._take(0, boot_clusters)                              # $Boot
        self.mirr_lcn = boot_clusters
        self._take(self.mirr_lcn, max(1, 4 * REC // cluster_size))
        # The MFT, in one piece or (as on a well-used drive) several
        per = self.mft_clusters // mft_pieces
        self.mft_runs = []
        for k in range(mft_pieces):
            n = per if k < mft_pieces - 1 else self.mft_clusters - per * (mft_pieces - 1)
            lcn = self.mft_lcn if k == 0 else self.clusters // 2 + k * (per + 7)
            self.mft_runs.append((lcn, n))
            self._take(lcn, n)
        self.bitmap_lcn = self.mft_lcn + self.mft_runs[0][1]
        self.bitmap_clusters = (len(self.bitmap) + cluster_size - 1) // cluster_size
        self._take(self.bitmap_lcn, self.bitmap_clusters)
        self._format()

    # ------------------------------------------------------------ clusters
    def _take(self, lcn: int, n: int) -> None:
        for c in range(lcn, lcn + n):
            self.bitmap[c >> 3] |= 1 << (c & 7)

    def _free(self, lcn: int, n: int) -> None:
        for c in range(lcn, lcn + n):
            self.bitmap[c >> 3] &= ~(1 << (c & 7)) & 0xFF

    def _used(self, c: int) -> bool:
        return bool(self.bitmap[c >> 3] & (1 << (c & 7)))

    def _alloc(self, n: int):
        """First fit: fills holes left by deleted files, so files fragment."""
        runs, c = [], 0
        while n and c < self.clusters:
            if self._used(c):
                c += 1
                continue
            start = c
            while c < self.clusters and not self._used(c) and c - start < n:
                c += 1
            runs.append((start, c - start))
            n -= c - start
        if n:
            raise OSError("volume full (simulator)")
        for lcn, length in runs:
            self._take(lcn, length)
        return runs

    def cluster_offset(self, c: int) -> int:
        return c * self.cs

    # ------------------------------------------------------------ records
    def _write_record(self, num: int) -> None:
        r = self.records[num]
        raw = bytearray(REC)
        usa_off, usa_n = 0x30, REC // BPS + 1
        attrs = b"".join(r["attrs"]) + struct.pack("<I", 0xFFFFFFFF)
        used = 0x38 + len(attrs)
        struct.pack_into("<4sHHQHHHHIIQHHI", raw, 0, b"FILE", usa_off, usa_n, 0, r["seq"], 1, 0x38, r["flags"],
                         (used + 7) & ~7, REC, r.get("base", 0), 0, 0, num)
        raw[0x38:0x38 + len(attrs)] = attrs
        usn = struct.pack("<H", (r["seq"] * 7 + num) & 0xFFFF or 1)
        raw[usa_off:usa_off + 2] = usn
        for i in range(1, usa_n):
            end = i * BPS - 2
            raw[usa_off + 2 * i:usa_off + 2 * i + 2] = raw[end:end + 2]
            raw[end:end + 2] = usn
        self.f.seek(self._record_offset(num))
        self.f.write(raw)
        if num < 4:
            self.f.seek(self.cluster_offset(self.mirr_lcn) + num * REC)
            self.f.write(raw)

    def _record_offset(self, num: int) -> int:
        pos = num * REC
        for lcn, n in self.mft_runs:
            if pos < n * self.cs:
                return self.cluster_offset(lcn) + pos
            pos -= n * self.cs
        raise OSError("record outside the MFT (simulator)")

    def _new_record(self, num: int, attrs, flags: int, seq: int = 1) -> None:
        self.records[num] = {"seq": seq, "flags": flags, "attrs": attrs}
        self._write_record(num)

    def _free_record(self) -> int:
        for num in range(FIRST_USER, N_RECORDS):
            r = self.records.get(num)
            if r is None or not r["flags"] & 1:
                return num
        raise OSError("MFT full (simulator)")

    def _format(self) -> None:
        t = _dt.datetime(2026, 1, 1, 12, 0, 0)
        names = ["$MFT", "$MFTMirr", "$LogFile", "$Volume", "$AttrDef", ".", "$Bitmap", "$Boot", "$BadClus",
                 "$Secure", "$UpCase", "$Extend"]
        for num, name in enumerate(names):
            is_dir = num in (5, 11)
            attrs = [_attr(0x10, _std(t), attr_id=0), _attr(0x30, _fname(5, 5, name, t, 0, is_dir), attr_id=1)]
            if num == 0:
                attrs.append(_attr(0x80, runs=self.mft_runs, real=N_RECORDS * REC,
                                   alloc=N_RECORDS * REC, attr_id=2))
            elif num == 6:
                attrs.append(_attr(0x80, runs=[(self.bitmap_lcn, self.bitmap_clusters)], real=len(self.bitmap),
                                   alloc=self.bitmap_clusters * self.cs, attr_id=2))
            elif is_dir:
                attrs.append(_attr(0x90, _index_root(), name="$I30", attr_id=2))
            self._new_record(num, attrs, 3 if is_dir else 1, seq=num or 1)
        for num in range(12, FIRST_USER):
            self._new_record(num, [], 1, seq=num)
        bs = bytearray(512)
        bs[0:3] = b"\xeb\x52\x90"
        bs[3:11] = b"NTFS    "
        struct.pack_into("<HB", bs, 0x0B, BPS, self.spc)
        bs[0x15] = 0xF8
        struct.pack_into("<QQQ", bs, 0x28, self.total_sectors, self.mft_lcn, self.mirr_lcn)
        bs[0x40] = 0xF6                                          # MFT records: 2**10 bytes
        bs[0x44] = 0x01                                          # index records: one cluster
        struct.pack_into("<Q", bs, 0x48, 0x1C2E3A4B5D6E7F80)
        bs[510:512] = b"\x55\xaa"
        self.f.seek(0)
        self.f.write(bs)
        self.f.seek(self.total_sectors * BPS)
        self.f.write(bs)

    # ------------------------------------------------------------ files
    def mkdir(self, path: str, t: _dt.datetime | None = None) -> int:
        path = path.strip("/")
        if path in self.by_path:
            return self.by_path[path]
        parent_path, _, name = path.rpartition("/")
        parent = self.mkdir(parent_path, t) if parent_path else 5
        t = t or _dt.datetime(2026, 1, 1, 12, 0, 0)
        num = self._free_record()
        seq = (self.records[num]["seq"] if num in self.records else 1)
        attrs = [_attr(0x10, _std(t), attr_id=0),
                 _attr(0x30, _fname(parent, self.records[parent]["seq"], name, t, 0, True), attr_id=1),
                 _attr(0x90, _index_root(), name="$I30", attr_id=2)]
        self._new_record(num, attrs, 3, seq=seq)
        self.records[num]["parent"], self.records[num]["name"] = parent, name
        self.by_path[path] = num
        return num

    def write_file(self, path: str, data: bytes, t: _dt.datetime | None = None, resident_max: int = 600):
        """Write a file; returns its runs [(lcn, clusters)] (empty when the
        data is small enough to live inside the record)."""
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        parent = self.mkdir(parent_path, t) if parent_path else 5
        t = t or _dt.datetime(2026, 1, 1, 12, 0, 0)
        runs = []
        if len(data) > resident_max:
            runs = self._alloc((len(data) + self.cs - 1) // self.cs)
            pos = 0
            for lcn, length in runs:
                self.f.seek(self.cluster_offset(lcn))
                self.f.write(data[pos:pos + length * self.cs])
                pos += length * self.cs
            dattr = _attr(0x80, runs=runs, real=len(data), alloc=sum(n for _, n in runs) * self.cs, attr_id=2)
        else:
            dattr = _attr(0x80, data, attr_id=2)
        num = self._free_record()
        seq = self.records[num]["seq"] if num in self.records else 1
        attrs = [_attr(0x10, _std(t), attr_id=0),
                 _attr(0x30, _fname(parent, self.records[parent]["seq"], name, t, len(data), False), attr_id=1),
                 dattr]
        self._new_record(num, attrs, 1, seq=seq)
        self.records[num].update(parent=parent, name=name, runs=runs, t=t, size=len(data))
        self.by_path[path] = num
        return runs

    def delete(self, path: str) -> None:
        """Like Windows: clear "in use", bump the sequence number, free the
        clusters. The record keeps its name, parent and runlist."""
        num = self.by_path.pop(path.strip("/"))
        r = self.records[num]
        r["flags"] &= ~1
        r["seq"] = (r["seq"] + 1) & 0xFFFF or 1
        self._write_record(num)
        for lcn, length in r.get("runs", []):
            self._free(lcn, length)

    def move(self, path: str, new_path: str) -> None:
        """Rename or move: the same record, a new name and parent."""
        path, new_path = path.strip("/"), new_path.strip("/")
        num = self.by_path.pop(path)
        r = self.records[num]
        parent_path, _, name = new_path.rpartition("/")
        parent = self.mkdir(parent_path, r["t"]) if parent_path else 5
        r["attrs"][1] = _attr(0x30, _fname(parent, self.records[parent]["seq"], name, r["t"], r["size"], False),
                              attr_id=1)
        r["parent"], r["name"] = parent, name
        self._write_record(num)
        self.by_path[new_path] = num

    def allocated_clusters(self) -> set:
        return {c for c in range(self.clusters) if self._used(c)}

    def close(self) -> None:
        self.f.seek(self.cluster_offset(self.bitmap_lcn))
        self.f.write(bytes(self.bitmap))
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
