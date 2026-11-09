"""Test files other than photos and videos, built from fixed seeds with the
standard library only: music, voice memos, dashcam clips, screen recordings,
phone databases, backups and pictures from other devices. Each one is a valid
file of its type (real checksums, real structure); the audio and video
payloads are noise, which is all recovery needs."""
from __future__ import annotations

import gzip
import hashlib
import os
import random
import sqlite3
import struct
import tempfile
import zlib

from ..formats.more import _CRC16, crc8, crc32_msb


def _rng(kind: str, seed: int) -> random.Random:
    return random.Random("%s-%d" % (kind, seed))


def _noise(rng: random.Random, n: int) -> bytes:
    return rng.getrandbits(8 * n).to_bytes(n, "little") if n else b""


# ------------------------------------------------------------------ MP3
def make_mp3(seed: int, seconds: float = 20.0, id3: bool = True, xing: bool = True, id3v1: bool = False,
             kbps: int = 128) -> bytes:
    """MPEG-1 Layer III, 44.1 kHz, constant bit rate, LAME-style Info frame."""
    rng = _rng("mp3", seed)
    bri = {32: 1, 64: 5, 96: 7, 128: 9, 192: 11, 256: 13, 320: 14}[kbps]
    nframes = int(seconds * 44100 / 1152)
    frames, acc = [], 0
    for i in range(nframes + (1 if xing else 0)):
        size = 144 * kbps * 1000 // 44100
        acc += 144 * kbps * 1000 % 44100
        pad = 1 if acc >= 44100 else 0
        acc -= 44100 if pad else 0
        hdr = bytes([0xFF, 0xFB, (bri << 4) | (pad << 1), 0x44])
        frames.append([hdr, size + pad])
    body = []
    for i, (hdr, n) in enumerate(frames):
        if xing and i == 0:
            continue
        body.append(hdr + _noise(rng, n - 4))
    audio = b"".join(body)
    out = []
    if xing:
        hdr, n = frames[0]
        info = hdr + b"\0" * 32 + b"Info" + struct.pack(">III", 3, nframes, n + len(audio))
        out.append(info + b"\0" * (n - len(info)))
    out.append(audio)
    data = b"".join(out)
    if id3:
        title = ("Track %d" % seed).encode()
        frame = b"TIT2" + struct.pack(">I", len(title) + 1) + b"\0\0" + b"\0" + title
        pad = rng.randint(0, 2000)
        size = len(frame) + pad
        ss = bytes([(size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127])
        data = b"ID3\x04\x00\x00" + ss + frame + b"\0" * pad + data
    if id3v1:
        data += b"TAG" + ("Track %d" % seed).encode().ljust(125, b"\0")
    return data


# ------------------------------------------------------------------ FLAC
def _crc16(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC16[(c >> 8) ^ b]
    return c


def _utf8num(v: int) -> bytes:
    if v < 0x80:
        return bytes([v])
    if v < 0x800:
        return bytes([0xC0 | (v >> 6), 0x80 | (v & 63)])
    return bytes([0xE0 | (v >> 12), 0x80 | ((v >> 6) & 63), 0x80 | (v & 63)])


def make_flac(seed: int, seconds: float = 10.0, block: int = 4096) -> bytes:
    """Mono 16-bit 44.1 kHz with verbatim subframes and real CRCs and MD5."""
    rng = _rng("flac", seed)
    total = int(seconds * 44100)
    frames, md5, pos, idx = [], hashlib.md5(), 0, 0
    while pos < total:
        bs = min(block, total - pos)
        raw = _noise(rng, 2 * bs)
        samples = struct.unpack(">%dh" % bs, raw)
        md5.update(struct.pack("<%dh" % bs, *samples))
        if bs == 4096:
            hdr = bytes([0xFF, 0xF8, 0xC9, 0x08]) + _utf8num(idx)
        else:
            hdr = bytes([0xFF, 0xF8, 0x79, 0x08]) + _utf8num(idx) + struct.pack(">H", bs - 1)
        hdr += bytes([crc8(hdr)])
        fr = hdr + b"\x02" + raw
        frames.append(fr + struct.pack(">H", _crc16(fr)))
        pos += bs
        idx += 1
    sizes = [len(f) for f in frames]
    info = struct.pack(">HH", block, block) + min(sizes).to_bytes(3, "big") + max(sizes).to_bytes(3, "big")
    info += ((44100 << 44) | (0 << 41) | (15 << 36) | total).to_bytes(8, "big") + md5.digest()
    vendor = b"recover-anything test"
    comment = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    meta = bytes([0x00]) + len(info).to_bytes(3, "big") + info
    meta += bytes([0x84]) + len(comment).to_bytes(3, "big") + comment
    return b"fLaC" + meta + b"".join(frames)


# ------------------------------------------------------------------ Ogg Opus
def _ogg_page(serial: int, seq: int, granule: int, flags: int, packets: list) -> bytes:
    segs, body = [], b""
    for p in packets:
        n = len(p)
        segs += [255] * (n // 255) + [n % 255]
        body += p
    hdr = b"OggS\x00" + bytes([flags]) + struct.pack("<qII", granule, serial, seq) + b"\0\0\0\0"
    page = hdr + bytes([len(segs)]) + bytes(segs) + body
    crc = crc32_msb(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def make_ogg(seed: int, seconds: float = 15.0) -> bytes:
    """An Opus voice memo: 20 ms packets, about 1 second per page."""
    rng = _rng("ogg", seed)
    serial = rng.getrandbits(32)
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 312, 48000, 0, 0)
    tags = b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0)
    pages = [_ogg_page(serial, 0, 0, 2, [head]), _ogg_page(serial, 1, 0, 0, [tags])]
    npk = int(seconds * 50)
    seq, gran, i = 2, 0, 0
    while i < npk:
        group = [_noise(rng, rng.randint(40, 120)) for _ in range(min(50, npk - i))]
        i += len(group)
        gran += 960 * len(group)
        pages.append(_ogg_page(serial, seq, gran, 4 if i >= npk else 0, group))
        seq += 1
    return b"".join(pages)


# ------------------------------------------------------------------ MPEG-TS
def _psi(pid: int, table: bytes, cc: int) -> bytes:
    sec = table + struct.pack(">I", crc32_msb(table, 0xFFFFFFFF))
    pkt = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | cc, 0]) + sec
    return pkt + b"\xff" * (188 - len(pkt))


