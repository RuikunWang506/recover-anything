"""Find partitions and identify filesystems (including ones we can only warn about)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .fs import fat as fatfs
from .fs import exfat as exfatfs


@dataclass
class Volume:
    offset: int
    size: int
    fs: str          # 'FAT12'|'FAT16'|'FAT32'|'exFAT'|'NTFS'|'APFS'|'HFS+'|'ext'|'BitLocker'|'LUKS'|'unknown'
    label: str = ""
    note: str = ""
    from_backup: bool = False


ENCRYPTED = {"BitLocker", "LUKS", "APFS (encrypted?)"}


def identify(src, off: int) -> tuple[str, str]:
    bs = src.read(off, 4096)
    if len(bs) < 512:
        return "unknown", ""
    if bs[3:11] == b"-FVE-FS-":
        return "BitLocker", ""
    if bs[:6] == b"LUKS\xba\xbe":
        return "LUKS", ""
    if bs[3:11] == b"EXFAT   ":
        return "exFAT", ""
    if bs[3:11] == b"NTFS    ":
        return "NTFS", ""
    if bs[32:36] == b"NXSB":
        return "APFS", ""
    if bs[1024:1026] in (b"H+", b"HX"):
        return "HFS+", ""
    if len(bs) >= 1082 and bs[1080:1082] == b"\x53\xef":
        return "ext", ""
    if fatfs.probe(bs):
        try:
            v = fatfs.FatVolume(src, off)
            return v.kind, v.label
        except Exception:
            return "FAT?", ""
    return "unknown", ""


def _mbr_parts(src) -> list[tuple[int, int, int]]:
    mbr = src.read(0, 512)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return []
    if fatfs.probe(mbr) or mbr[3:11] in (b"EXFAT   ", b"NTFS    "):
        return []  # a boot sector, not a partition table ("superfloppy" card)
    parts = []
    for i in range(4):
        e = mbr[446 + 16 * i:462 + 16 * i]
        typ = e[4]
        start, count = struct.unpack("<II", e[8:16])
        if typ and count and e[0] in (0x00, 0x80):
            parts.append((typ, start * 512, count * 512))
    return parts


def _gpt_parts(src) -> list[tuple[int, int, int]]:
    hdr = src.read(512, 92)
    if hdr[:8] != b"EFI PART":
        return []
    lba, n, esize = struct.unpack("<QII", hdr[72:88])
    out = []
    table = src.read(lba * 512, n * esize)
    for i in range(n):
        e = table[i * esize:(i + 1) * esize]
        if len(e) < 48 or e[:16] == bytes(16):
            continue
        first, last = struct.unpack("<QQ", e[32:48])
        out.append((0xEE, first * 512, (last - first + 1) * 512))
    return out


def find_volumes(src) -> list[Volume]:
    parts = _gpt_parts(src) or [(t, o, s) for t, o, s in _mbr_parts(src) if t != 0xEE]
    vols = []
    if parts:
        for _t, off, size in parts:
            fs, label = identify(src, off)
            if fs == "unknown":
                fs2, label2, boff = _backup_boot(src, off)
                if fs2 != "unknown":
                    vols.append(Volume(off, size, fs2, label2, "main boot sector is damaged; using the backup copy",
                                       from_backup=True))
                    continue
            vols.append(Volume(off, size, fs, label))
        return vols
    fs, label = identify(src, 0)
    if fs == "unknown":
        fs2, label2, _ = _backup_boot(src, 0)
        if fs2 != "unknown":
            return [Volume(0, src.size, fs2, label2, "main boot sector is damaged; using the backup copy", True)]
        # A card wiped of its partition table often still has a boot sector at
        # the usual SD alignment.
        for guess in (4 << 20, 8 << 20, 1 << 20, 32256, 16 << 20):
            if guess < src.size:
                g, gl = identify(src, guess)
                if g != "unknown":
                    return [Volume(guess, src.size - guess, g, gl, "partition table missing; found the filesystem anyway")]
    return [Volume(0, src.size, fs, label)]


def _backup_boot(src, off: int) -> tuple[str, str, int]:
    # FAT32 keeps a copy at sector 6, exFAT at sector 12.
    for sec in (6, 12):
        bs = src.read(off + sec * 512, 512)
        if bs[3:11] == b"EXFAT   " and sec == 12:
            return "exFAT", "", off + sec * 512
        if fatfs.probe(bs) and sec == 6:
            return "FAT32", "", off + sec * 512
    return "unknown", "", 0


def open_filesystem(src, vol: Volume):
    """Return a filesystem reader for volumes we can parse, else None."""
    from .source import SubSource
    if vol.fs.startswith("FAT"):
        if vol.from_backup:
            return fatfs.FatVolume(_Patched(src, vol.offset, 6), vol.offset)
        return fatfs.FatVolume(src, vol.offset)
    if vol.fs == "exFAT":
        if vol.from_backup:
            return exfatfs.ExfatVolume(_Patched(src, vol.offset, 12), vol.offset)
        return exfatfs.ExfatVolume(src, vol.offset)
    _ = SubSource
    return None


class _Patched:
    """Read-through view that serves the backup boot sector in place of the
    damaged main one. Nothing is ever written to the source."""

    def __init__(self, src, vol_off: int, backup_sector: int):
        self.src, self.vol_off = src, vol_off
        self.boot = src.read(vol_off + backup_sector * 512, 512)
        self.size, self.path = src.size, getattr(src, "path", "?")

    def read(self, off: int, n: int) -> bytes:
        data = self.src.read(off, n)
        s, e = self.vol_off, self.vol_off + 512
        if off < e and off + n > s:
            b = bytearray(data)
            lo, hi = max(off, s), min(off + n, e)
            b[lo - off:hi - off] = self.boot[lo - s:hi - s]
            data = bytes(b)
        return data

    def read_uncached(self, off: int, n: int) -> bytes:
        return self.read(off, n)
