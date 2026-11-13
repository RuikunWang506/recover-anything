"""'This card needs to be formatted': the start of the card is damaged and no
computer can open it, though nothing was deleted. A default recovery used to
say "Nothing recoverable was found" and call the photos "still visible"."""
import hashlib
import os

import pytest

from recover_anything import organize
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.triage import triage


def _written(res, out):
    w = organize.write_all(res, str(out))
    return {hashlib.sha256(open(r.meta["written_to"], "rb").read()).hexdigest(): os.path.basename(r.meta["written_to"])
            for r in w["written"]}


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
@pytest.mark.parametrize("damage", ["boot", "mbr"])
def test_every_photo_on_a_card_the_computer_cant_open_is_copied(tmp_path, fs, damage):
    sc = scenarios.unopenable(str(tmp_path), fs=fs, damage=damage)
    res = scan(sc.image, workers=1)
    got = _written(res, tmp_path / "out")
    assert got == sc.truth                       # every photo, byte for byte, under its own name
    assert res.live_count == 0                   # none of them is "still visible"
    t = triage(sc.image)
    assert t["listed_but_unopenable"] == 10 and t["visible_files"] == 0
    assert t["odds"] == "good" and "Don't format it" in t["summary"]


def test_a_card_the_computer_can_open_still_leaves_visible_files_alone(tmp_path):
    sc = scenarios.unopenable(str(tmp_path), damage="none")     # nothing wiped: a healthy card
    res = scan(sc.image, workers=1)
    assert _written(res, tmp_path / "out") == {}
    assert res.live_count == 10
    assert triage(sc.image)["visible_files"] == 10