def make_ts(seed: int, packets: int = 4000, m2ts: bool = False) -> bytes:
    """A dashcam-style transport stream (or an AVCHD .MTS with 192-byte packets):
    PAT and PMT up front and every 500 packets, video on PID 0x100 carrying the
    programme clock (PCR) every 30 packets, audio on PID 0x101 mixed in
    irregularly. Each recording's clocks start at a random point, as a
    camera's free-running clock would."""
    rng = _rng("ts", seed)
    pat = bytes([0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0]) + bytes([0, 1, 0xE1, 0x00])
    pmt = bytes([0x02, 0xB0, 23, 0, 1, 0xC1, 0, 0, 0xE1, 0x00, 0xF0, 0]) + bytes([0x1B, 0xE1, 0x00, 0xF0, 0]) \
        + bytes([0x0F, 0xE1, 0x01, 0xF0, 0])
    out, ccs = [], {0: 0, 0x100: 0, 0x101: 0, 0x1000: 0}
    ats = rng.getrandbits(30)
    pcr = rng.getrandbits(33) * 300  # 27 MHz
    per_packet = 5076  # 188 bytes at 8 Mbit/s, in 27 MHz ticks
    video = 0
    for i in range(packets):
        if i % 500 == 0:
            pk = [_psi(0, pat, ccs[0]), _psi(0x1000, pmt, ccs[0x1000])]
            ccs[0] = (ccs[0] + 1) & 15
            ccs[0x1000] = (ccs[0x1000] + 1) & 15
        elif rng.random() < 0.12:
            pk = [bytes([0x47, 0x41 if rng.random() < 0.1 else 0x01, 0x01, 0x10 | ccs[0x101]]) + _noise(rng, 184)]
            ccs[0x101] = (ccs[0x101] + 1) & 15
        else:
            pusi = 0x40 if video % 50 == 0 else 0
            if video % 30 == 0:  # adaptation field with the programme clock
                base, ext = (pcr // 300) & ((1 << 33) - 1), pcr % 300
                af = bytes([7, 0x10]) + ((base << 15) | (0x3F << 9) | ext).to_bytes(6, "big")
                pk = [bytes([0x47, pusi | 0x01, 0x00, 0x30 | ccs[0x100]]) + af + _noise(rng, 184 - len(af))]
            else:
                pk = [bytes([0x47, pusi | 0x01, 0x00, 0x10 | ccs[0x100]]) + _noise(rng, 184)]
            ccs[0x100] = (ccs[0x100] + 1) & 15
            video += 1
        for p in pk:
            pcr += per_packet + rng.randint(-200, 200)
            if m2ts:
                ats = (ats + 1728 + rng.randint(0, 200)) & 0x3FFFFFFF  # 192 bytes at about 24 Mbit/s
                out.append(struct.pack(">I", ats) + p)
            else:
                out.append(p)
    return b"".join(out)


# ------------------------------------------------------------------ Matroska / WebM
def _ebml_size(n: int) -> bytes:
    for k in range(1, 8):
        if n < (1 << (7 * k)) - 1:
            return ((1 << (7 * k)) | n).to_bytes(k, "big")
    return (0x01 << 56 | n).to_bytes(8, "big")


def _el(eid: int, payload: bytes) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big") + _ebml_size(len(payload)) + payload


def make_mkv(seed: int, clusters: int = 12, webm: bool = False) -> bytes:
    rng = _rng("mkv", seed)
    doctype = b"webm" if webm else b"matroska"
    ebml = _el(0x1A45DFA3, _el(0x4286, b"\x01") + _el(0x4282, doctype) + _el(0x4287, b"\x04") + _el(0x4285, b"\x02"))
    info = _el(0x1549A966, _el(0x2AD7B1, struct.pack(">I", 1000000)) + _el(0x4D80, b"test"))
    avcc = bytes([1, 0x64, 0, 0x28, 0xFF, 0xE1]) + struct.pack(">H", 4) + b"\x67\x64\x00\x28" + b"\x01" \
        + struct.pack(">H", 4) + b"\x68\xee\x3c\x80"
    track = _el(0xAE, _el(0xD7, b"\x01") + _el(0x83, b"\x01") + _el(0x86, b"V_VP9" if webm else b"V_MPEG4/ISO/AVC")
                + (b"" if webm else _el(0x63A2, avcc)))
    tracks = _el(0x1654AE6B, track)
    body = [info, tracks]
    for c in range(clusters):
        blocks = [_el(0xE7, struct.pack(">H", c * 1000))]
        for f in range(25):
            size = rng.randint(30000, 90000) if f == 0 else rng.randint(1500, 9000)
            if webm:
                frame = _noise(rng, size)
            else:  # H.264 in Matroska: NAL units with 4-byte lengths, slices of about 1-4 KB
                parts, left = [], size
                while left > 0:
                    n = min(left, rng.randint(1000, 4000))
                    parts.append(struct.pack(">I", n + 1) + (b"\x65" if f == 0 else b"\x41") + _noise(rng, n))
                    left -= n
                frame = b"".join(parts)
            blocks.append(_el(0xA3, b"\x81" + struct.pack(">h", f * 40) + (b"\x80" if f == 0 else b"\x00") + frame))
        body.append(_el(0x1F43B675, b"".join(blocks)))
    return ebml + _el(0x18538067, b"".join(body))


# ------------------------------------------------------------------ SQLite
def make_sqlite(seed: int, rows: int = 400) -> bytes:
    """A phone-style message database with an index."""
    rng = _rng("sqlite", seed)
    words = ("see you at the station", "ok", "happy birthday!", "running late", "call me", "photos from today",
             "where are you", "thanks", "on my way", "dinner at 7?")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA page_size=4096")
        con.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, sender TEXT, body TEXT, sent INTEGER)")
        con.execute("CREATE INDEX message_sent ON message(sent)")
        t = 1780000000 + seed * 100000
        for i in range(rows):
            t += rng.randint(5, 4000)
            con.execute("INSERT INTO message (sender, body, sent) VALUES (?, ?, ?)",
                        ("+1555%07d" % rng.randint(0, 9999999), rng.choice(words) + " " * rng.randint(0, 200), t))
        con.commit()
        con.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


