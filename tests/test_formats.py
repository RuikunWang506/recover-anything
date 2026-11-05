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
