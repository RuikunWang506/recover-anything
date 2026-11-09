"""More file types: audio (MP3/MP2, FLAC, Ogg), video streams (MPEG-TS from
dashcams and camcorders, Matroska/WebM), databases (SQLite: phone messages,
browser history, app data), gzip archives and BMP pictures.

Same contract as others.py: probe(head) looks at the first bytes,
measure(read, start, limit) returns a Result with the exact length, and
validate(read, length, sized) returns (status, note, check). `sized` is True
when the length came from outside the file (a folder entry), which is what
proves a stream format without an end marker is whole.
"""
from __future__ import annotations

import os
import struct
import tempfile
import zlib

from .others import Result

STEP = 1 << 20
NO_START = "it may be the second half of a longer recording: nothing marks this as its beginning"
NOT_PROVEN = "it plays to the end of what was found, but this file type doesn't mark its end"
WEAK = ("its structure checks out, but this file type has no checksums, so it can't prove all of its "
        "contents are its own")


def same_cluster(grid, a: int, b: int) -> bool:
    """True if file offsets a and b (a <= b) are in the same cluster on the
    card. `grid` is (phase, cluster size): the file's first byte sits `phase`
    bytes into a cluster. A file only continues somewhere else at a cluster
    boundary, so bytes in the same cluster as a checked header are the file's own."""
    if not grid:
        return False
    ph, cs = grid
    return cs > 0 and (ph + a) // cs == (ph + b) // cs


OTHER_FILE = "its space now holds a different file (the data goes on past the size its folder entry gives)"


def _runs_on(read, length: int, after: bytes, grid, need: int, continues) -> bool:
    """A folder entry says the file ends at `length`, mid-cluster. If the
    recording carries on past that point inside the same cluster (the next
    frame or packet starts right there and fits), the data isn't the file the
    entry describes: a newer, longer file of the same kind took its place.
    Only the next unit's first bytes are needed, so a nearly full cluster counts."""
    if not grid or len(after) < need:
        return False
    ph, cs = grid
    room = (cs - (ph + length) % cs) % cs
    return room >= need and continues(read, length, after[:room])


def _mp3_continues(read, length: int, tail: bytes) -> bool:
    f, first = mp3_frame(tail[:4]), _mp3_first(read)
    return f is not None and first is not None and f[1] == first[1]


def _ts_continues(read, length: int, tail: bytes) -> bool:
    lay = _ts_layout(read(0, 1024))
    if not lay:
        return False
    stride, off = lay
    if len(tail) < off + 4 or tail[off] != 0x47 or tail[off + 1] & 0x80:
        return False
    if off == 4:  # .MTS: the arrival time carries on from the last packet
        last = int.from_bytes(read(length - stride, 4), "big") & 0x3FFFFFFF
        if (int.from_bytes(tail[:4], "big") - last) & 0x3FFFFFFF > MAX_ATS_STEP:
            return False
    pid, c = ((tail[off + 1] & 0x1F) << 8) | tail[off + 2], tail[off + 3] & 15
    if not (tail[off + 3] >> 4) & 1:
        return off == 4
    back = read(max(0, length - 64 * stride), min(length, 64 * stride))
    for q in range(len(back) - stride, -1, -stride):  # the same stream's last packet before the end
        h = back[q + off:q + off + 4]
        if len(h) == 4 and h[0] == 0x47 and ((h[1] & 0x1F) << 8 | h[2]) == pid and (h[3] >> 4) & 1:
            return c in ((h[3] + 1) & 15, h[3] & 15)  # its counter carries on
    return off == 4


