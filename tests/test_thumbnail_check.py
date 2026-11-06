"""A rebuilt photo is checked against the small preview (EXIF thumbnail) the
camera stored in it: a piece of another photo that happens to decode shows up
as part of the picture no longer matching its own thumbnail."""
import datetime
import io

import pytest

from recover_anything.formats import jpeg
from recover_anything.simcard.media import _strip_app0, build_exif, make_photo

pytest.importorskip("PIL")


def _rd(d):
    return lambda o, n: d[o:o + n]


def _decode(data):
    h = jpeg.parse_header(_rd(data), 0)
    dec = jpeg.Decoder(h)
    dec.buf = data
    assert dec.run(final=True) == "done"
    return h, dec


def _thumb(h, data):
    o, n = h.thumb
    return jpeg.Thumb(h, data[o:o + n])


def test_a_photo_matches_its_own_thumbnail():
    for seed, kw in [(1, {}), (2, dict(optimize=True)), (3, dict(restart_blocks=64)), (4, dict(quality=97))]:
        data = make_photo(seed, width=1200, height=900, **kw)
        h, dec = _decode(data)
        assert h.thumb and h.q0, seed
        assert _thumb(h, data).unlike(dec) is None, seed


def test_a_piece_of_another_photo_shows_against_the_thumbnail():
    data = make_photo(5, width=1200, height=900)
    h, dec = _decode(data)
    t = _thumb(h, data)
    # What a piece of another photo does: each block's brightness is stored as
    # a step from the block before, so everything after it comes out brighter.
    start = dec.total * 2 // 3 * dec.bh * dec.bv
    step = round(30 * 8 / h.q0[h.components[0][3]])  # 30 levels
    for i in range(start, len(dec.dc)):
        dec.dc[i] += step
    first, last = t.unlike(dec)
    assert dec.total // 2 <= first <= last < dec.total


def test_thumbnail_with_black_bars():
    """DSLRs store a 3:2 photo's thumbnail as 160x120 with black bars."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1200, 800), (40, 90, 160))
    d = ImageDraw.Draw(im)
    d.rectangle((100, 500, 1100, 800), fill=(200, 180, 120))
    d.ellipse((700, 100, 1000, 400), fill=(250, 250, 230))
    t = Image.new("RGB", (160, 120))
    t.paste(im.resize((160, 107)), (0, 6))
    tb = io.BytesIO()
    t.save(tb, "JPEG", quality=80)
    exif = build_exif(datetime.datetime(2026, 1, 2, 3, 4, 5), "Canon", "EOS", _strip_app0(tb.getvalue()))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, exif=exif, subsampling=2)
    data = _strip_app0(out.getvalue())
    h, dec = _decode(data)
    assert _thumb(h, data).unlike(dec) is None


def test_a_thumbnail_of_another_shape_is_not_used():
    """A thumbnail turned on its side doesn't show the picture's layout."""
    from PIL import Image
    im = Image.new("RGB", (1200, 900), (90, 120, 30))
    t = Image.new("RGB", (120, 160), (90, 120, 30))
    tb = io.BytesIO()
    t.save(tb, "JPEG", quality=80)
    exif = build_exif(datetime.datetime(2026, 1, 2, 3, 4, 5), "Canon", "EOS", _strip_app0(tb.getvalue()))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, exif=exif)
    data = _strip_app0(out.getvalue())
    h = jpeg.parse_header(_rd(data), 0)
    with pytest.raises(jpeg.JpegError):
        _thumb(h, data)


def test_a_search_leaves_the_block_table_as_it_found_it(monkeypatch):
    """Regression: trial decodes of candidate clusters wrote other photos'
    blocks into the table that seam scores and the thumbnail check read, and a
    search that found nothing left them there. A correct rebuild was then
    rejected because of blocks that weren't its own."""
    from recover_anything import reassemble as R
    cs = 4096
    a, b = make_photo(11), make_photo(12)  # same camera settings: B's data decodes as A's for a while
    ca, cb = [a[i:i + cs] for i in range(0, len(a), cs)], [b[i:i + cs] for i in range(0, len(b), cs)]
    clusters = ca[:60] + cb[40:50] + ca[60:]
    card = b"".join(c.ljust(cs, b"\0") for c in clusters)

    class Geom:
        cluster_size, last_cluster = cs, len(clusters) - 1

        def offset(self, c):
            return c * cs

    class Src:
        def read(self, o, n):
            return card[o:o + n]

    same = []
    orig = R._Assembler._search

    def search(self, dec, path, buf, states, j, rejected, mine):
        before = dec.dc[:]
        out = orig(self, dec, path, buf, states, j, rejected, mine)
        same.append(dec.dc == before)
        return out
    monkeypatch.setattr(R._Assembler, "_search", search)
    out = R._Assembler(Src(), Geom(), bytearray(len(clusters))).run(0, len(a))
    assert out and out[2] == "ok" and out[0] == list(range(60)) + list(range(70, len(clusters)))
    assert same and all(same), "%d of %d searches changed the block table" % (same.count(False), len(same))


def test_a_damaged_photo_the_decoder_accepts_is_caught_by_its_thumbnail():
    """The decoder falls back into step after damage: this photo, with 100
    bytes wiped, still decodes to exactly the right number of blocks. Its
    thumbnail shows that part of the picture is wrong."""
    a = make_photo(21)
    d = a[:60960] + bytes(100) + a[61060:]
    assert jpeg.check(d)[0] == "ok"
    assert jpeg.check(d, thumb=True)[0] == "unverified"
    assert jpeg.check(a, thumb=True)[0] == "ok"
