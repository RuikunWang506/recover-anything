"""iPhone photos (HEIC): every tile of the picture is checked where the file's
item table says it is, so a deleted one can come back as recovered, and a
split one is rebuilt from that table."""
import os
import random

from conftest import SMALL, recovered_hashes, sha
from recover_anything.formats import others
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card


def _rd(d):
    return lambda o, n: d[o:o + n]


def test_every_tile_is_found_and_checked():
    d = F.make_heic(3, size=1_200_000)
    items = others.heif_items(_rd(d), len(d))
    assert sorted({t for t, _ in items.values()}) == [b"Exif", b"grid", b"hvc1"]
    assert others.validate_heif(_rd(d), len(d))[0] == "ok"


def test_a_tile_that_isnt_where_the_table_says_is_caught():
    d = bytearray(F.make_heic(4, size=900_000))
    items = others.heif_items(_rd(bytes(d)), len(d))
    off = sorted(ext[0][0] for t, ext in items.values() if t == b"hvc1")[5]
    d[off:off + 4096] = random.Random(1).randbytes(4096)  # another file's cluster where a tile starts
    st, why, at = others.validate_heif(_rd(bytes(d)), len(d))
    assert st == "corrupt" and at == off, (st, why, at)


def test_a_deleted_iphone_photo_comes_back_recovered(workdir):
    """Regression: a deleted HEIC was never more than "check that it opens":
    nothing in it was checked."""
    img = os.path.join(workdir, "phone.img")
    heic = F.make_heic(5, size=700_000)
    with Fat32Card(img, **SMALL) as card:
        card.write_file("DCIM/100APPLE/IMG_0001.HEIC", heic)
        card.format()
    got = recovered_hashes(scan(img, workers=1))
    assert sha(heic) in got


def test_a_split_iphone_photo_is_rebuilt(workdir):
    """The phone fills the hole a deleted photo left, then goes on past the next
    photo. That photo checks out, so its clusters are its own, and the tiles
    say where the rest of this one is."""
    img = os.path.join(workdir, "split.img")
    heic, other = F.make_heic(6, size=600_000), F.make_heic(7, size=300_000)
    with Fat32Card(img, **SMALL) as card:
        card.write_file("IMG_0001.HEIC", F.make_heic(8, size=200_000))
        card.write_file("hole.bin", random.Random(2).randbytes(4096 * 30))
        card.write_file("IMG_0003.HEIC", other)
        card.delete("hole.bin")
        cl = card.write_file("IMG_0002.HEIC", heic)
        card.format()
    assert any(b != a + 1 for a, b in zip(cl, cl[1:]))
    got = recovered_hashes(scan(img, workers=1))
    assert sha(other) in got
    assert sha(heic) in got and got[sha(heic)].meta.get("reassembled_from", 1) > 1


def test_a_split_photo_whose_break_cant_be_placed_is_only_saved(workdir):
    """Between two tile starts any cluster fits: when what lies between isn't
    known to be another file's, two layouts explain the photo and it is saved
    as "check that it opens", never called recovered."""
    img = os.path.join(workdir, "split2.img")
    heic = F.make_heic(6, size=600_000)
    with Fat32Card(img, **SMALL) as card:
        card.write_file("a.bin", random.Random(1).randbytes(20000))
        card.write_file("hole.bin", random.Random(2).randbytes(4096 * 30))
        card.write_file("b.bin", random.Random(3).randbytes(30000))
        card.delete("hole.bin")
        card.write_file("IMG_0002.HEIC", heic)
        card.format()
    res = scan(img, workers=1)
    heics = [r for r in res.found if r.ext == ".heic"]
    assert heics and all(r.status != "ok" for r in heics)