# ------------------------------------------------------------------ gzip
def make_gzip(seed: int, lines: int = 20000) -> bytes:
    """A compressed log file, as a router, camera or NAS keeps."""
    rng = _rng("gzip", seed)
    text = "".join("2026-07-04T%02d:%02d:%02d device[%d]: %s\n" % (
        rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59), rng.randint(100, 999),
        rng.choice(("link up", "link down", "dhcp lease renewed", "motion detected", "backup ok", "disk 71%"))
    ) for _ in range(lines))
    return gzip.compress(text.encode(), mtime=0)


# ------------------------------------------------------------------ BMP
def make_bmp(seed: int, width: int = 320, height: int = 240) -> bytes:
    rng = _rng("bmp", seed)
    row = (width * 3 + 3) & ~3
    pix = bytearray()
    a, b = rng.randint(0, 255), rng.randint(0, 255)
    for y in range(height):
        line = bytes(((x + a) & 255, (y + b) & 255, (x ^ y) & 255)[k] for x in range(width) for k in range(3))
        pix += line + b"\0" * (row - width * 3)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pix), 2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", 14 + 40 + len(pix), 0, 0, 54) + dib + bytes(pix)


# ------------------------------------------------------------------ HEIC (structure only)
def make_heic(seed: int, size: int = 900000) -> bytes:
    """An iPhone-style HEIC container: ftyp, meta, mdat (the image data is noise)."""
    rng = _rng("heic", seed)
    ftyp = struct.pack(">I", 24) + b"ftypheic" + struct.pack(">I", 0) + b"mif1heic"
    meta_body = b"\0\0\0\0" + struct.pack(">I", 33) + b"hdlr" + b"\0" * 8 + b"pict" + b"\0" * 13
    meta = struct.pack(">I", 8 + len(meta_body)) + b"meta" + meta_body
    mdat = struct.pack(">I", 8 + size) + b"mdat" + _noise(rng, size)
    return ftyp + meta + mdat


