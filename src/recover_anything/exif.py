"""Capture dates and embedded thumbnails, read without any dependencies."""
from __future__ import annotations

import datetime as _dt
import struct


def _tiff_date(t: bytes):
    if len(t) < 8 or t[:2] not in (b"II", b"MM"):
        return None, None
    bo = "<" if t[:2] == b"II" else ">"

    def ifd(off):
        if off + 2 > len(t):
            return {}
        n = struct.unpack(bo + "H", t[off:off + 2])[0]
        out = {}
        for i in range(min(n, 512)):
            e = off + 2 + 12 * i
            if e + 12 > len(t):
                break
            tag, typ, count, val = struct.unpack(bo + "HHII", t[e:e + 12])
            out[tag] = (typ, count, val, e + 8)
        nxt_at = off + 2 + 12 * n
        out["_next"] = struct.unpack(bo + "I", t[nxt_at:nxt_at + 4])[0] if nxt_at + 4 <= len(t) else 0
        return out

    def ascii(entry):
        typ, count, val, inline = entry
        raw = t[inline:inline + count] if count <= 4 else t[val:val + count]
        return raw.split(b"\x00")[0].decode("ascii", "replace").strip()

    ifd0 = ifd(struct.unpack(bo + "I", t[4:8])[0])
    stamp = None
    if 0x8769 in ifd0:
        ex = ifd(ifd0[0x8769][2])
        for tag in (0x9003, 0x9004):
            if tag in ex:
                stamp = ascii(ex[tag])
                break
    if not stamp and 0x0132 in ifd0:
        stamp = ascii(ifd0[0x0132])
    when = None
    if stamp:
        try:
            when = _dt.datetime.strptime(stamp[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            when = None
    thumb = None
    if ifd0.get("_next"):
        ifd1 = ifd(ifd0["_next"])
        if 0x0201 in ifd1 and 0x0202 in ifd1:
            o, n = ifd1[0x0201][2], ifd1[0x0202][2]
            if 0 < n < 1 << 20 and o + n <= len(t) and t[o:o + 2] == b"\xff\xd8":
                thumb = t[o:o + n]
    return when, thumb


def jpeg_exif(data: bytes):
    """(capture datetime, embedded thumbnail bytes) from a JPEG's APP1."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        m = data[pos + 1]
        if m == 0xDA or m == 0xD9:
            break
        ln = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if m == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00":
            return _tiff_date(data[pos + 10:pos + 2 + ln])
        pos += 2 + ln
    return None, None


def mp4_date(head: bytes):
    """Creation time from an ISO-BMFF 'mvhd' box (seconds since 1904)."""
    i = head.find(b"mvhd")
    if i < 4:
        return None
    ver = head[i + 4]
    try:
        secs = struct.unpack(">Q", head[i + 8:i + 16])[0] if ver == 1 else struct.unpack(">I", head[i + 8:i + 12])[0]
    except struct.error:
        return None
    if secs < 86400 * 365 * 70:  # before ~1974: unset
        return None
    try:
        return _dt.datetime(1904, 1, 1) + _dt.timedelta(seconds=secs)
    except OverflowError:
        return None


def capture_time(kind: str, read, length: int):
    """Best capture time for a file, given read(off, n) over its bytes."""
    try:
        if kind == "jpeg":
            return jpeg_exif(read(0, min(length, 256 * 1024)))[0]
        if kind == "tiff":
            return _tiff_date(read(0, min(length, 1 << 20)))[0]
        if kind == "isobmff":
            head = read(0, min(length, 1 << 20))
            if b"moov" not in head and length > (1 << 20):
                head = read(max(0, length - (8 << 20)), 8 << 20)
            return mp4_date(head)
    except Exception:
        return None
    return None
