"""Read-only access to a card, disk or image file.

The one rule this whole project is built on: we never write to the source.
`Source` only ever opens with O_RDONLY, there is no write method, and reads
that fail (bad sectors) come back as zeros and are logged instead of retried
forever, which is what keeps a failing card alive long enough to image it.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from collections import OrderedDict

SECTOR = 512


def is_block_device(path: str) -> bool:
    if sys.platform == "win32":
        p = path.replace("/", "\\")
        return p.startswith("\\\\.\\")
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


def _device_size(fd: int, path: str) -> int:
    size = 0
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        size = 0
    if size > 0:
        return size
    if sys.platform == "darwin":
        try:
            import fcntl
            import struct
            buf = fcntl.ioctl(fd, 0x40046418, b"\0" * 4)  # DKIOCGETBLOCKSIZE
            bs = struct.unpack("I", buf)[0]
            buf = fcntl.ioctl(fd, 0x40086419, b"\0" * 8)  # DKIOCGETBLOCKCOUNT
            return bs * struct.unpack("Q", buf)[0]
        except Exception:
            pass
        try:
            import plistlib
            out = subprocess.run(["diskutil", "info", "-plist", path], capture_output=True, check=True).stdout
            return int(plistlib.loads(out).get("TotalSize", 0))
        except Exception:
            return 0
    if sys.platform == "win32":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            handle = msvcrt.get_osfhandle(fd)
            length = ctypes.c_longlong(0)
            returned = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(handle), 0x7405C, None, 0, ctypes.byref(length), 8, ctypes.byref(returned), None)
            if ok:
                return int(length.value)
        except Exception:
            return 0
    return size


class Source:
    """Random-access, read-only, cached view of a device or image file."""

    BLOCK = 1 << 20

    def __init__(self, path: str, cache_blocks: int = 96):
        self.path = path
        self.is_device = is_block_device(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        self._fd = os.open(path, flags)
        if self.is_device:
            self.size = _device_size(self._fd, path)
        else:
            self.size = os.fstat(self._fd).st_size
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_blocks = cache_blocks
        self._lock = threading.Lock()
        self.bad_ranges: list[tuple[int, int]] = []
        if self.size <= 0 and self.is_device:
            self.size = 0

    # Raw devices (macOS /dev/rdisk*, Windows \\.\PhysicalDrive*) only accept
    # reads whose offset and length are multiples of the sector size, which
    # can be 512 or 4096 bytes. Device reads are therefore always widened to
    # 4096-byte boundaries and trimmed afterwards.
    ALIGN = 4096

    def _pread(self, off: int, n: int) -> bytes:
        with self._lock:
            if hasattr(os, "pread"):
                return os.pread(self._fd, n, off)
            os.lseek(self._fd, off, os.SEEK_SET)
            return os.read(self._fd, n)

    def _raw(self, off: int, n: int, align: int = 0) -> bytes:
        if not self.is_device:
            return self._pread(off, n)
        align = align or self.ALIGN
        a0 = off - off % align
        a1 = -(-(off + n) // align) * align
        if self.size:
            a1 = min(a1, self.size)
        data = self._pread(a0, a1 - a0)
        return data[off - a0:off - a0 + n]

    def _block(self, idx: int) -> bytes:
        blk = self._cache.get(idx)
        if blk is not None:
            self._cache.move_to_end(idx)
            return blk
        off = idx * self.BLOCK
        n = min(self.BLOCK, self.size - off)
        try:
            blk = self._raw(off, n)
        except OSError:
            blk = self._careful_read(off, n)
        if len(blk) < n:
            blk = blk + bytes(n - len(blk))
        self._cache[idx] = blk
        if len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)
        return blk

    def _careful_read(self, off: int, n: int) -> bytes:
        """Read around bad spots: 4 KB at a time, then sector by sector inside a
        failing 4 KB. Unreadable sectors become zeros and are logged."""
        out = bytearray()
        end = off + n
        pos = off
        while pos < end:
            step = min(self.ALIGN - pos % self.ALIGN, end - pos)
            try:
                out += self._raw(pos, step)
            except OSError:
                for s in range(pos, pos + step, SECTOR):
                    k = min(SECTOR, pos + step - s)
                    try:
                        out += self._raw(s, k, align=SECTOR)
                    except OSError:
                        out += bytes(k)
                        self.bad_ranges.append((s, k))
            pos += step
        return bytes(out)

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        n = min(n, self.size - off)
        first, last = off // self.BLOCK, (off + n - 1) // self.BLOCK
        if first == last:
            b = self._block(first)
            s = off - first * self.BLOCK
            return b[s:s + n]
        parts = []
        for idx in range(first, last + 1):
            b = self._block(idx)
            s = off - idx * self.BLOCK if idx == first else 0
            e = off + n - idx * self.BLOCK if idx == last else self.BLOCK
            parts.append(b[s:e])
        return b"".join(parts)

    def read_uncached(self, off: int, n: int) -> bytes:
        """Big sequential reads (scans) bypass the cache to avoid thrashing it."""
        if off >= self.size:
            return b""
        n = min(n, self.size - off)
        try:
            data = self._raw(off, n)
        except OSError:
            data = self._careful_read(off, n)
        return data + bytes(n - len(data)) if len(data) < n else data

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SubSource:
    """A window into a Source (e.g. one partition)."""

    def __init__(self, parent, offset: int, size: int):
        self.parent, self.offset, self.size = parent, offset, size
        self.path = getattr(parent, "path", "?")
        self.is_device = getattr(parent, "is_device", False)

    def read(self, off: int, n: int) -> bytes:
        if off >= self.size:
            return b""
        return self.parent.read(self.offset + off, min(n, self.size - off))

    def read_uncached(self, off: int, n: int) -> bytes:
        if off >= self.size:
            return b""
        return self.parent.read_uncached(self.offset + off, min(n, self.size - off))
