"""NTFS drives: a deleted file's MFT record keeps its name, its folder and the
exact list of its pieces, so it comes back named, in its folder, even when it
was stored in several pieces. Before this, NTFS files were only carved."""
import hashlib
import struct

import pytest

from recover_anything import organize
from recover_anything.fs import ntfs
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.simcard.ntfs import encode_runlist
from recover_anything.triage import triage


@pytest.mark.parametrize("cs", [4096, 1024])
def test_deleted_files_on_ntfs_come_back_named_and_in_their_folders(tmp_path, cs):
    sc = scenarios.ntfs_drive(str(tmp_path), cluster_size=cs)
    res = scan(sc.image, workers=1)
    w = organize.write_all(res, str(tmp_path / "out"))
    got = {}
    for r in w["written"]:
        got[hashlib.sha256(open(r.meta["written_to"], "rb").read()).hexdigest()] = r
    for sha, name in sc.truth.items():
        assert sha in got, "%s not recovered" % name
        assert got[sha].name == name
        assert got[sha].path == sc.groups["folders"][sha] or \
            got[sha].meta.get("original_path", "").replace("\\", "/").endswith(sc.groups["folders"][sha] + "/" + name)
    for sha, r in got.items():
        if r.status == "ok":
            assert sha in sc.truth, "%s marked recovered but wrong" % r.name     # CLAUDE.md rule 3
    (overwritten,) = sc.groups["partial"]
    assert overwritten not in got or got[overwritten].status != "ok"
    assert any(r.name == "IMG_0009.JPG" and r.status == "partial" for r in w["written"])
    assert res.live_count == 4            # photos 4 and 5, outline.txt, new video.part


def test_triage_reads_ntfs(tmp_path):
    sc = scenarios.ntfs_drive(str(tmp_path))
    t = triage(sc.image)
    assert t["volumes"][0]["filesystem"] == "NTFS"
    assert t["deleted_entries"] >= 8 and t["in_card_trash"] == 2


def test_runlists():
    runs = [(100, 5), (40, 3), (70000, 1), (5, 2)]          # forward, backward, far, backward
    assert ntfs.decode_runlist(encode_runlist(runs), 0, 64) == runs
    sparse = bytes([0x01, 0x04, 0x11, 0x02, 0x20, 0x00])     # 4 sparse clusters, then 2 at LCN 32
    assert ntfs.decode_runlist(sparse, 0, len(sparse)) == [(None, 4), (32, 2)]
    with pytest.raises(ValueError):
        ntfs.decode_runlist(bytes([0x0F, 1, 2]), 0, 3)


def test_fixups_catch_a_torn_record():
    rec = bytearray(1024)
    rec[0:4] = b"FILE"
    struct.pack_into("<HH", rec, 4, 0x30, 3)
    rec[0x30:0x32] = b"\x07\x00"                              # check value
    rec[0x32:0x36] = b"AB" + b"CD"                            # the real last two bytes of each sector
    rec[510:512] = rec[1022:1024] = b"\x07\x00"
    good = bytearray(rec)
    assert ntfs.fixup(good) and good[510:512] == b"AB" and good[1022:1024] == b"CD"
    torn = bytearray(rec)
    torn[1022:1024] = b"\x00\x00"                             # second sector from another write
    assert not ntfs.fixup(torn)
