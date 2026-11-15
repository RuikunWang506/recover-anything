"""A deleted, carved or rebuilt photo is called recovered only when every block
decodes AND it matches the preview the camera stored in it (CLAUDE.md rule 3):
the decoder falls back into step after damage, so another photo's clusters
can decode cleanly, and a structure check can't see them at all.

Each of these used to be "ok" on less: a structure check on cards with over
8 GB of photos (only a random sample was decoded), a progressive JPEG (never
decoded), a photo without a preview (decoder only), a rebuilt photo without
a preview, and --fast."""
import io
import os

import pytest

from recover_anything import validate
from recover_anything.records import Found
from recover_anything.simcard.media import make_photo
from recover_anything.simcard.scenarios import PHOTO_STYLE, _when


def _photo(i, **kw):
    return make_photo(i, taken=_when(i), style=PHOTO_STYLE, **kw)


def _spliced(i, j, **kw):
    """Photo i with one 4 KB stretch in its picture data taken from photo j
    (the same camera settings): what a reused-then-freed cluster leaves."""
    a, b = _photo(i, **kw), _photo(j, **kw)
    at = (len(a) // 2) // 4096 * 4096
    return a[:at] + b[at:at + 4096] + a[at + 4096:]


def _progressive(data):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, progressive=True, exif=im.info.get("exif", b""))
    return out.getvalue()


class _Img:
    """A source image holding the given files one after another."""

    def __init__(self, tmp_path, blobs):
        self.path = str(tmp_path / "card.img")
        self.spans = []
        with open(self.path, "wb") as f:
            for b in blobs:
                off = f.tell()
                f.write(b)
                f.write(bytes((-len(b)) % 4096))
                self.spans.append((off, len(b)))

    def record(self, k, deleted=True):
        off, n = self.spans[k]
        r = Found(kind="jpeg", ext=".jpg", extents=[(off, n)], origin="filesystem", name="IMG_%04d.JPG" % k,
                  deleted=deleted, expected_size=n)
        if not deleted:
            r.meta["live"], r.meta["chain"] = True, "fat"
        return r


def _validate(img, recs, mode="auto", **patch):
    from recover_anything.source import Source
    with Source(img.path) as src:
        return validate.validate_all(src, recs, workers=1, mode=mode)


def test_the_splice_is_one_a_structure_check_misses():
    """The premise: only decoding and the preview catch it."""
    from recover_anything.formats import jpeg
    data = _spliced(1, 2)
    assert jpeg.check_fast(data)[0] == "ok"
    assert jpeg.check(data, thumb=True)[0] != "ok"


def test_on_a_big_card_deleted_photos_are_still_decoded_and_compared(tmp_path, monkeypatch):
    """Above DEEP_LIMIT only a sample of photos used to be decoded; a deleted
    one outside the sample kept its structure-only "ok"."""
    monkeypatch.setattr(validate, "DEEP_LIMIT", 0)                # as if there were terabytes of photos
    monkeypatch.setattr(validate, "SAMPLE", 0)                    # and the sample missed it
    img = _Img(tmp_path, [_photo(10), _photo(11), _spliced(1, 2), _photo(3)])
    live = [img.record(0, deleted=False), img.record(1, deleted=False)]
    spliced, fine = img.record(2), img.record(3)
    _validate(img, live + [spliced, fine])
    assert spliced.status != "ok"
    assert fine.status == "ok" and fine.note == "every block decoded"
    assert all(r.status == "ok" for r in live)                   # visible photos: sampling is still fine


def test_a_deleted_progressive_jpeg_is_not_called_recovered(tmp_path):
    """The decoder can't read progressive JPEGs (messaging apps, the web), so
    nothing but structure can be checked: fine for a file never deleted."""
    img = _Img(tmp_path, [_progressive(_spliced(1, 2)), _progressive(_photo(3))])
    spliced, live = img.record(0), img.record(1, deleted=False)
    _validate(img, [spliced, live])
    assert spliced.status == "unverified"
    assert live.status == "ok"


def test_a_deleted_photo_without_a_preview_is_not_called_recovered(tmp_path):
    img = _Img(tmp_path, [_photo(1, thumbnail=False), _photo(2)])
    no_preview, with_preview = img.record(0), img.record(1)
    _validate(img, [no_preview, with_preview], mode="deep")
    assert no_preview.status == "unverified" and "preview" in no_preview.note
    assert with_preview.status == "ok"


def test_fast_mode_labels_deleted_photos_instead_of_calling_them_recovered(tmp_path):
    img = _Img(tmp_path, [_photo(1), _photo(2)])
    deleted, live = img.record(0), img.record(1, deleted=False)
    _validate(img, [deleted, live], mode="fast")
    assert deleted.status == "unverified" and "fast" in deleted.note
    assert live.status == "ok"


def test_a_rebuilt_photo_without_a_preview_is_not_called_recovered(tmp_path):
    """A photo split by a hole, then the card formatted: the rebuild decodes,
    but without a preview nothing shows that every piece is its own."""
    import datetime as dt

    from recover_anything.pipeline import scan
    from recover_anything.simcard.fat32 import Fat32Card
    path = str(tmp_path / "card.img")
    with Fat32Card(path, size=96 * 1024**2, cluster_size=1024) as card:
        t = dt.datetime(2026, 7, 4, 9, 30)
        card.write_file("DCIM/100CANON/IMG_0001.JPG", _photo(1, thumbnail=False), t)
        card.write_file("DCIM/100CANON/x.tmp", os.urandom(20000), t)
        card.write_file("DCIM/100CANON/IMG_0002.JPG", _photo(2, thumbnail=False), t)
        card.delete("DCIM/100CANON/x.tmp")
        split = _photo(3, thumbnail=False)
        clusters = card.write_file("DCIM/100CANON/IMG_0003.JPG", split, t)   # fills the hole, continues after
        assert any(b != a + 1 for a, b in zip(clusters, clusters[1:]))
        card.format()
    res = scan(path, workers=1)
    from recover_anything.source import Source
    with Source(path) as src:
        rebuilt = [r for r in res.found if r.kind == "jpeg" and len(r.extents) > 1
                   and b"".join(src.read(o, n) for o, n in r.extents) == split]
    assert rebuilt, "the split photo should be put back together (with the right bytes)"
    for r in rebuilt:
        assert r.status != "ok", (r.name, r.note)
