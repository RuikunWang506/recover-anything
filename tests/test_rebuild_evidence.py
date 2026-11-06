"""What rebuilders may assume about clusters: in-use and folder clusters are
nobody's deleted data, files that check out take their clusters out of the
others' way, and an unchecked stretch is filled the way files are stored."""
import os

import pytest

from conftest import SMALL, recovered_hashes, sha
from recover_anything.pipeline import scan
from recover_anything.simcard.fat32 import Fat32Card

pytest.importorskip("PIL")


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_allocation_map_matches_the_per_cluster_lookup(workdir, fs):
    from recover_anything.simcard.media import make_photo
    from recover_anything.simcard.scenarios import _card
    from recover_anything.source import Source
    from recover_anything.volume import find_volumes, open_filesystem
    img = os.path.join(workdir, "alloc.img")
    with _card(img, fs, **SMALL) as card:
        for i in range(6):
            card.write_file("DCIM/IMG_%04d.JPG" % i, make_photo(i, width=640, height=480))
        card.delete("DCIM/IMG_0002.JPG")
    with Source(img) as src:
        fsr = open_filesystem(src, find_volumes(src)[0])
        list(fsr.walk())
        m = fsr.allocated_map()
        assert len(m) == fsr.cluster_count + 2
        assert [i for i in range(2, len(m)) if m[i]] == [c for c in range(2, len(m)) if fsr.is_allocated(c)]
        assert sum(m) > 0


def test_lost_folders_are_known_as_folders(workdir):
    """Regression: a folder's own cluster, left over from before a format,
    looked free, and a video rebuild took it as part of the video."""
    from recover_anything.simcard.media import make_photo
    from recover_anything.source import Source
    from recover_anything.volume import find_volumes, open_filesystem
    img = os.path.join(workdir, "dirs.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("Videos/a.bin", b"x" * 5000)
        card.write_file("Music/b.bin", make_photo(1, width=320, height=240))
        dirs = [card.mkdir("Videos").clusters[0], card.mkdir("Music").clusters[0]]
        card.format()
    with Source(img) as src:
        fsr = open_filesystem(src, find_volumes(src)[0])
        list(fsr.walk())
        list(fsr.lost_folder_records())
        assert set(dirs) <= fsr.dir_clusters


@pytest.fixture(scope="module")
def card5(tmp_path_factory):
    """Regression, from a randomized card (seed 5): two videos split around a
    folder's cluster, a dashcam clip and an unproven HEIC photo were both
    rebuilt from the wrong pieces. Now the first is recovered, and the second,
    whose break falls inside one large frame where nothing can be checked,
    comes back byte for byte (still labeled 'check that it plays', because
    nothing proves those clusters)."""
    from recover_anything.simcard import scenarios
    from recover_anything.simcard.fat32 import Fat32Card as Card
    log = {}
    orig = Card.write_file

    def wf(self, path, data, t=None):
        log[path] = data
        return orig(self, path, data, t)
    Card.write_file = wf
    try:
        sc = scenarios.mixed_device(str(tmp_path_factory.mktemp("card5")), 5)
    finally:
        Card.write_file = orig
    return sc, log, scan(sc.image, workers=1)


def test_a_video_split_around_a_folder_and_a_dashcam_clip_is_rebuilt(card5):
    sc, log, res = card5
    assert sha(log["DCIM/MVI_0047.MP4"]) in recovered_hashes(res)


def test_an_unchecked_stretch_is_filled_with_the_fewest_jumps(card5):
    sc, log, res = card5
    want = sha(log["DCIM/MVI_0053.MP4"])
    exact = [r for r in res.found if r.extents and sha(r.read(res._src)) == want]
    assert exact and exact[0].status in ("ok", "unverified")


def test_the_shared_card_still_marks_nothing_wrong(card5):
    sc, log, res = card5
    assert set(recovered_hashes(res)) <= sc.groups["ever written"]
