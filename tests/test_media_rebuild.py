"""Trail-camera AVIs and GIFs stored in pieces are put back together:
every Motion-JPEG frame decoded, every GIF picture decoded to its last pixel."""
import os
import random

import pytest

from conftest import SMALL, recovered_hashes, sha
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card

pytest.importorskip("PIL")


def _split_card(workdir, name, data, hole_clusters):
    """`data` (saved at the card's root, so no new folder takes the hole) fills
    the hole a deleted file left, and goes on after the next file."""
    img = os.path.join(workdir, "split.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("a.bin", random.Random(1).randbytes(20000))
        card.write_file("hole.bin", random.Random(2).randbytes(4096 * hole_clusters))
        card.write_file("b.bin", random.Random(3).randbytes(30000))
        card.delete("hole.bin")
        cl = card.write_file(name, data)
        card.format()
    assert any(b != a + 1 for a, b in zip(cl, cl[1:])), "the file should be in pieces"
    return img


def _found(img):
    res = scan(img, workers=1)
    return {sha(r.read(res._src)): r for r in res.found if r.extents}


def test_a_split_trail_camera_video_is_rebuilt(workdir):
    data = F.make_avi(4, frames=12)
    got = _found(_split_card(workdir, "IMAG0001.AVI", data, 3))
    assert sha(data) in got and got[sha(data)].status == "ok"


def test_a_join_inside_sound_proves_nothing(workdir):
    """Nothing can check sound data: a rebuilt AVI whose join falls inside it
    is saved, but labeled."""
    data = F.make_avi(5, frames=6, audio=9000)
    got = _found(_split_card(workdir, "IMAG0002.AVI", data, 3))
    r = got.get(sha(data))
    assert r is not None and r.status == "unverified", r and (r.status, r.note)


def test_a_split_gif_is_rebuilt(workdir):
    data = F.make_gif(6, frames=5)
    assert len(data) > 4096
    got = _found(_split_card(workdir, "anim.gif", data, 1))
    assert sha(data) in got and got[sha(data)].status == "ok"
