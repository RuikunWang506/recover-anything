"""A Mac leaves a hidden ._ companion next to every file it copies onto a card,
and both Macs and Windows leave their own system files there. Deleted ._
companions (named like the photo, but holding 4 KB of Finder metadata) used to
be reported as photos that couldn't be recovered."""
import hashlib
import os

import pytest

from recover_anything import organize
from recover_anything.clutter import is_clutter
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.triage import triage


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_a_macs_hidden_companions_are_not_reported_as_lost_photos(tmp_path, fs):
    sc = scenarios.mac_copied(str(tmp_path), fs=fs)
    res = scan(sc.image, workers=1)
    w = organize.write_all(res, str(tmp_path / "out"))
    got = {}
    for r in w["written"]:
        got[hashlib.sha256(open(r.meta["written_to"], "rb").read()).hexdigest()] = \
            os.path.basename(r.meta["written_to"])
    assert {sha: got.get(sha) for sha in sc.truth} == sc.truth      # every photo, under its own name
    assert set(got) == set(sc.truth)                                 # and nothing else saved
    assert [r.name for r in res.found if r.status == "lost"] == []
    assert res.live_count == 0                                       # the system's files aren't the owner's


def test_triage_counts_only_the_owners_files(tmp_path):
    sc = scenarios.mac_copied(str(tmp_path))
    t = triage(sc.image)
    assert t["deleted_entries"] == 12 and t["visible_files"] == 0


def test_what_counts_as_clutter():
    for path, name in (("DCIM/100CANON", "._IMG_0001.JPG"), ("DCIM/100CANON", ".DS_Store"),
                       (".fseventsd", "fseventsd-uuid"), (".Spotlight-V100/Store-V2", "store.db"),
                       ("System Volume Information", "IndexerVolumeGuid"), ("", ".apdisk")):
        assert is_clutter(path, name), (path, name)
    for path, name in (("DCIM/100CANON", "IMG_0001.JPG"), ("DCIM/100CANON", "Thumbs.db"),
                       ("Documents", "_notes.txt"), ("Documents", ".hidden_but_mine.txt")):
        assert not is_clutter(path, name), (path, name)
