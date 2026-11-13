"""Files deleted on a computer sit whole in the trash folder on the card itself
(macOS .Trashes, Linux .Trash-1000, Windows $RECYCLE.BIN). They used to be
skipped as "still visible" and their old entries reported as overwritten."""
import hashlib
import os

import pytest

from recover_anything import organize, trash
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.triage import triage


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_photos_in_the_cards_trash_come_back_by_default_with_their_names(tmp_path, fs):
    sc = scenarios.trashed(str(tmp_path), fs=fs)
    res = scan(sc.image, workers=1)
    out = tmp_path / "out"
    w = organize.write_all(res, str(out))
    got = {}
    for r in w["written"]:
        data = open(r.meta["written_to"], "rb").read()
        got[hashlib.sha256(data).hexdigest()] = (os.path.basename(r.meta["written_to"]), r)
    for sha, name in sc.truth.items():
        assert sha in got, "%s not recovered" % name
        assert got[sha][0] == name                       # the original name, also for Windows $R files
    for sha, (name, r) in got.items():
        if r.status == "ok":
            assert sha in sc.truth, "%s marked recovered but wrong" % name
    # the entries a move into the trash leaves behind are not losses, and the
    # trash's own records are not files the user lost
    assert [r.name for r in res.found if r.status == "lost"] == []
    assert not any(n.endswith(".trashinfo") or n.startswith(("$I", "_I")) or n == ".DS_Store"
                   for n, _ in got.values())
    assert res.trash_count == 12                          # 15 trashed, 3 of them emptied from the trash again
    assert res.live_count == 9                            # only the photos still in DCIM


def test_triage_counts_the_files_in_the_cards_trash(tmp_path):
    sc = scenarios.trashed(str(tmp_path))
    t = triage(sc.image)
    assert t["in_card_trash"] == 12
    assert t["visible_files"] == 9
    assert t["odds"] == "good" and "trash" in t["summary"]


def test_trash_folders_and_their_records():
    assert trash.kind(".Trashes/501", "IMG_0001.JPG") == "mac"
    assert trash.kind(".Trash-1000/files", "IMG_0001.JPG") == "linux"
    assert trash.kind("$RECYCLE.BIN/S-1-5-21-1-2-3-1001", "$RAB12CD.JPG") == "windows"
    assert trash.kind("$RECYCLE.BIN/S-1-5-21-1-2-3-1001", "_RAB12CD.JPG") == "windows"   # deleted short name
    assert trash.kind("DCIM/100CANON", "IMG_0001.JPG") is None
    assert trash.kind("Trashes", "IMG_0001.JPG") is None
    for path, name in ((".Trashes/501", ".DS_Store"), (".Trash-1000/info", "a.jpg.trashinfo"),
                       ("$RECYCLE.BIN/S-1-5-21-1-2-3-1001", "$IAB12CD.JPG"),
                       ("$RECYCLE.BIN/S-1-5-21-1-2-3-1001", "desktop.ini"), ("RECYCLED", "INFO2")):
        assert trash.bookkeeping(path, name), (path, name)
        assert not trash.kind(path, name), (path, name)
    assert trash.companion(".Trash-1000/files", "a b.JPG") == (".Trash-1000/info", "a b.JPG.trashinfo")
    assert trash.companion("$RECYCLE.BIN/S-1", "_RAB12CD.JPG") == ("$RECYCLE.BIN/S-1", "$IAB12CD.JPG")


def test_original_paths_from_windows_and_linux_records():
    v1 = scenarios._win_trash_record("E:\\DCIM\\100CANON\\IMG_0042.JPG", 1234, version=1)
    v2 = scenarios._win_trash_record("E:\\Photos\\Été 2026\\IMG_0043.JPG", 1234, version=2)
    assert trash.original_path(v1) == "E:\\DCIM\\100CANON\\IMG_0042.JPG"
    assert trash.split(trash.original_path(v2)) == ("Photos/Été 2026", "IMG_0043.JPG")
    info = b"[Trash Info]\nPath=/media/me/CARD/DCIM/My%20Trip/IMG_0044.JPG\nDeletionDate=2026-07-06T10:00:00\n"
    assert trash.split(trash.original_path(info)) == ("media/me/CARD/DCIM/My Trip", "IMG_0044.JPG")
    # a deleted record whose space was reused: not mistaken for a path
    assert trash.original_path(b"\xff\xd8\xff\xe1" + bytes(600)) is None
    assert trash.original_path(bytes(600)) is None
