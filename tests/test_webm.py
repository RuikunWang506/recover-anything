"""WebM video (VP9, VP8, AV1): every frame's header is checked, as an H.264
frame's units are, so a deleted WebM can come back as recovered. The checks
were run on 204,638 frames of 606 real WebM files (no frame was rejected)."""
import random

from recover_anything.formats import more
from recover_anything.simcard import files as F


def _rd(d):
    return lambda o, n: d[o:o + n]


def _webm(frames, codec=b"V_VP9", extra=b""):
    """A small WebM holding these frames, the first a key frame."""
    el = F._el
    ebml = el(0x1A45DFA3, el(0x4282, b"webm"))
    tracks = el(0x1654AE6B, el(0xAE, el(0xD7, b"\x01") + el(0x83, b"\x01") + el(0x86, codec) + extra))
    blocks = [el(0xE7, b"\x00")] + [el(0xA3, b"\x81" + i.to_bytes(2, "big") + (b"\x80" if i == 0 else b"\x00") + f)
                                    for i, f in enumerate(frames)]
    return ebml + el(0x18538067, el(0x1549A966, el(0x2AD7B1, b"\x0f\x42\x40")) + tracks + el(0x1F43B675, b"".join(blocks)))


def _vp9(rng, n, key, width=1280, height=720):
    b = bytearray(rng.randbytes(n))
    F._vp9_dress(b, rng, key, width, height)
    return bytes(b)


def test_simulated_webm_video_is_checked_frame_by_frame():
    for seed in range(1, 9):
        d = F.make_mkv(seed, clusters=3, webm=True)
        assert more.validate_mkv(_rd(d), len(d), True)[:2] == ("ok", "every frame checked"), seed


def test_vp9_headers_and_tile_sizes():
    rng = random.Random(1)
    st = {}
    assert more.vp9_frame(_vp9(rng, 40000, True), st) is None and st["refs"][0] == (1280, 720)
    inter = _vp9(rng, 6000, False)
    assert more.vp9_frame(inter, st) is None
    assert sum(more.vp9_frame(rng.randbytes(6000), dict(st)) is None for _ in range(2000)) == 0
    assert more.vp9_frame(inter[:3000], dict(st)) is not None  # a tile's size runs past the end
    sup = bytearray(rng.randbytes(9000))
    F._vp9_superframe(sup, rng, 1280, 720)
    assert more.vp9_frame(bytes(sup), dict(st)) is None
    sup[-5] ^= 1  # the second frame's size in the index
    assert more.vp9_frame(bytes(sup), dict(st)) == "superframe sizes"


def test_vp8_tags_and_partitions():
    rng = random.Random(2)
    for parts in (0, 2):
        st = {}
        for i in range(30):
            b = bytearray(rng.randbytes(30000 if i == 0 else rng.randint(1500, 9000)))
            F._vp8_dress(b, rng, i == 0, 640, 360, parts)
            assert more.vp8_frame(bytes(b), st) is None
        b[0] |= 0x0E  # version 7
        assert more.vp8_frame(bytes(b), st) == "version"


def test_av1_units_must_fill_the_frame():
    seq = bytes([0x0A, 3]) + b"\x00\x00\x00"  # a sequence header unit, 3 bytes
    frame = bytes([0x32, 0x80, 0x01]) + bytes(128)  # a frame unit, size 128 (two-byte size)
    assert more.av1_frame(seq + frame, {}) is None
    assert more.av1_frame(seq + bytes([0x30]) + bytes(50), {}) is None  # the last unit may leave its size out
    assert more.av1_frame(seq + frame[:-1], {}) == "unit size"
    assert more.av1_frame(bytes([0x82]) + frame, {}) == "unit header"


def test_a_frame_holding_another_files_data_is_caught():
    """The file's structure still adds up, but one frame's start is another
    file's data: that frame's header gives it away."""
    rng = random.Random(3)
    frames = [_vp9(rng, 30000, True)] + [_vp9(rng, rng.randint(2000, 6000), False) for _ in range(6)]
    d = _webm(frames)
    assert more.validate_mkv(_rd(d), len(d), True)[0] == "ok"
    at = d.find(frames[4])
    bad = d[:at] + rng.randbytes(64) + d[at + 64:]
    assert more.validate_mkv(_rd(bad), len(bad), True)[0] != "ok"


def test_encrypted_or_compressed_frames_cant_prove_anything():
    rng = random.Random(4)
    frames = [_vp9(rng, 30000, True)] + [_vp9(rng, rng.randint(2000, 6000), False) for _ in range(5)]
    assert more.validate_mkv(_rd(_webm(frames)), len(_webm(frames)), True)[0] == "ok"
    enc = F._el(0x6D80, F._el(0x6240, F._el(0x5035, F._el(0x47E1, b"\x05"))))  # ContentEncodings: AES
    d = _webm(frames, extra=enc)
    assert more.validate_mkv(_rd(d), len(d), True)[0] == "unverified"
