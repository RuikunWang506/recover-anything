import io

import pytest

from recover_anything.formats import jpeg, others
from recover_anything.simcard.media import make_photo, make_video

pytest.importorskip("PIL")


@pytest.mark.parametrize("kw", [{}, {"restart_blocks": 8}, {"optimize": True}, {"quality": 97}])
def test_decoder_accepts_real_jpegs(kw):
    p = make_photo(3, width=640, height=480, **kw)
    st, msg, pos = jpeg.check(p)
    assert st == "ok", msg
    m = jpeg.measure(lambda o, n: p[o:o + n], 0)
    assert m.complete and m.end == len(p)


def test_decoder_catches_spliced_data():
    a = make_photo(1, width=1024, height=768, optimize=True)
    b = make_photo(2, width=1024, height=768, optimize=True)
    mid = len(a) // 2
    bad = a[:mid] + b[mid:mid + 8192] + a[mid + 8192:]
    st, msg, pos = jpeg.check(bad)
    assert st == "corrupt"
    assert mid - 64 <= pos <= mid + 8192 + 4096


def test_truncated_is_partial():
    a = make_photo(4, width=800, height=600)
    st, _msg, pos = jpeg.check(a[: len(a) // 2])
    assert st == "partial"


def test_progressive_is_structure_checked():
    from PIL import Image
    im = Image.new("RGB", (300, 200), (90, 120, 200))
    b = io.BytesIO()
    im.save(b, "JPEG", progressive=True)
    st, msg, _ = jpeg.check(b.getvalue())
    assert st == "ok"


def test_exif_date_and_thumbnail():
    from recover_anything.exif import jpeg_exif
    import datetime as dt
    p = make_photo(9, width=640, height=480, taken=dt.datetime(2025, 12, 24, 18, 30, 5))
    when, thumb = jpeg_exif(p)
    assert when == dt.datetime(2025, 12, 24, 18, 30, 4) or when == dt.datetime(2025, 12, 24, 18, 30, 5)
    assert thumb and thumb[:2] == b"\xff\xd8"


def test_mp4_validation_finds_foreign_cluster():
    v = make_video(5, frames=90)
    read = lambda o, n: v[o:o + n]  # noqa: E731
    assert others.validate_isobmff(read, len(v))[0] == "ok"
    m = others.measure_isobmff(read, 0, len(v) + 10000)
    assert m.complete and m.end == len(v)
    bad = bytearray(v)
    bad[200000:232768] = bytes(range(256)) * 128
    st, why, pos = others.validate_isobmff(lambda o, n: bytes(bad[o:o + n]), len(bad))
    assert st == "corrupt"


def test_png_measure():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (200, 100), (1, 2, 3)).save(b, "PNG")
    data = b.getvalue() + b"\x00" * 1000
    m = others.measure_png(lambda o, n: data[o:o + n], 0, len(data))
    assert m.complete and m.end == len(b.getvalue())


def _naive_samples(offsets, sizes, stsc):
    out, si = [], 0
    runs = [tuple(stsc[i:i + 3]) for i in range(0, len(stsc), 3)]
    for ci, coff in enumerate(offsets, start=1):
        per = 1
        for first, spc, _ in runs:
            if ci >= first:
                per = spc
        pos = coff
        for _ in range(per):
            if si >= len(sizes):
                break
            out.append((pos, sizes[si]))
            pos += sizes[si]
            si += 1
    return out


def test_sample_walk_matches_the_spec_and_is_linear():
    import random
    import time
    rng = random.Random(3)
    for _ in range(300):
        chunks = rng.randrange(0, 40)
        offsets = sorted(rng.randrange(0, 1 << 30) for _ in range(chunks))
        sizes = [rng.randrange(0, 5000) for _ in range(rng.randrange(0, 200))]
        firsts = sorted(rng.randrange(1, 45) for _ in range(rng.randrange(0, 6)))
        stsc = [v for f in firsts for v in (f, rng.randrange(0, 9), 1)]
        assert list(others.iter_samples(offsets, sizes, stsc)) == _naive_samples(offsets, sizes, stsc)
    # 100,000 chunks and 50,000 runs: the old nested walk took minutes
    offsets = list(range(0, 100000 * 64, 64))
    stsc = [v for k in range(50000) for v in (2 * k + 1, 1, 1)]
    t = time.time()
    assert sum(1 for _ in others.iter_samples(offsets, others._Fixed(8, 100000), stsc)) == 100000
    assert time.time() - t < 5


def test_tiny_index_claiming_millions_of_frames_is_refused():
    import struct

    def box(t, body):
        return struct.pack(">I", 8 + len(body)) + t + body

    def full(t, body):
        return box(t, b"\0\0\0\0" + body)

    stsd = full(b"stsd", struct.pack(">I", 1) + struct.pack(">I", 16) + b"avc1" + bytes(8))
    stsz = full(b"stsz", struct.pack(">II", 5, 50_000_000))  # 50M frames in 20 bytes
    stsc = full(b"stsc", struct.pack(">IIII", 1, 1, 50_000_000, 1))
    stco = full(b"stco", struct.pack(">II", 1, 64))
    stbl = box(b"stbl", stsd + stsz + stsc + stco)
    moov = box(b"moov", box(b"trak", box(b"mdia", box(b"minf", stbl))))
    data = box(b"ftyp", b"isom" + bytes(4)) + moov + box(b"mdat", bytes(1000))
    st, why, _ = others.validate_isobmff(lambda o, n: data[o:o + n], len(data))
    assert (st, why) == ("unverified", "too many frames to check one by one")