# ------------------------------------------------------------------ OLE2 (old Office)
def make_ole(seed: int, kind: str = "doc", size: int = 40000) -> bytes:
    """A compound file like Word 97-2003 writes: allocation table, folder,
    and streams (all at least 4 KB, so no small-stream table is needed)."""
    rng = _rng("ole", seed)
    ss = 512
    names = {"doc": ["WordDocument", "1Table", "\x05SummaryInformation"],
             "xls": ["Workbook", "\x05SummaryInformation", "\x05DocumentSummaryInformation"],
             "ppt": ["PowerPoint Document", "Current User", "\x05SummaryInformation"]}[kind]
    streams = [_noise(rng, size)] + [_noise(rng, 4096 + rng.randint(0, 6000)) for _ in names[1:]]
    data_secs = [(len(s) + ss - 1) // ss for s in streams]
    ndir = 1
    nfat = 1
    while (sum(data_secs) + ndir + nfat) > nfat * (ss // 4):
        nfat += 1
    fat = [0xFFFFFFFD] * nfat + [0xFFFFFFFE] * ndir  # allocation sectors, then the folder (one sector)
    starts = []
    for n in data_secs:
        first = len(fat)
        starts.append(first)
        fat += list(range(first + 1, first + n)) + [0xFFFFFFFE]
    fat += [0xFFFFFFFF] * (nfat * (ss // 4) - len(fat))

    def entry(name, typ, left, right, child, start, sz):
        raw = (name.encode("utf-16-le") + b"\0\0") if name else b""
        e = raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw), typ, 1) + struct.pack("<III", left, right, child)
        e += b"\0" * 16 + b"\0" * 4 + b"\0" * 16 + struct.pack("<IQ", start, sz)
        return e

    F_ = 0xFFFFFFFF
    dirs = [entry("Root Entry", 5, F_, F_, 1, 0xFFFFFFFE, 0)]
    for i, (nm, st, s) in enumerate(zip(names, starts, streams)):
        dirs.append(entry(nm, 2, F_, i + 2 if i + 2 <= len(names) else F_, F_, st, len(s)))
    folder = b"".join(dirs).ljust(ss, b"\0")
    hdr = OLE_SIGNATURE + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
    hdr += struct.pack("<IIIIIIIII", 0, nfat, nfat, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    hdr += struct.pack("<109I", *(list(range(nfat)) + [0xFFFFFFFF] * (109 - nfat)))
    body = struct.pack("<%dI" % len(fat), *fat) + folder
    body += b"".join(s.ljust(n * ss, b"\0") for s, n in zip(streams, data_secs))
    return hdr + body


OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ------------------------------------------------------------------ MPEG program stream
def _scr(scr: int) -> bytes:
    return bytes([0x40 | ((scr >> 27) & 0x38) | 0x04 | ((scr >> 28) & 0x03), (scr >> 20) & 0xFF,
                  ((scr >> 12) & 0xF8) | 0x04 | ((scr >> 13) & 0x03), (scr >> 5) & 0xFF,
                  ((scr << 3) & 0xF8) | 0x04, 0x01])


def make_ps(seed: int, packs: int = 400, first_clock: int = 0, end_code: bool = True) -> bytes:
    """An MPEG-2 program stream as DVD camcorders write: 2048-byte packs, each
    a pack header and one video packet; a system header in the first."""
    rng = _rng("ps", seed)
    mux = struct.pack(">I", (25200 << 2) | 3)[1:]
    out, clock = [], first_clock
    for i in range(packs):
        pk = b"\x00\x00\x01\xba" + _scr(clock) + mux + b"\xf8"
        if i == 0:
            sh = b"\x80\x01\x01\x04\xe1\xff" + b"\xe0\xe0\xe8"
            pk += b"\x00\x00\x01\xbb" + struct.pack(">H", len(sh)) + sh
        n = 2048 - len(pk) - 6
        pes = b"\x81\x80\x05" + bytes([0x21 | ((clock >> 29) & 0x0E), 0, 1, 0, 1]) + _noise(rng, n - 8)
        pk += b"\x00\x00\x01\xe0" + struct.pack(">H", n) + pes
        out.append(pk)
        clock += 300 + rng.randint(0, 40)
    return b"".join(out) + (b"\x00\x00\x01\xb9" if end_code else b"")


# ------------------------------------------------------------------ RAR
def make_rar(seed: int, files: int = 3, version: int = 4) -> bytes:
    """A RAR archive of stored (uncompressed) files."""
    rng = _rng("rar", seed)
    items = [("photo%02d.txt" % i, ("note %d from seed %d\n" % (i, seed)).encode() * rng.randint(200, 900))
             for i in range(files)]
    if version == 4:
        def block(typ, flags, body):
            hdr = struct.pack("<BHH", typ, flags, 7 + len(body)) + body
            return struct.pack("<H", zlib.crc32(hdr) & 0xFFFF) + hdr
        out = [b"Rar!\x1a\x07\x00", block(0x73, 0, b"\0" * 6)]
        for name, data in items:
            nm = name.encode()
            body = struct.pack("<IIBIIBBHI", len(data), len(data), 2, zlib.crc32(data) & 0xFFFFFFFF, 0x5AE10000,
                               20, 0x30, len(nm), 0x20) + nm
            out.append(block(0x74, 0x8000, body) + data)
        out.append(block(0x7B, 0x4000, b""))
        return b"".join(out)

    def vint(v):
        b = bytearray()
        while True:
            c = v & 0x7F
            v >>= 7
            b.append(c | (0x80 if v else 0))
            if not v:
                return bytes(b)

    def block5(typ, flags, body, data=b""):
        h = vint(typ) + vint(flags) + (vint(len(data)) if flags & 2 else b"") + body
        h = vint(len(h)) + h
        return struct.pack("<I", zlib.crc32(h) & 0xFFFFFFFF) + h + data
    out = [b"Rar!\x1a\x07\x01\x00", block5(1, 0, vint(0))]
    for name, data in items:
        nm = name.encode()
        body = vint(0x04) + vint(len(data)) + vint(0x20) + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) \
            + vint(0) + vint(0) + vint(len(nm)) + nm
        out.append(block5(2, 2, body, data))
    out.append(block5(5, 0, vint(0)))
    return b"".join(out)


# ------------------------------------------------------------------ 7z
def _num7(v: int) -> bytes:
    for n in range(8):
        if v < (1 << (7 * (n + 1))):
            first = ((0xFF << (8 - n)) & 0xFF) | (v >> (8 * n))
            return bytes([first]) + (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
    return b"\xff" + v.to_bytes(8, "little")


def make_7z(seed: int, size: int = 30000) -> bytes:
    """A 7z archive holding one file with the Copy method."""
    rng = _rng("7z", seed)
    data = ("backup line %d\n" % seed).encode() * (size // 16) + _noise(rng, 100)
    name = ("backup-%d.txt" % seed).encode("utf-16-le") + b"\0\0"
    hdr = bytes([0x01, 0x04, 0x06]) + _num7(0) + _num7(1) + b"\x09" + _num7(len(data)) + b"\x00"
    hdr += bytes([0x07, 0x0B]) + _num7(1) + b"\x00" + _num7(1) + b"\x01\x00" + b"\x0c" + _num7(len(data)) + b"\x00"
    hdr += bytes([0x08, 0x0A, 0x01]) + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) + b"\x00\x00"
    hdr += bytes([0x05]) + _num7(1) + b"\x11" + _num7(len(name) + 1) + b"\x00" + name + b"\x00\x00"
    start = struct.pack("<QQI", len(data), len(hdr), zlib.crc32(hdr) & 0xFFFFFFFF)
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF) + start + data + hdr


# ------------------------------------------------------------------ Photoshop
def make_psd(seed: int, width: int = 160, height: int = 120) -> bytes:
    rng = _rng("psd", seed)
    res = b"8BIM" + struct.pack(">H", 0x03ED) + b"\0\0" + struct.pack(">I", 16) + struct.pack(">IHHIHH", 72 << 16,
                                                                                            1, 1, 72 << 16, 1, 1)
    hdr = b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, height, width, 8, 3)
    return hdr + struct.pack(">I", 0) + struct.pack(">I", len(res)) + res + struct.pack(">I", 0) \
        + struct.pack(">H", 0) + _noise(rng, 3 * width * height)


# ------------------------------------------------------------------ AMR
def make_amr(seed: int, seconds: float = 20.0) -> bytes:
    """A phone voice memo: AMR-NB at 12.2 kbit/s, 50 frames a second."""
    rng = _rng("amr", seed)
    return b"#!AMR\n" + b"".join(bytes([(7 << 3) | 0x04]) + _noise(rng, 31) for _ in range(int(seconds * 50)))


# ------------------------------------------------------------------ PDF
_WORDS = ("the report shows that our plan for next year needs more time and money than we "
          "thought so we will meet again on monday to agree on what comes first").split()


def make_pdf(seed: int, pages: int = 4, image: bool = True, xref_stream: bool = False,
             raw_streams: bool = False) -> bytes:
    """A PDF as a word processor writes it: zlib-compressed page contents, an
    embedded font, a compressed picture, a cross-reference table (or, as
    newer writers do, a compressed cross-reference stream). `raw_streams`
    stores the page contents uncompressed."""
    rng = _rng("pdf", seed)

    def stream(meta: bytes, raw: bytes, compress: bool = True) -> bytes:
        z = zlib.compress(raw, 6) if compress else raw
        flt = b" /Filter /FlateDecode" if compress else b""
        return b"<< " + meta + flt + b" /Length %d >>\nstream\n" % len(z) + z + b"\nendstream"

    objs = {}
    page_ids = [7 + 2 * i for i in range(pages)]
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % p for p in page_ids), pages)
    objs[3] = b"<< /Type /Font /Subtype /TrueType /BaseFont /Arial /FontDescriptor 4 0 R >>"
    objs[4] = b"<< /Type /FontDescriptor /FontName /Arial /Flags 32 /FontFile2 5 0 R >>"
    font = _noise(rng, rng.randint(20000, 60000))
    objs[5] = stream(b"/Length1 %d" % len(font), font)
    w, h = 64 * rng.randint(2, 6), 48 * rng.randint(2, 6)
    pix = bytes((x * 255 // w + rng.randint(0, 40)) & 255 for _ in range(h) for x in range(w * 3))
    objs[6] = stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                     b"/BitsPerComponent 8" % (w, h), pix)
    for i, p in enumerate(page_ids):
        lines = []
        for k in range(rng.randint(20, 45)):
            text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(6, 14)))
            lines.append(b"BT /F1 11 Tf 72 %d Td (%s) Tj ET" % (740 - 15 * k, text.encode()))
        if image and i == 0:
            lines.append(b"q 200 0 0 150 72 60 cm /Im1 Do Q")
        objs[p] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> "
                   b"/XObject << /Im1 6 0 R >> >> /Contents %d 0 R >>" % (p + 1))
        objs[p + 1] = stream(b"", b"\n".join(lines), not raw_streams)
    n = max(objs) + 1
    objs[n] = b"<< /Producer (Writer %d) /CreationDate (D:2026010%d120000) >>" % (seed, seed % 9 + 1)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for k in sorted(objs):
        offs[k] = len(out)
        out += b"%d 0 obj\n" % k + objs[k] + b"\nendobj\n"
    xref = len(out)
    if xref_stream:
        # one row per object: type, offset, generation; each row stored as its
        # difference from the row above (the PNG 'up' predictor), then zlib
        rows = [bytes([0]) + bytes(4) + b"\xff\xff"] + [bytes([1]) + struct.pack(">I", offs[k]) + bytes(2)
                                                     for k in range(1, n + 1)] + [bytes([1]) + struct.pack(">I", xref) + bytes(2)]
        prev, enc = bytes(7), b""
        for r in rows:
            enc += b"\x02" + bytes((a - b) & 255 for a, b in zip(r, prev))
            prev = r
        z = zlib.compress(enc)
        out += (b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Root 1 0 R /Info %d 0 R /Filter /FlateDecode "
                b"/DecodeParms << /Columns 7 /Predictor 12 >> /Length %d >>\nstream\n" % (n + 1, n + 2, n, len(z))
                + z + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref)
        return bytes(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (n + 1)
    for k in range(1, n + 1):
        out += b"%010d 00000 n \n" % offs[k]
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n + 1, n, xref)
    return bytes(out)


