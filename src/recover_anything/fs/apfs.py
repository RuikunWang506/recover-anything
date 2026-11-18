"""Just enough APFS to tell whether its volumes are encrypted.

Every APFS volume's superblock carries a flag, APFS_FS_UNENCRYPTED. Deleted
data on an encrypted volume (FileVault, an encrypted external drive, every
internal volume of an Apple-silicon Mac) can't be carved; on an unencrypted
one (most external drives formatted APFS) it can. Triage used to call every
APFS volume encrypted.

The way there: the newest container superblock (NXSB) from the checkpoint
area, its object map (a B-tree from virtual object ids to block addresses),
then each volume's superblock (APSB) through that map. Every block is checked
against its Fletcher-64 checksum; anything unexpected gives up (None).
"""
from __future__ import annotations

import struct
from typing import Optional

FS_UNENCRYPTED = 0x1
_ROOT, _LEAF, _FIXED = 1, 2, 4
_OBJ_NX, _OBJ_BTREE, _OBJ_NODE, _OBJ_OMAP, _OBJ_FS = 0x1, 0x2, 0x3, 0xB, 0xD


def _checksum_ok(blk: bytes) -> bool:
    """Fletcher-64 over 32-bit words, as APFS stores it in a block's first 8 bytes."""
    if len(blk) < 64 or blk[:8] in (bytes(8), b"\xff" * 8):
        return False
    words = struct.unpack_from("<%dI" % ((len(blk) - 8) // 4), blk, 8)
    s1 = s2 = 0
    m = 0xFFFFFFFF
    for w in words:
        s1 = (s1 + w) % m
        s2 = (s2 + s1) % m
    c1 = m - ((s1 + s2) % m)
    c2 = m - ((s1 + c1) % m)
    return struct.unpack_from("<Q", blk, 0)[0] == (c2 << 32) | c1


def _otype(blk: bytes) -> int:
    return struct.unpack_from("<I", blk, 24)[0] & 0xFFFF


def _xid(blk: bytes) -> int:
    return struct.unpack_from("<Q", blk, 16)[0]


class _Container:
    def __init__(self, src, off: int):
        self.src, self.off = src, off
        sb = src.read(off, 4096)
        if sb[32:36] != b"NXSB":
            raise ValueError("not APFS")
        self.bs = struct.unpack_from("<I", sb, 36)[0]
        if self.bs not in (4096, 8192, 16384, 32768, 65536):
            raise ValueError("odd block size")
        self.blocks = struct.unpack_from("<Q", sb, 40)[0]
        sb = self.block(0)
        best = sb if _checksum_ok(sb) else None
        desc_blocks, = struct.unpack_from("<I", sb, 104)
        desc_base, = struct.unpack_from("<Q", sb, 112)
        if not desc_blocks & 0x80000000 and 0 < desc_blocks <= 4096:   # not a tree: a plain run of blocks
            for i in range(desc_blocks):
                b = self.block(desc_base + i)
                if b[32:36] == b"NXSB" and _otype(b) == _OBJ_NX and _checksum_ok(b) and (
                        best is None or _xid(b) > _xid(best)):
                    best = b
        if best is None:
            raise ValueError("no good container superblock")
        self.sb = best

    def block(self, n: int) -> bytes:
        if not 0 <= n < max(self.blocks, 1):
            raise ValueError("block %d outside the container" % n)
        return self.src.read(self.off + n * self.bs, self.bs)

    def _omap(self, omap_oid: int, xid: int) -> dict:
        """Virtual oid -> block address, newest version at or before xid."""
        om = self.block(omap_oid)
        if _otype(om) != _OBJ_OMAP or not _checksum_ok(om):
            raise ValueError("bad object map")
        found: dict = {}
        stack, seen = [struct.unpack_from("<Q", om, 48)[0]], 0
        while stack:
            seen += 1
            if seen > 4096:
                raise ValueError("object map too big")
            node = self.block(stack.pop())
            if _otype(node) not in (_OBJ_BTREE, _OBJ_NODE) or not _checksum_ok(node):
                raise ValueError("bad object map node")
            flags, level, nkeys = struct.unpack_from("<HHI", node, 32)
            toff, tlen = struct.unpack_from("<HH", node, 40)
            if not flags & _FIXED or nkeys > self.bs // 4:
                raise ValueError("unexpected object map layout")
            keys = 56 + tlen
            vend = self.bs - (40 if flags & _ROOT else 0)
            for i in range(nkeys):
                k, v = struct.unpack_from("<HH", node, 56 + toff + 4 * i)
                oid, kxid = struct.unpack_from("<QQ", node, keys + k)
                if level == 0:
                    _vflags, _size, paddr = struct.unpack_from("<IIQ", node, vend - v)
                    if kxid <= xid and (oid not in found or found[oid][0] < kxid):
                        found[oid] = (kxid, paddr)
                else:
                    stack.append(struct.unpack_from("<Q", node, vend - v)[0])
        return {oid: paddr for oid, (_x, paddr) in found.items()}

    def volumes(self) -> list:
        """[(name, encrypted)] for every volume in the container."""
        sb = self.sb
        omap = self._omap(struct.unpack_from("<Q", sb, 160)[0], _xid(sb))
        max_fs = min(struct.unpack_from("<I", sb, 180)[0], 100)
        out = []
        for i in range(max_fs):
            oid = struct.unpack_from("<Q", sb, 184 + 8 * i)[0]
            if not oid:
                continue
            if oid not in omap:
                raise ValueError("volume %d isn't in the object map" % i)
            v = self.block(omap[oid])
            if v[32:36] != b"APSB" or _otype(v) != _OBJ_FS or not _checksum_ok(v):
                raise ValueError("bad volume superblock")
            flags = struct.unpack_from("<Q", v, 264)[0]
            name = v[704:704 + 256].split(b"\x00", 1)[0].decode("utf-8", "replace")
            out.append((name, not flags & FS_UNENCRYPTED))
        return out


def volumes(src, off: int) -> Optional[list]:
    """[(name, encrypted)] for the APFS container at off, or None when it
    can't be read with confidence."""
    try:
        return _Container(src, off).volumes()
    except (ValueError, struct.error, OSError):
        return None