def _sized_verdict(m: Result, length: int, sized: bool, ok_note: str, check: str, ends_here: bool = False,
                   tail_ok: bool = True):
    """For formats checked unit by unit (frames, packets, pages). `tail_ok`
    says the unchecked body of the last frame is certainly the file's own."""
    if m.end <= 0:
        return "corrupt", m.reason or "not a valid file", "structure"
    if m.end < length:
        if sized:
            return "partial", "damaged after %d%% (%s)" % (100 * m.end // max(length, 1), m.reason), "structure"
        return "partial", m.reason, "structure"
    if (sized or m.complete) and not tail_ok:
        return "unverified", ("every frame checked, but the last one runs into a part of the card that may "
                              "belong to another file"), "structure"
    if sized or m.complete:
        return "ok", ok_note, check
    if ends_here:
        return "ok", ok_note + "; it ends inside a cluster, so nothing is missing after it", check
    return "unverified", NOT_PROVEN, "structure"


# ------------------------------------------------------------------ CRCs
_REV = bytes(int("{:08b}".format(i)[::-1], 2) for i in range(256))


def _rev32(x: int) -> int:
    return int("{:032b}".format(x & 0xFFFFFFFF)[::-1], 2)


def crc32_msb(data: bytes, crc: int = 0) -> int:
    """CRC-32 with polynomial 0x04C11DB7, not reflected, no final xor (Ogg
    uses init 0, MPEG tables init 0xFFFFFFFF). zlib computes the reflected
    version of the same polynomial in C, so feed it bit-reversed bytes."""
    r = zlib.crc32(data.translate(_REV), _rev32(crc) ^ 0xFFFFFFFF) ^ 0xFFFFFFFF
    return _rev32(r)


def _table(poly: int, width: int):
    top, mask, out = 1 << (width - 1), (1 << width) - 1, []
    for i in range(256):
        c = i << (width - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) if c & top else (c << 1)
        out.append(c & mask)
    return out


_CRC8 = _table(0x07, 8)
_CRC16 = _table(0x8005, 16)


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


# ------------------------------------------------------------------- MP3 / MP2
_BR = {(1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
       (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
       (0, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}
_BR[(0, 2)] = _BR[(0, 3)]
_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
MP3_MIN_FRAMES = 16  # without a tag, this many frames in a row before believing it


def mp3_frame(h: bytes):
    """(length, stream key) of the MPEG audio frame header at h[0:4], or None.
    Layers II and III only (layer I is extinct in practice)."""
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    ver, layer = (h[1] >> 3) & 3, (h[1] >> 1) & 3
    if ver == 1 or layer not in (1, 2):
        return None
    bri, sri, pad = h[2] >> 4, (h[2] >> 2) & 3, (h[2] >> 1) & 1
    if bri in (0, 15) or sri == 3 or (h[3] & 3) == 2:
        return None
    v1 = ver == 3
    lay = 3 if layer == 1 else 2
    br = _BR[(1 if v1 else 0, lay)][bri] * 1000
    sr = _SR[ver][sri]
    n = (144 if (v1 or lay == 2) else 72) * br // sr + pad
    return n, (ver, layer, sri)


def mp3_side(h: bytes, n: int):
    """(main_data_begin, bits of main data, room for main data in this frame)
    of a Layer III frame of n bytes whose first bytes are h (header, CRC, side
    information), or None for another layer or too few bytes."""
    if len(h) < 4 or (h[1] >> 1) & 3 != 1:
        return None
    v1, mono = (h[1] >> 3) & 3 == 3, h[3] >> 6 == 3
    crc = 0 if h[1] & 1 else 2
    side = (17 if mono else 32) if v1 else (9 if mono else 17)
    si = h[4 + crc:4 + crc + side]
    if len(si) < side:
        return None
    val, nb = int.from_bytes(si, "big"), side * 8
    nch = 1 if mono else 2
    if v1:
        mdb, pos, count, step = val >> (nb - 9), 9 + (5 if mono else 3) + 4 * nch, 2 * nch, 59
    else:
        mdb, pos, count, step = val >> (nb - 8), 8 + (1 if mono else 2), nch, 63
    bits = 0
    for _ in range(count):
        bits += (val >> (nb - pos - 12)) & 0xFFF
        pos += step
    return mdb, bits, n - 4 - crc - side


class Reservoir:
    """Layer III's bit reservoir ties each frame to the one before: its main
    data begins main_data_begin bytes back, in room the frames before left
    unused, so it can't point further back than that room (a frame of another
    recording often does). An exact encoder points at exactly that room: then
    the frame is tied to the one before. An encoder may also leave a gap
    (bytes it drains when the reservoir is full): legal, but no proof."""

    def __init__(self):
        self.room = None

    def step(self, side) -> str:
        """'tie', 'gap' or 'bad' for the next frame's (main_data_begin, bits,
        room); 'start' for the first frame seen."""
        mdb, bits, area = side
        verdict = "start" if self.room is None else "bad" if mdb > self.room else "tie" if mdb == self.room else "gap"
        used = (bits + 7) // 8
        if used > mdb + area:
            verdict = "bad"  # its main data can't fit where it says
        if verdict != "bad":
            self.room = mdb + area - used
        return verdict


def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def probe_mp3(head: bytes) -> bool:
    if head[:3] == b"ID3":
        return len(head) >= 10 and 2 <= head[3] <= 4 and head[4] == 0 and all(x < 0x80 for x in head[6:10])
    return mp3_frame(head[:4]) is not None


class _Win:
    """Buffered reads for walks that take many small steps."""

    def __init__(self, read, start: int):
        self.read, self.start, self.base, self.buf = read, start, 0, b""

    def get(self, pos: int, n: int) -> bytes:
        if pos < self.base or pos + n > self.base + len(self.buf):
            self.base = pos
            self.buf = self.read(self.start + pos, max(n, STEP))
        return self.buf[pos - self.base:pos - self.base + n]


def measure_mp3(read, start: int, limit: int) -> Result:
    w = _Win(read, start)
    head = w.get(0, 10)
    pos, tagged = 0, False
    if head[:3] == b"ID3" and len(head) >= 10:
        pos = 10 + _synchsafe(head[6:10]) + (10 if head[5] & 0x10 else 0)
        tagged = True
    first_at, key, frames, declared, last = pos, None, 0, None, None
    ext = ".mp3"
    reason = "ok"
    res = Reservoir()
    while pos < limit:
        if declared is not None and pos - first_at == declared:
            break  # where its own header says the audio ends
        f = mp3_frame(w.get(pos, 4))
        if f is None or (key is not None and f[1] != key):
            reason = "frames stop here"
            break
        n, k = f
        if pos + n > limit:
            reason = "last frame cut short"
            break
        side = mp3_side(w.get(pos, 40), n)
        if side is not None and res.step(side) == "bad":
            reason = "a frame of another recording follows here"  # its data can't be where it points
            break
        if key is None:
            key = k
            ext = ".mp3" if k[1] == 1 else ".mp2"
            body = w.get(pos, min(n, 200))
            for tag in (b"Xing", b"Info"):
                i = body.find(tag)
                if 0 < i < 64 and len(body) >= i + 12:
                    flags = struct.unpack(">I", body[i + 4:i + 8])[0]
                    if flags & 2:  # byte count present
                        at = i + 8 + (4 if flags & 1 else 0)
                        if len(body) >= at + 4:
                            declared = struct.unpack(">I", body[at:at + 4])[0]
                    break
        last = pos
        pos += n
        frames += 1
    if frames < (1 if tagged else MP3_MIN_FRAMES):
        return Result(0, False, ext, "not enough audio frames")
    audio_end = pos
    end = pos
    proven = declared is not None and declared == audio_end - first_at
    tail = w.get(end, 32)
    if tail[:8] == b"APETAGEX" and len(tail) >= 32:
        end += struct.unpack("<I", tail[12:16])[0] + 32
        tail = w.get(end, 3)
        proven = True
    if tail[:3] == b"TAG" and end + 128 <= limit:
        end += 128
        proven = True
    return Result(end, proven, ext, reason if not proven else "ok", break_pos=None if proven else audio_end,
                  last=last, kind="start" if (tagged or declared is not None) else None)


def _mp3_first(read):
    h = read(0, 10)
    pos = 10 + _synchsafe(h[6:10]) + (10 if h[5] & 0x10 else 0) if h[:3] == b"ID3" and len(h) >= 10 else 0
    return mp3_frame(read(pos, 4))


def validate_mp3(read, length: int, sized: bool, after: bytes = b"", grid=None, layout_known: bool = False, **_):
    m = measure_mp3(read, 0, length)
    nxt, first = mp3_frame(after[:4]), _mp3_first(read)
    a = m.last if m.last is not None else 0
    audio_end = m.break_pos if m.break_pos is not None else m.end
    ends_here = (m.end == length and after[:3] != b"TAG" and same_cluster(grid, a, length + 3)
                 and (nxt is None or (first is not None and nxt[1] != first[1])))
    tail_ok = layout_known or same_cluster(grid, a, max(a, audio_end - 1))
    if sized and m.end == length and after[:3] != b"TAG" and _runs_on(read, length, after, grid, 4, _mp3_continues):
        return "lost", OTHER_FILE, "structure"
    st = _sized_verdict(m, length, sized, "every frame checked", "structure", ends_here, tail_ok)
    if st[0] == "ok" and not sized and m.kind != "start":
        return "unverified", NO_START, "structure"
    return st


# ------------------------------------------------------------------ FLAC
def probe_flac(head: bytes) -> bool:
    return head[:4] == b"fLaC" and len(head) >= 5 and (head[4] & 0x7F) == 0  # STREAMINFO comes first


def _flac_header(h: bytes):
    """(block size, frame or sample number, header length) or None."""
    if len(h) < 16 or h[0] != 0xFF or h[1] not in (0xF8, 0xF9):
        return None
    bsc, src_, ch, ss = h[2] >> 4, h[2] & 15, h[3] >> 4, (h[3] >> 1) & 7
    if bsc == 0 or src_ == 15 or ch > 10 or ss == 3 or h[3] & 1:
        return None
    b0 = h[4]
    if b0 < 0x80:
        n, v = 1, b0
    else:
        n = 2 if b0 >> 5 == 6 else 3 if b0 >> 4 == 14 else 4 if b0 >> 3 == 30 else 5 if b0 >> 2 == 62 else \
            6 if b0 >> 1 == 126 else 7 if b0 == 0xFE else 0
        if not n:
            return None
        v = b0 & (0x7F >> n) if n < 7 else 0
        for i in range(1, n):
            c = h[4 + i]
            if c >> 6 != 2:
                return None
            v = (v << 6) | (c & 0x3F)
    p = 4 + n
    if bsc == 6:
        bs, p = h[p] + 1, p + 1
    elif bsc == 7:
        bs, p = ((h[p] << 8) | h[p + 1]) + 1, p + 2
    elif bsc == 1:
        bs = 192
    elif bsc <= 5:
        bs = 576 << (bsc - 2)
    else:
        bs = 256 << (bsc - 8)
    if src_ == 12:
        p += 1
    elif src_ in (13, 14):
        p += 2
    if crc8(h[:p]) != h[p]:
        return None
    return bs, v, p + 1


def measure_flac(read, start: int, limit: int) -> Result:
    pos, info = 4, None
    for _ in range(4096):
        h = read(start + pos, 4)
        if len(h) < 4 or (h[0] & 0x7F) == 127:
            return Result(0, False, ".flac", "damaged header")
        n = int.from_bytes(h[1:4], "big")
        if (h[0] & 0x7F) == 0:
            info = read(start + pos + 4, 34)
        pos += 4 + n
        if h[0] & 0x80:
            break
    else:
        return Result(0, False, ".flac", "damaged header")
    if not info or len(info) < 34 or pos > limit:
        return Result(0, False, ".flac", "damaged header")
    min_fs = int.from_bytes(info[4:7], "big")
    max_fs = int.from_bytes(info[7:10], "big") or (16 << 20)
    total = ((info[13] & 15) << 32) | int.from_bytes(info[14:18], "big")
    samples, index = 0, 0
    tab = _CRC16
    while pos < limit:
        h16 = read(start + pos, 16)
        hd = _flac_header(h16)
        var = h16[1:2] == b"\xf9"  # variable block size: frames are numbered by sample
        if hd is None or hd[1] != (samples if var else index):
            return Result(pos, False, ".flac", "frames stop here", break_pos=pos)
        bs, _num, hlen = hd
        last = total and samples + bs >= total
        data = read(start + pos, min(max_fs + 2, limit - pos))
        crc, end = 0, 0
        for i, b in enumerate(data):
            crc = ((crc << 8) & 0xFFFF) ^ tab[(crc >> 8) ^ b]
            if crc == 0 and i + 1 >= max(hlen + 3, min_fs if last else 0):
                if last:
                    end = i + 1
                    break
                nxt = _flac_header(read(start + pos + i + 1, 16))
                if nxt and nxt[1] == (samples + bs if var else index + 1):
                    end = i + 1
                    break
        if not end:
            return Result(pos, False, ".flac", "checksum mismatch in a frame", break_pos=pos)
        pos += end
        samples += bs
        index += 1
        if last:
            return Result(pos, True, ".flac")
    return Result(pos, False, ".flac", "frames stop here", break_pos=pos)


def _checksummed_verdict(m: Result, length: int, ok_note: str):
    if m.complete and m.end == length:
        return "ok", ok_note, "full"
    if m.complete:
        return "partial", "extra data after the end of the file", "full"
    return ("corrupt" if "mismatch" in m.reason or "damaged" in m.reason else "partial"), m.reason, "full"


def validate_flac(read, length: int, sized: bool, **_):
    return _checksummed_verdict(measure_flac(read, 0, length), length, "every frame's checksum verified")


# ------------------------------------------------------------------ Ogg
OGG_EXT = ((b"OpusHead", ".opus"), (b"\x01vorbis", ".ogg"), (b"\x80theora", ".ogv"), (b"\x7fFLAC", ".oga"),
           (b"Speex   ", ".spx"))


def probe_ogg(head: bytes) -> bool:
    return len(head) >= 27 and head[:5] == b"OggS\x00" and head[5] & 2 and head[6:14] == b"\x00" * 8


def measure_ogg(read, start: int, limit: int) -> Result:
    pos, live, ext, began = 0, set(), ".ogg", False
    while pos + 27 <= limit:
        h = read(start + pos, 27 + 255)
        if len(h) < 27 or h[:5] != b"OggS\x00":
            break
        flags, serial, nseg = h[5], h[14:18], h[26]
        plen = 27 + nseg + sum(h[27:27 + nseg])
        if len(h) < 27 + nseg or pos + plen > limit:
            break
        page = read(start + pos, plen)
        if len(page) < plen or crc32_msb(page[:22] + b"\0\0\0\0" + page[26:]) != struct.unpack("<I", page[22:26])[0]:
            return Result(pos, False, ext, "checksum mismatch", break_pos=pos)
        if flags & 2:
            if serial in live:
                break
            if not began:
                body = page[27 + nseg:]
                ext = next((e for sig, e in OGG_EXT if body.startswith(sig)), ".ogg")
            live.add(serial)
        elif serial not in live:
            break
        began = True
        if flags & 4:
            live.discard(serial)
        pos += plen
        if not live:
            nxt = read(start + pos, 6)
            if not (nxt[:5] == b"OggS\x00" and nxt[5] & 2):  # not a chained stream
                return Result(pos, True, ext)
    return Result(pos, False, ext, "the stream doesn't finish", break_pos=pos)


def validate_ogg(read, length: int, sized: bool, **_):
    return _checksummed_verdict(measure_ogg(read, 0, length), length, "every page's checksum verified")


# ------------------------------------------------------------------ MPEG transport stream
TS_MIN_PACKETS = 32
PCR_WRAP = (1 << 33) * 300   # the programme clock, in 27 MHz ticks
MAX_ATS_STEP = 270_000       # 10 ms between two packets of an AVCHD clip is already a very low bit rate


class TsClock:
    """The clocks a recording carries, which another recording's data won't
    continue: the programme clock (PCR), which must run forward at a pace that
    fits the bytes in between, and in a 192-byte-packet .MTS the arrival time
    stamp in front of every packet. The 4-bit packet counter alone matches
    another recording's data one time in 16."""

    def __init__(self):
        self.pcr_pid = None
        self.pcr = None   # (value, packet number)
        self.ats = None
        self.checked = -1  # byte position of the last clock that checked out against an earlier one

    def packet(self, pkt: bytes, off: int, n: int, pos: int) -> bool:
        """pkt: the whole packet (with the 4-byte time stamp if off == 4)."""
        if off == 4:
            ats = int.from_bytes(pkt[:4], "big") & 0x3FFFFFFF
            if self.ats is not None:
                if (ats - self.ats) & 0x3FFFFFFF > MAX_ATS_STEP:
                    return False
                self.checked = pos
            self.ats = ats
        h = pkt[off:off + 12]
        if len(h) >= 12 and h[3] & 0x20 and h[4] >= 7 and h[5] & 0x10:
            pid = ((h[1] & 0x1F) << 8) | h[2]
            if self.pcr_pid is None:
                self.pcr_pid = pid
            if pid == self.pcr_pid:
                v = int.from_bytes(h[6:12], "big")
                pcr = (v >> 15) * 300 + (v & 0x1FF)
                if self.pcr is not None and not h[5] & 0x80:  # not a marked discontinuity
                    d = (pcr - self.pcr[0]) % PCR_WRAP
                    k = n - self.pcr[1]
                    if not (k * 200 <= d <= min(5_400_000, k * 81_216 + 270_000)):
                        return False
                    self.checked = pos
                self.pcr = (pcr, n)
        return True


def _ts_layout(head: bytes):
    for stride, off in ((188, 0), (192, 4)):
        if len(head) >= off + 2 * stride + 4 and all(head[off + k * stride] == 0x47 for k in range(3)):
            return stride, off
    return None


def probe_ts(head: bytes) -> bool:
    lay = _ts_layout(head)
    if not lay:
        return False
    p = lay[1]
    pid = ((head[p + 1] & 0x1F) << 8) | head[p + 2]
    # a recording starts with its tables (PAT, SDT or SIT), not in the middle of the video
    return bool(head[p + 1] & 0x40) and pid in (0x0000, 0x0011, 0x001F) and not head[p + 1] & 0x80


def measure_ts(read, start: int, limit: int) -> Result:
    lay = _ts_layout(read(start, 1024))
    if not lay:
        return Result(0, False, ".ts", "not a transport stream")
    stride, off = lay
    ext = ".ts" if stride == 188 else ".mts"
    cc: dict = {}
    firsts: list = []  # each stream's first counter: a recorder starts every one at 0
    clock = TsClock()
    pos, n, reason = 0, 0, "packets stop here"
    while pos + stride <= limit:
        blk = read(start + pos, min(stride * 4096, (limit - pos) // stride * stride))
        if len(blk) < stride:
            break
        bad = False
        for q in range(0, len(blk) - stride + 1, stride):
            p = blk[q + off:q + off + 4]
            if p[0] != 0x47 or p[1] & 0x80:
                bad = True
                break
            pid = ((p[1] & 0x1F) << 8) | p[2]
            afc, c = (p[3] >> 4) & 3, p[3] & 15
            if afc == 0:
                bad = True
                break
            if pid != 0x1FFF and afc & 1:
                prev = cc.get(pid)
                if prev is None and n < 256:
                    firsts.append(c)
                disc = afc & 2 and blk[q + off + 4] > 0 and blk[q + off + 5] & 0x80
                if prev is not None and not disc and c != (prev + 1) & 15 and c != prev:
                    reason = "a packet is out of sequence (another recording's data?)"
                    bad = True
                    break
                cc[pid] = c
            if not clock.packet(blk[q:q + stride], off, n, pos):
                reason = "the recording's clock jumps (another recording's data?)"
                bad = True
                break
            pos += stride
            n += 1
        if bad:
            break
    if n < TS_MIN_PACKETS:
        return Result(0, False, ext, "too short")
    return Result(pos, False, ext, reason, break_pos=pos, last=pos - stride + off,
                  kind="start" if firsts and not any(firsts) else None)


def validate_ts(read, length: int, sized: bool, after: bytes = b"", grid=None, layout_known: bool = False, **_):
    m = measure_ts(read, 0, length)
    lay = _ts_layout(read(0, 1024))
    off = lay[1] if lay else 0
    a = m.last if m.last is not None else 0
    ends_here = m.end == length and after[off:off + 1] != b"\x47" and same_cluster(grid, a, length + off + 3)
    tail_ok = layout_known or same_cluster(grid, a, max(a, m.end - 1))
    if sized and m.end == length and _runs_on(read, length, after, grid, off + 4, _ts_continues):
        return "lost", OTHER_FILE, "structure"
    st = _sized_verdict(m, length, sized, "every packet checked", "structure", ends_here, tail_ok)
    if st[0] == "ok" and not sized and m.kind != "start":
        return "unverified", NO_START, "structure"
    return st


# ------------------------------------------------------------------ Matroska / WebM
_SEG_CHILDREN = {0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B, 0x1043A770, 0x1254C367, 0x1941A469,
                 0xEC, 0xBF}
_CLUSTER_CHILDREN = {0xE7, 0xA3, 0xA0, 0xA7, 0xAB, 0xBF, 0xEC, 0x5854, 0xAF}


def _vint(b: bytes, p: int, keep_marker: bool):
    if p >= len(b) or b[p] == 0:
        return None
    n = 9 - b[p].bit_length()
    if p + n > len(b):
        return None
    v = int.from_bytes(b[p:p + n], "big")
    if keep_marker:
        return v, n
    v &= (1 << (7 * n)) - 1
    if v == (1 << (7 * n)) - 1:
        return -1, n  # unknown size
    return v, n


def _element(read, at: int):
    h = read(at, 12)
    i = _vint(h, 0, True)
    if not i or i[1] > 4:
        return None
    s = _vint(h, i[1], False)
    if not s:
        return None
    return i[0], s[0], i[1] + s[1]


def probe_mkv(head: bytes) -> bool:
    return head[:4] == b"\x1a\x45\xdf\xa3"


def nal_chain_ok(data: bytes, nsize: int, hevc: bool) -> bool:
    """A frame stored as length-prefixed NAL units must be made of exactly
    those units, each with a valid header. Another file's data almost never is."""
    p, n = 0, len(data)
    while p < n:
        if p + nsize + 1 > n:
            return False
        ln = int.from_bytes(data[p:p + nsize], "big")
        b0 = data[p + nsize]
        if ln == 0 or b0 & 0x80 or p + nsize + ln > n:
            return False
        if hevc:
            if ln < 2 or (b0 >> 1) & 0x3F > 47 or (data[p + nsize + 1] & 7) == 0:
                return False
        elif (b0 & 0x1F) == 0 or (b0 & 0x1F) > 23:
            return False
        p += nsize + ln
    return True


def _mkv_tracks(body: bytes) -> dict:
    """Track number -> (codec id, NAL length size or 0)."""
    out, p = {}, 0
    while p < len(body):
        e = _vint(body, p, True)
        s = e and _vint(body, p + e[1], False)
        if not s or s[0] < 0:
            break
        q, end = p + e[1] + s[1], p + e[1] + s[1] + s[0]
        if e[0] == 0xAE:
            num, codec, priv = 0, b"", b""
            while q < end:
                ce = _vint(body, q, True)
                cs = ce and _vint(body, q + ce[1], False)
                if not cs or cs[0] < 0:
                    break
                v = body[q + ce[1] + cs[1]:q + ce[1] + cs[1] + cs[0]]
                if ce[0] == 0xD7:
                    num = int.from_bytes(v, "big")
                elif ce[0] == 0x86:
                    codec = v
                elif ce[0] == 0x63A2:
                    priv = v
                q += ce[1] + cs[1] + cs[0]
            nsize = 0
            if codec in (b"V_MPEG4/ISO/AVC",):
                nsize = (priv[4] & 3) + 1 if len(priv) > 4 else 4
            elif codec in (b"V_MPEGH/ISO/HEVC",):
                nsize = (priv[21] & 3) + 1 if len(priv) > 21 else 4
            out[num] = (codec, nsize)
        p = end
    return out


def measure_mkv(read, start: int, limit: int, stats: dict | None = None) -> Result:
    """Walks every element down to each frame. With `stats`, frames of
    H.264/H.265 tracks are checked NAL by NAL and counted."""
    rd = lambda o, n: read(start + o, n)  # noqa: E731
    e = _element(rd, 0)
    if not e or e[1] < 0 or e[1] > 4096:
        return Result(0, False, ".mkv", "damaged header")
    ebml = rd(e[2], e[1])
    ext = ".webm" if b"\x42\x82\x84webm" in ebml else ".mkv"
    pos = e[2] + e[1]
    s = _element(rd, pos)
    if not s or s[0] != 0x18538067:
        return Result(0, False, ext, "no segment")
    declared = pos + s[2] + s[1] if s[1] >= 0 else None
    seg_end = min(declared, limit) if declared is not None else limit
    pos += s[2]
    tracks: dict = {}

    def frame(q, size, hl, kind):
        """Check one Block/SimpleBlock; return an error string or None."""
        t = _vint(rd(q + hl, 8), 0, False)
        if not t or (tracks and t[0] not in tracks):
            return "a frame belongs to no track"
        if stats is None:
            return None
        codec, nsize = tracks.get(t[0], (b"", 0))
        flags = rd(q + hl + t[1] + 2, 1)
        if nsize and flags and not flags[0] & 0x06:  # not laced
            body = rd(q + hl + t[1] + 3, size - t[1] - 3)
            if not nal_chain_ok(body, nsize, codec == b"V_MPEGH/ISO/HEVC"):
                return "a frame is damaged (another file's data?)"
            stats["checked"] = stats.get("checked", 0) + 1
        elif codec.startswith(b"V_"):
            stats["unchecked"] = stats.get("unchecked", 0) + 1
        return None

    while pos < seg_end:
        el = _element(rd, pos)
        if not el or el[0] not in _SEG_CHILDREN:
            break
        eid, size, hl = el
        if eid == 0x1F43B675:
            cend = pos + hl + size if size >= 0 else seg_end
            if cend > seg_end:
                break
            q = pos + hl
            while q < cend:
                c = _element(rd, q)
                if not c or c[0] not in _CLUSTER_CHILDREN or c[1] < 0 or q + c[2] + c[1] > cend:
                    if size < 0 and c and c[0] in _SEG_CHILDREN:
                        break  # an unknown-size cluster ends where the next one starts
                    return Result(pos, False, ext, "a cluster is damaged", break_pos=q)
                why = None
                if c[0] == 0xA3:
                    why = frame(q, c[1], c[2], "simple")
                elif c[0] == 0xA0:  # BlockGroup: its Block
                    g, gend = q + c[2], q + c[2] + c[1]
                    while g < gend and not why:
                        b = _element(rd, g)
                        if not b or b[1] < 0 or g + b[2] + b[1] > gend:
                            why = "a frame group is damaged"
                            break
                        if b[0] == 0xA1:
                            why = frame(g, b[1], b[2], "block")
                        g += b[2] + b[1]
                if why:
                    return Result(pos, False, ext, why, break_pos=q)
                q += c[2] + c[1]
            pos = q
            continue
        if size < 0 or pos + hl + size > seg_end:
            break
        if eid == 0x1654AE6B:
            tracks = _mkv_tracks(rd(pos + hl, min(size, 1 << 20)))
        pos += hl + size
    complete = declared is not None and pos == declared  # not just the end of what was read
    return Result(pos, complete, ext, "ok" if complete else "structure stops here", break_pos=None if complete else pos)


def validate_mkv(read, length: int, sized: bool, layout_known: bool = False, **_):
    stats: dict = {}
    m = measure_mkv(read, 0, length, stats)
    if not (m.complete and m.end == length):
        return _sized_verdict(m, length, sized, "structure checked", "structure")
    if stats.get("checked") and not stats.get("unchecked"):
        return "ok", "every frame checked", "full"
    if layout_known:
        return "ok", "structure checked", "structure"
    return "unverified", WEAK, "structure"


# ------------------------------------------------------------------ SQLite
SQLITE_MAX_CHECK = 4 << 30


def probe_sqlite(head: bytes) -> bool:
    return head[:16] == b"SQLite format 3\x00"


def _sqlite_size(h: bytes):
    ps = struct.unpack(">H", h[16:18])[0]
    ps = 65536 if ps == 1 else ps
    if ps < 512 or ps & (ps - 1):
        return None
    count = struct.unpack(">I", h[28:32])[0]
    if count and h[92:96] == h[24:28]:
        return ps * count
    return None


def measure_sqlite(read, start: int, limit: int) -> Result:
    h = read(start, 100)
    if len(h) < 100:
        return Result(0, False, ".sqlite", "truncated")
    size = _sqlite_size(h)
    if size is None:
        return Result(0, False, ".sqlite", "size not recorded (very old database)")
    if size > limit:
        return Result(limit, False, ".sqlite", "cut short", break_pos=limit)
    return Result(size, True, ".sqlite")


def validate_sqlite(read, length: int, sized: bool, **_):
    import sqlite3
    m = measure_sqlite(read, 0, length)
    if not m.complete:
        return "partial", m.reason, "structure"
    if m.end > SQLITE_MAX_CHECK:
        return "unverified", "too large to check", "none"
    fd, tmp = tempfile.mkstemp(suffix=".sqlite", prefix="ra-check-")
    try:
        with os.fdopen(fd, "wb") as f:
            pos = 0
            while pos < m.end:
                piece = read(pos, min(STEP * 16, m.end - pos))
                if not piece:
                    break
                f.write(piece)
                pos += len(piece)
        from urllib.request import pathname2url
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % pathname2url(tmp), uri=True)
        try:
            rows = con.execute("PRAGMA integrity_check(20)").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return "corrupt", "the database is damaged (%s)" % e, "full"
    finally:
        for p in (tmp, tmp + "-wal", tmp + "-shm", tmp + "-journal"):
            try:
                os.unlink(p)
            except OSError:
                pass
    if rows == [("ok",)]:
        return "ok", "every page and index checked", "full"
    return "corrupt", "the database is damaged (%s)" % rows[0][0][:120], "full"


# ------------------------------------------------------------------ gzip
GZIP_BUDGET = 2 << 30


def probe_gzip(head: bytes) -> bool:
    return len(head) >= 10 and head[:3] == b"\x1f\x8b\x08" and not head[3] & 0xE0 and (head[9] <= 13 or head[9] == 255)


def _gzip_member(read, start: int, pos: int, limit: int, budget: list):
    """End of one member, (crc ok), first bytes of its contents."""
    h = read(start + pos, 10)
    flg = h[3]
    p = pos + 10
    if flg & 4:
        p += 2 + struct.unpack("<H", read(start + p, 2))[0]
    for bit in (8, 16):
        if flg & bit:
            for _ in range(64):
                chunk = read(start + p, 4096)
                i = chunk.find(b"\0")
                if i >= 0:
                    p += i + 1
                    break
                p += len(chunk)
                if not chunk:
                    return None
            else:
                return None
    if flg & 2:
        p += 2
    d = zlib.decompressobj(-15)
    crc, size, first = 0, 0, b""
    while not d.eof:
        if p >= limit:
            return None
        data = read(start + p, min(STEP, limit - p))
        if not data:
            return None
        buf = data
        while buf and not d.eof:
            out = d.decompress(buf, STEP)
            crc = zlib.crc32(out, crc)
            size += len(out)
            if len(first) < 512:
                first += out[:512 - len(first)]
            buf = d.unconsumed_tail
            budget[0] -= len(out)
            if budget[0] < 0:
                return None
        # at the end, the bytes after the stream show up as unused_data, or as
        # unconsumed_tail when the output limit was hit on the same call (or both)
        p += len(data) - (max(len(d.unused_data), len(buf)) if d.eof else len(buf))
    tr = read(start + p, 8)
    if len(tr) < 8:
        return None
    ok = struct.unpack("<II", tr) == (crc & 0xFFFFFFFF, size & 0xFFFFFFFF)
    return p + 8, ok, first


def measure_gzip(read, start: int, limit: int) -> Result:
    budget = [GZIP_BUDGET]
    pos, ext = 0, ".gz"
    try:
        while True:
            m = _gzip_member(read, start, pos, limit, budget)
            if m is None:
                return Result(pos, False, ext, "the compressed data is cut short or too large to check", break_pos=pos)
            end, ok, first = m
            if not ok:
                return Result(end, False, ext, "checksum mismatch", break_pos=pos)
            if pos == 0 and first[257:262] == b"ustar":
                ext = ".tar.gz"
            pos = end
            if not probe_gzip(read(start + pos, 10)):
                return Result(pos, True, ext)
    except zlib.error:
        return Result(pos, False, ext, "the compressed data is damaged", break_pos=pos)


def validate_gzip(read, length: int, sized: bool, **_):
    return _checksummed_verdict(measure_gzip(read, 0, length), length, "checksum verified")


# ------------------------------------------------------------------ BMP
def probe_bmp(head: bytes) -> bool:
    if len(head) < 26 or head[:2] != b"BM" or head[6:10] != b"\0\0\0\0":
        return False
    off, dib = struct.unpack("<II", head[10:18])
    return dib in (12, 40, 52, 56, 64, 108, 124) and 14 + dib <= off < (1 << 20)


def _bmp_need(h: bytes):
    off, dib = struct.unpack("<II", h[10:18])
    if dib == 12:
        w, hh, _planes, bpp = struct.unpack("<HhHH", h[18:26])
        comp = 0
    else:
        w, hh, _planes, bpp, comp = struct.unpack("<iiHHI", h[18:34])
    if w <= 0 or hh == 0 or bpp not in (1, 2, 4, 8, 16, 24, 32) or w > 1 << 16 or abs(hh) > 1 << 16:
        return None
    if comp not in (0, 3, 6):
        return 0  # compressed: the header's size field is all there is
    return off + ((bpp * w + 31) // 32) * 4 * abs(hh)


def measure_bmp(read, start: int, limit: int) -> Result:
    h = read(start, 64)
    if len(h) < 34:
        return Result(0, False, ".bmp", "truncated")
    declared = struct.unpack("<I", h[2:6])[0]
    need = _bmp_need(h)
    if need is None:
        return Result(0, False, ".bmp", "damaged header")
    size = declared or need
    if not size or size < need or (need and size > need + 65536):
        return Result(0, False, ".bmp", "sizes don't agree")
    if size > limit:
        return Result(limit, False, ".bmp", "cut short", break_pos=limit)
    return Result(size, True, ".bmp")


def validate_bmp(read, length: int, sized: bool, layout_known: bool = False, **_):
    m = measure_bmp(read, 0, length)
    if m.complete and m.end <= length:
        return ("ok", "structure checked", "structure") if layout_known else ("unverified", WEAK, "structure")
    return "partial", m.reason, "structure"


# ------------------------------------------------------------------ registry
# kind: (probe, measure, validate, max size, group)
KINDS = {
    "flac": (probe_flac, measure_flac, validate_flac, 4 << 30, "audio"),
    "ogg": (probe_ogg, measure_ogg, validate_ogg, 4 << 30, "audio"),
    "mkv": (probe_mkv, measure_mkv, validate_mkv, 1 << 40, "video"),
    "ts": (probe_ts, measure_ts, validate_ts, 1 << 40, "video"),
    "sqlite": (probe_sqlite, measure_sqlite, validate_sqlite, 64 << 30, "document"),
    "gzip": (probe_gzip, measure_gzip, validate_gzip, 1 << 40, "document"),
    "bmp": (probe_bmp, measure_bmp, validate_bmp, 1 << 30, "photo"),
    "mp3": (probe_mp3, measure_mp3, validate_mp3, 1 << 30, "audio"),  # last: its frame-sync probe is the loosest
}
# Formats whose frames or packets repeat through the file: a later "start"
# inside one already measured is just the middle of the same recording.
STREAMS = {"mp3", "ts"}
# Checked unit by unit with nothing in between, so once a file checks out
# every byte of it is accounted for.
DENSE = {"flac", "ogg", "gzip", "mp3", "ts"}

# Old Office documents, program streams, RAR/7z, Photoshop, AMR (formats/extra.py)
from . import extra as _extra  # noqa: E402

KINDS = {**{k: v for k, v in KINDS.items() if k != "mp3"}, **_extra.KINDS, "mp3": KINDS["mp3"]}
STREAMS |= _extra.STREAMS
DENSE |= _extra.DENSE
