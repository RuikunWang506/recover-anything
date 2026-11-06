"""The signature scan skips blank space (free space reads back as all zeros or
all 0xFF) and still finds every header, including ones right next to it."""
import gzip
import time

import pytest

from recover_anything import carve
from recover_anything.simcard import files as F

KB = 1024


class Mem:
    def __init__(self, data: bytes):
        self.data, self.size = data, len(data)

    def read_uncached(self, off, n):
        return self.data[off:off + n]

    read = read_uncached


def _scan(data, chunk=16 << 20):
    return [(o, k) for o, k, _ in carve.find_headers(Mem(data), 0, len(data), 512, 0, chunk=chunk)]


@pytest.mark.parametrize("fill", [b"\x00", b"\xff"], ids=["zeros", "ff"])
def test_blank_space_scans_fast(fill):
    size = 64 << 20
    data = bytearray(fill * size)
    ps = F.make_ps(1, packs=8)
    at = size - 64 * KB
    data[at:at + len(ps)] = ps
    t = time.perf_counter()
    got = _scan(bytes(data))
    took = time.perf_counter() - t
    assert (at, "ps") in got  # a camcorder recording that starts with zero bytes
    # Before: every blank byte was tried against every header, a few seconds per
    # 64 MB (hours on a big card). Now blank blocks are skipped whole.
    assert took < 1.0, "64 MB of blank space took %.1f s to scan" % took


def test_every_header_is_found_next_to_blank_space():
    filler = b"\x10\x20\x30\x40" * (16 * KB)  # no header can start in this
    parts = [
        bytes(128 * KB),                         # blank (zeros)
        F.make_ps(1, packs=40),                  # starts with zero bytes, right after zeros
        filler[:3 * 512],
        b"\xff" * (128 * KB),                    # blank (0xFF)
        F.make_ts(1, packets=600),               # right after 0xFF
        bytes(64 * KB),
        F.make_ts(2, packets=600, m2ts=True),    # its first 4 bytes are a time stamp
        filler[:512],
        F.make_ole(1),
        bytes(200 * KB),
        gzip.compress(b"hello, card\n" * 3000),
    ]
    data = bytearray()
    for p in parts:
        data += p
        data += bytes(-len(data) % 512)  # every file starts at a sector
    mp3_at = (1 << 20) - 512                     # straddles the scan's chunk boundary
    mp3 = F.make_mp3(1, seconds=3.0)
    if len(data) < mp3_at:
        data += bytes(mp3_at - len(data))
    data[mp3_at:mp3_at + len(mp3)] = mp3
    data += bytes(-len(data) % 512) + bytes(70 * KB)
    data = bytes(data)

    # what a byte-by-byte look at every sector start finds
    want = [(c, k) for c in range(0, len(data), 512) for k in [carve.identify(data[c:c + carve.HEAD])] if k]
    kinds = {k for _, k in want}
    assert {"ps", "ts", "ole", "gzip", "mp3"} <= kinds
    assert _scan(data, chunk=1 << 20) == want
    assert _scan(data) == want