# ------------------------------------------------------------------ Word
def make_docx(seed: int, paragraphs: int = 60) -> bytes:
    import io
    import zipfile
    rng = _rng("docx", seed)
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % " ".join(rng.choice(_WORDS) for _ in range(rng.randint(8, 30)))
                   for _ in range(paragraphs))
    members = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                               'content-types"><Override PartName="/word/document.xml" ContentType="application/'
                               'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships><Relationship Id="rId1" Target="word/document.xml"/>'
                       '</Relationships>',
        "word/document.xml": '<?xml version="1.0"?><w:document><w:body>%s</w:body></w:document>' % body,
        "docProps/core.xml": '<?xml version="1.0"?><cp:coreProperties><dc:creator>user %d</dc:creator>'
                             '</cp:coreProperties>' % seed,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            zi = zipfile.ZipInfo(name, (2026, 1, 1 + seed % 27, 12, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, text)
        zi = zipfile.ZipInfo("word/media/image1.jpeg", (2026, 1, 1, 12, 0, 0))
        z.writestr(zi, _noise(rng, rng.randint(5000, 80000)))  # a picture: stored, like Word does
    return buf.getvalue()


# ------------------------------------------------------------------ WAV
def make_wav(seed: int, seconds: float = 10.0, rate: int = 16000, channels: int = 1) -> bytes:
    """A voice recorder's PCM recording (16-bit)."""
    rng = _rng("wav", seed)
    n = int(seconds * rate) * channels
    data = bytearray(_noise(rng, 2 * n))
    for i in range(1, len(data), 2):
        data[i] = (data[i] >> 3) - 16 & 255 if data[i] & 1 else data[i] >> 4  # quiet: small sample values
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16)
    info = b"INFO" + b"ISFT" + struct.pack("<I", 8) + b"Rec %03d" % (seed % 1000) + b"\0"
    info += bytes(len(info) & 1)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"LIST" + struct.pack("<I", len(info)) + info \
        + b"data" + struct.pack("<I", len(data)) + bytes(data) + bytes(len(data) & 1)
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ------------------------------------------------------------------ AVI
def make_avi(seed: int, frames: int = 30, width: int = 320, height: int = 240, audio: int = 0) -> bytes:
    """A Motion-JPEG AVI, as trail cameras and older digital cameras write:
    every frame is a JPEG in a '00dc' chunk, with an idx1 index at the end.
    `audio`: bytes of sound (a '01wb' chunk) after each frame."""
    from .media import make_photo
    rng = _rng("avi", seed)
    jpegs = [make_photo(seed * 1000 + i, width=width, height=height, thumbnail=False, noise=4.0,
                        quality=rng.choice([70, 80, 85])) for i in range(frames)]
    movi = bytearray(b"movi")
    idx = bytearray()
    for j in jpegs:
        idx += b"00dc" + struct.pack("<III", 0x10, len(movi), len(j))
        movi += b"00dc" + struct.pack("<I", len(j)) + j + bytes(len(j) & 1)
        if audio:
            idx += b"01wb" + struct.pack("<III", 0, len(movi), audio)
            movi += b"01wb" + struct.pack("<I", audio) + _noise(rng, audio) + bytes(audio & 1)
    big = max(len(j) for j in jpegs)
    avih = struct.pack("<IIIIIIIIII4I", 66666, big * 15, 0, 0x10, frames, 0, 1, big, width, height, 0, 0, 0, 0)
    strh = b"vidsMJPG" + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, 1, 15, 0, frames, big, 10000, 0,
                                     0, 0, width, height)
    strf = struct.pack("<IiiHH4sIiiII", 40, width, height, 1, 24, b"MJPG", width * height * 3, 0, 0, 0, 0)
    strl = b"strl" + b"strh" + struct.pack("<I", len(strh)) + strh + b"strf" + struct.pack("<I", len(strf)) + strf
    hdrl = b"hdrl" + b"avih" + struct.pack("<I", len(avih)) + avih + b"LIST" + struct.pack("<I", len(strl)) + strl
    body = b"AVI " + b"LIST" + struct.pack("<I", len(hdrl)) + hdrl + b"LIST" + struct.pack("<I", len(movi)) \
        + bytes(movi) + b"idx1" + struct.pack("<I", len(idx)) + bytes(idx)
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ------------------------------------------------------------------ RAW
def make_raw(seed: int, width: int = 1200, height: int = 800, raw_size: int = 1_500_000) -> bytes:
    """A Canon-style CR2: a TIFF holding a full-size JPEG preview, a small
    thumbnail and the sensor data (losslessly compressed: here noise after a
    lossless-JPEG header)."""
    from .media import make_photo
    rng = _rng("raw", seed)
    preview = make_photo(seed, width=width, height=height, thumbnail=False)
    thumb = make_photo(seed, width=160, height=120, thumbnail=False, quality=75)
    sensor = (b"\xff\xd8\xff\xc4\x00\x14\x00" + bytes(16) + b"\x00\xff\xc3\x00\x0e\x0e" + b"\x00" * 12
              + bytes(b if b != 0xFF else 0xFE for b in _noise(rng, raw_size)) + b"\xff\xd9")
    make, model = b"Canon\0", b"Canon EOS 90D\0"
    when = b"2026:01:%02d 12:00:00\0" % (seed % 28 + 1)

    def ifd(entries, at, nxt):
        """entries: (tag, type, count, value bytes or int); returns (ifd bytes, data bytes)."""
        head = struct.pack("<H", len(entries))
        data = b""
        dpos = at + 2 + 12 * len(entries) + 4
        for tag, typ, count, val in entries:
            if isinstance(val, bytes) and len(val) > 4:
                head += struct.pack("<HHII", tag, typ, count, dpos + len(data))
                data += val + bytes(len(val) & 1)
            elif isinstance(val, bytes):
                head += struct.pack("<HHI", tag, typ, count) + val.ljust(4, b"\0")
            else:
                head += struct.pack("<HHII", tag, typ, count, val)
        return head + struct.pack("<I", nxt) + data

    # layout: header 16 | IFD0 | EXIF IFD | IFD1 | IFD3 | thumbnail | preview | sensor data
    def build(o):
        ifd0_at = 16
        ifd0 = ifd([(0x100, 3, 1, width), (0x101, 3, 1, height), (0x103, 3, 1, 6), (0x10F, 2, len(make), make),
                    (0x110, 2, len(model), model), (0x111, 4, 1, o["preview"]), (0x112, 3, 1, 1),
                    (0x117, 4, 1, len(preview)), (0x132, 2, len(when), when), (0x8769, 4, 1, o["exif"])],
                   ifd0_at, o["ifd1"])
        exif = ifd([(0x829A, 5, 1, struct.pack("<II", 1, 250)), (0x9003, 2, len(when), when)], o["exif"], 0)
        ifd1 = ifd([(0x103, 3, 1, 6), (0x201, 4, 1, o["thumb"]), (0x202, 4, 1, len(thumb))], o["ifd1"], o["ifd3"])
        ifd3 = ifd([(0x103, 3, 1, 6), (0x111, 4, 1, o["sensor"]), (0x117, 4, 1, len(sensor))], o["ifd3"], 0)
        return [ifd0, exif, ifd1, ifd3]

    o = dict(preview=0, exif=0, ifd1=0, ifd3=0, thumb=0, sensor=0)
    for _ in range(2):  # sizes don't depend on the offsets, so two passes settle them
        parts = build(o)
        pos = 16
        for name, p in zip(("ifd0", "exif", "ifd1", "ifd3"), parts):
            o[name] = pos
            pos += len(p) + (len(p) & 1)
        o["thumb"] = pos
        o["preview"] = pos + len(thumb) + (len(thumb) & 1)
        o["sensor"] = o["preview"] + len(preview) + (len(preview) & 1)
    parts = build(o)
    out = b"II*\x00" + struct.pack("<I", 16) + b"CR\x02\x00" + struct.pack("<I", o["ifd3"])
    for p in parts:
        out += p + bytes(len(p) & 1)
    out += thumb + bytes(len(thumb) & 1) + preview + bytes(len(preview) & 1) + sensor
    return out


# ------------------------------------------------------------------ GIF
def make_gif(seed: int, width: int = 320, height: int = 240, frames: int = 1) -> bytes:
    import io
    from PIL import Image, ImageDraw
    rng = _rng("gif", seed)
    ims = []
    for f in range(frames):
        im = Image.new("RGB", (width, height), tuple(rng.randint(0, 255) for _ in range(3)))
        d = ImageDraw.Draw(im)
        for _ in range(12):
            x0, y0 = rng.randint(0, width), rng.randint(0, height)
            d.ellipse((x0, y0, x0 + rng.randint(10, 120), y0 + rng.randint(10, 90)),
                      fill=tuple(rng.randint(0, 255) for _ in range(3)))
        ims.append(im.convert("P", palette=Image.ADAPTIVE, colors=64))
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=frames > 1, append_images=ims[1:], duration=120, loop=0)
    return buf.getvalue()
