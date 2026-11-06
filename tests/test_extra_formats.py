"""Old Office documents, camcorder MPEG program streams, RAR and 7z archives,
Photoshop files and AMR voice memos: found with or without their names,
filed by what they are, and only called recovered when that can be proven."""
import os

import pytest

from conftest import SMALL, sha
from recover_anything.formats import extra
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card

FILES = {
    "Documents/report.doc": lambda: F.make_ole(1),
    "Documents/budget.xls": lambda: F.make_ole(2, "xls", 90000),
    "Documents/talk.ppt": lambda: F.make_ole(3, "ppt"),
    "MP_ROOT/MOV001.MOD": lambda: F.make_ps(1),
    "Backup/notes.rar": lambda: F.make_rar(1),
    "Backup/notes5.rar": lambda: F.make_rar(2, version=5),
    "Backup/backup.7z": lambda: F.make_7z(1),
    "Design/poster.psd": lambda: F.make_psd(1),
    "VOICE/memo.amr": lambda: F.make_amr(1),
}
EXT = {"report.doc": ".doc", "budget.xls": ".xls", "talk.ppt": ".ppt", "MOV001.MOD": ".mpg", "notes.rar": ".rar",
       "notes5.rar": ".rar", "backup.7z": ".7z", "poster.psd": ".psd", "memo.amr": ".amr"}


def _rd(d):
    return lambda o, n: d[o:o + n]


def _by_hash(res):
    return {sha(r.read(res._src)): r for r in res.found if r.extents and r.status in ("ok", "unverified")}


@pytest.mark.parametrize("mode", ["deleted", "carved"])
def test_every_new_type_comes_back_exact(workdir, mode):
    data = {k: f() for k, f in FILES.items()}
    img = os.path.join(workdir, "x.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("filler.bin", b"\0" * 65536)  # the recreated folders land here
        for k, v in data.items():
            card.write_file(k, v)
        if mode == "deleted":
            for k in data:
                card.delete(k)
        else:
            card.format(recreate_dirs=sorted({k.split("/")[0] for k in data}))
    got = _by_hash(scan(img, workers=1))
    for path, v in data.items():
        r = got.get(sha(v))
        assert r is not None, path
        assert r.ext == EXT[path.rsplit("/", 1)[1]] or (r.name or "").endswith(path[-6:]), (path, r.ext)
        # only a RAR of stored files can prove every byte: its files' checksums are checked
        if "notes.rar" in path:
            assert r.status == "ok" and r.meta.get("check") == "full"
        else:
            assert r.status == "unverified", (path, r.status, r.note)


def test_new_types_go_to_their_folders():
    from recover_anything.carve import group_of
    assert group_of("ole", ".doc") == "document"
    assert group_of("rar", ".rar") == "document"
    assert group_of("psd", ".psd") == "photo"
    assert group_of("amr", ".amr") == "audio"
    assert group_of("ps", ".mpg") == "video"


def test_a_damaged_office_file_is_caught():
    d = bytearray(F.make_ole(4))
    d[512:516] = b"\x07\x00\x00\x00"  # the allocation table's first entry no longer marks itself
    m = extra.measure_ole(_rd(bytes(d)), 0, len(d), check=True)
    assert not m.complete


def test_a_cluster_from_elsewhere_in_a_rar_is_caught():
    d, other = F.make_rar(5, 6), F.make_rar(6, 6)
    k = len(d) // 4096 // 2
    bad = d[:k * 4096] + other[k * 4096:(k + 1) * 4096] + d[(k + 1) * 4096:]
    assert extra.validate_rar(_rd(d), len(d), True)[0] == "ok"
    assert extra.validate_rar(_rd(bad), len(bad), True)[0] != "ok"


def test_a_voice_memo_ends_where_its_sound_ends():
    """A zero byte is a valid AMR frame header, so the blank rest of a memo's
    last cluster used to be counted as more (silent) frames of it."""
    memo = F.make_amr(2, seconds=3.0)
    data = memo + bytes(8192)
    assert extra.measure_amr(_rd(data), 0, len(data)).end == len(memo)
    # blank space after the last frame is not a frame that could follow
    st = extra.validate_amr(_rd(memo), len(memo), False, after=bytes(1024), grid=(0, 1 << 20), layout_known=True)
    assert st[0] == "ok", st


@pytest.mark.parametrize("make,validate", [(F.make_amr, extra.validate_amr), (F.make_ps, extra.validate_ps)])
def test_frames_that_look_alike_in_every_file_prove_nothing(make, validate):
    """Every AMR frame header of a voice memo is the same byte, and a program
    stream has a pack every 2 KB in every file: another recording's cluster
    would pass. So a deleted one is saved, never called recovered."""
    d = make(7)
    assert validate(_rd(d), len(d), True)[0] == "unverified"
    assert validate(_rd(d), len(d), True, layout_known=True)[0] == "ok"
