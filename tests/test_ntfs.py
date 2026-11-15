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


# ---------------------------------------------------------------- hardening
# Found by an adversarial review of the NTFS reader (14 November 2026). Each
# of these used to mark wrong bytes as recovered, or end the listing.

import datetime as _dt
import os

from recover_anything.fs.ntfs import NtfsVolume
from recover_anything.simcard.files import make_wav
from recover_anything.simcard.ntfs import REC, NtfsCard, _attr, _fname, _std
from recover_anything.source import Source

_T = _dt.datetime(2026, 1, 1)


def _written(res, out):
    w = organize.write_all(res, str(out))
    return [(r, open(r.meta["written_to"], "rb").read()) for r in w["written"]]


def test_data_past_the_valid_length_is_never_called_recovered(tmp_path):
    """Windows shows zeros past a file's valid data length; the disk holds
    older data there (a copy cut short by pulling the drive)."""
    img = str(tmp_path / "ntfs.img")
    wav = make_wav(1, seconds=4.0)
    valid = 8192
    path = "$RECYCLE.BIN/S-1-5-21-1-2-3-1001/$RABC123.wav"
    with NtfsCard(img, size=32 * 1024**2) as v:
        runs = v.write_file(path, wav)
        num = v.by_path[path]
        v.f.seek(v.cluster_offset(runs[0][0]) + valid)
        v.f.write(os.urandom(len(wav) - valid))                  # stale data after what was written
        a = bytearray(v.records[num]["attrs"][2])
        struct.pack_into("<Q", a, 56, valid)                     # initialized size
        v.records[num]["attrs"][2] = bytes(a)
        v._write_record(num)
    recs = {r.name: r for r in NtfsVolume(Source(img), 0).walk()}
    r = recs["$RABC123.wav"]
    assert sum(n for _, n in r.extents) == valid and r.meta["chain"] != "runlist"
    for rec, data in _written(scan(img, workers=1), tmp_path / "out"):
        assert rec.status != "ok", rec.name
        assert data == wav[:len(data)]                           # only bytes that were really written


def test_a_half_written_record_is_not_trusted(tmp_path):
    """A power cut mid-write leaves a record with one sector new and one old:
    its list of pieces may be the old file's, in space since reused."""
    img = str(tmp_path / "ntfs.img")
    name = "Interview, second session, recorded on the porch at dusk, with the neighbours joining in, unedited.wav"
    old, new = make_wav(1, seconds=3.0), make_wav(2, seconds=3.0)
    with NtfsCard(img, size=32 * 1024**2) as v:
        runs_a = v.write_file(name, old)
        num = v.by_path[name]
        off = v._record_offset(num)
        v.f.seek(off)
        rec_a = v.f.read(REC)
        runs_b = v._alloc(sum(n for _, n in runs_a))
        pos = 0
        for lcn, n in runs_b:
            v.f.seek(v.cluster_offset(lcn))
            v.f.write(new[pos:pos + n * v.cs])
            pos += n * v.cs
        v.records[num]["attrs"][2] = _attr(0x80, runs=runs_b, real=len(new), alloc=sum(n for _, n in runs_b) * v.cs,
                                           attr_id=2)
        v.records[num]["seq"] += 1
        v._write_record(num)
        v.f.seek(off)
        rec_b = v.f.read(REC)
        for lcn, n in runs_a:
            v._free(lcn, n)
        v.f.seek(v.cluster_offset(runs_a[0][0]) + 4096)
        v.f.write(os.urandom(len(old) - 4096))                   # the old space is reused
        v.f.seek(off)
        v.f.write(rec_b[:512] + rec_a[512:])                     # torn: new first sector, old second
    (r,) = [r for r in NtfsVolume(Source(img), 0).walk() if r.name == name]
    assert r.status == "lost" and not r.extents and "half-written" in r.note
    for rec, data in _written(scan(img, include_live=True, workers=1), tmp_path / "out"):
        if rec.status == "ok":
            assert data in (old, new), rec.name


def test_a_reused_record_does_not_take_a_deleted_files_leftover_pieces(tmp_path):
    img = str(tmp_path / "ntfs.img")
    old = b"OLD!" * 5000
    new = b"meeting at 10, room 4\n" * 10                         # small: kept inside its record
    with NtfsCard(img, size=32 * 1024**2) as v:
        runs = v._alloc((len(old) + v.cs - 1) // v.cs)
        pos = 0
        for lcn, n in runs:
            v.f.seek(v.cluster_offset(lcn))
            v.f.write(old[pos:pos + n * v.cs])
            pos += n * v.cs
        base = v._free_record()
        v._new_record(base, [_attr(0x10, _std(_T)), _attr(0x30, _fname(5, 5, "old big.bin", _T, len(old), False),
                                                          attr_id=1)], 1)
        ext = v._free_record()
        v.records[ext] = {"seq": 1, "flags": 1, "base": base,
                          "attrs": [_attr(0x80, runs=runs, real=len(old), alloc=len(runs) * v.cs, attr_id=2)]}
        v._write_record(ext)
        for num in (base, ext):                                   # deleted: both records freed
            v.records[num]["flags"] = 0
            v.records[num]["seq"] += 1
            v._write_record(num)
        for lcn, n in runs:
            v._free(lcn, n)
        v.write_file("notes.txt", new)                            # takes the base record
        assert v.by_path["notes.txt"] == base
    src = Source(img)
    (r,) = [r for r in NtfsVolume(src, 0).walk() if r.name == "notes.txt"]
    assert b"".join(src.read(o, n) for o, n in r.extents) == new


def test_an_unreadable_bitmap_does_not_end_the_listing(tmp_path):
    img = str(tmp_path / "ntfs.img")
    with NtfsCard(img, size=32 * 1024**2) as v:
        for i in range(10):
            v.write_file("file%02d.txt" % i, b"%d" % i * 5000)
        v.delete("file00.txt")
        bitmap_at = v.cluster_offset(v.bitmap_lcn)
    with open(img, "r+b") as f:                                   # an image cut short before $Bitmap
        f.truncate(bitmap_at)
    vol = NtfsVolume(Source(img), 0)
    names = sorted(r.name for r in vol.walk())
    assert names == ["file%02d.txt" % i for i in range(10)]
    assert not vol.bitmap_known and vol.warnings
    res = scan(img, workers=1)
    assert any("map of used space" in w for w in res.warnings)


def test_a_runlist_that_cant_be_a_file_is_rejected(tmp_path):
    """One record claiming 20 GB on a small volume used to make recover
    refuse the whole run for lack of space."""
    img = str(tmp_path / "ntfs.img")
    with NtfsCard(img, size=32 * 1024**2) as v:
        v.write_file("IMG_0001.JPG", b"\xff\xd8\xff\xe0" + os.urandom(30000))
        num = v._free_record()
        bogus = [(2000, 3000)] * 40                               # the same clusters again and again
        v._new_record(num, [_attr(0x10, _std(_T)), _attr(0x30, _fname(5, 5, "x.bin", _T, 0, False), attr_id=1),
                            _attr(0x80, runs=bogus, real=40 * 3000 * v.cs, alloc=40 * 3000 * v.cs, attr_id=2)], 1)
        overlap = [(v.mft_lcn, 4)]                                # data inside the MFT
        num2 = v._free_record()
        v._new_record(num2, [_attr(0x10, _std(_T)), _attr(0x30, _fname(5, 5, "y.bin", _T, 0, False), attr_id=1),
                             _attr(0x80, runs=overlap, real=4 * v.cs, alloc=4 * v.cs, attr_id=2)], 1)
    recs = {r.name: r for r in NtfsVolume(Source(img), 0).walk()}
    for name in ("x.bin", "y.bin"):
        assert recs[name].status == "lost" and not recs[name].extents, name
    from recover_anything.organize import plan_size
    assert plan_size(scan(img, include_live=True, workers=1).found) < 32 * 1024**2


def test_a_long_chain_of_parent_links_stays_fast(tmp_path):
    import time
    from recover_anything.fs import ntfs as N
    vol = N.NtfsVolume.__new__(N.NtfsVolume)
    recs = {}
    for i in range(16, 8016):                                     # each a folder inside the next
        r = N._Rec(i)
        r.is_dir, r.in_use = True, True
        r.names = [(1, "d%d" % i, i + 1, 0)]
        recs[i] = r
    t = time.time()
    for i in range(16, 8016):                                     # as walk() does, for every record
        vol._deleted_above(recs, recs[i])
    assert time.time() - t < 3.0                                  # bounded depth: about 0.2 s; unbounded: 10 s


def test_a_folder_record_reused_by_a_new_folder_is_not_the_old_folder(tmp_path):
    img = str(tmp_path / "ntfs.img")
    with NtfsCard(img, size=32 * 1024**2) as v:
        v.write_file("Wedding 2025/IMG_0001.JPG", b"\xff\xd8\xff\xe0" + os.urandom(20000))
        v.write_file("keep.txt", b"x" * 9000)
        v.delete("Wedding 2025/IMG_0001.JPG")
        num = v.by_path.pop("Wedding 2025")                       # the folder deleted ...
        v.records[num]["flags"] &= ~1
        v.records[num]["seq"] += 1
        v._write_record(num)
        v.mkdir("Tax returns")                                    # ... and its record reused
    (r,) = [r for r in NtfsVolume(Source(img), 0).walk() if r.name == "IMG_0001.JPG"]
    assert r.path != "Tax returns"
