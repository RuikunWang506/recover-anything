"""The report must not oversell. A rebuilt photo nothing could prove got the
green "rebuilt from 3 pieces" badge, and the headline counted every file that
couldn't be checked as recovered. Now the badge comes from the file's status
first, the headline counts only files whose check passed, and the files to
open are listed in the terminal, in recovery.json and in --json."""
import os
from types import SimpleNamespace

from recover_anything import report
from recover_anything.records import Found


def _rec(tmp_path, name, kind="jpeg", ext=".jpg", status="ok", check="full", note="every block decoded", **meta):
    path = tmp_path / name
    path.write_bytes(b"x")
    r = Found(kind=kind, ext=ext, extents=[(0, 1)], origin="filesystem", name=name, status=status, note=note)
    r.meta.update(written_to=str(path), check=check, **meta)
    return r


def _files(tmp_path):
    return [
        _rec(tmp_path, "good.jpg"),
        _rec(tmp_path, "rebuilt.jpg", reassembled_from=3),
        _rec(tmp_path, "unproven.jpg", status="unverified", check="full", reassembled_from=3,
             note="every block decodes, but it has no preview to compare with"),
        _rec(tmp_path, "song.mp3", kind="mp3", ext=".mp3", check="structure", note="every frame checked"),
        _rec(tmp_path, "scan.bmp", kind="bmp", ext=".bmp", status="unverified", check="structure",
             note="this file type has no checksums"),
        _rec(tmp_path, "half.jpg", status="partial", note="the rest was overwritten"),
    ]


def _res():
    return SimpleNamespace(found=[], warnings=[], size=1, source="card.img", seconds=1.0)


def test_headline_counts_only_files_whose_check_passed(tmp_path):
    s = report.summarize(_res(), _files(tmp_path))
    assert report.headline(s) == ("2 photos and 1 audio file recovered; "
                                  "2 more saved that you should open to make sure")
    assert s["reassembled"] == 1                      # the unproven rebuild isn't counted as rebuilt


def test_an_unproven_rebuild_gets_a_warning_badge_not_a_green_one(tmp_path):
    files = _files(tmp_path)
    page = open(report.write_html(_res(), files, str(tmp_path)), encoding="utf-8").read()
    assert page.count("rebuilt from 3 pieces</span>") == 1          # only the proven one
    assert page.count(">open it to check</span>") == 2
    assert "every frame checked</span>" in page                      # what was checked, not "structure checked"
    assert ">partial</span>" in page


def test_the_terminal_lists_the_files_to_open(tmp_path):
    files = _files(tmp_path)
    text = report.text_summary(_res(), files, str(tmp_path))
    assert "2 were saved but couldn't be proven whole" in text
    assert "unproven.jpg: every block decodes" in text and "scan.bmp: this file type" in text


def test_the_manifest_has_a_summary_and_each_files_standing(tmp_path):
    from recover_anything.organize import write_all
    src = tmp_path / "card.img"
    src.write_bytes(os.urandom(8192))
    recs = []
    for i, (st, chk) in enumerate((("ok", "full"), ("unverified", "structure"))):
        r = Found(kind="jpeg", ext=".jpg", extents=[(i * 4096, 4096)], origin="carved", status=st, note="n")
        r.meta["check"] = chk
        recs.append(r)
    from recover_anything.source import Source
    with Source(str(src)) as s:
        res = SimpleNamespace(found=recs, warnings=[], size=8192, source=str(src), image=None, volumes=[],
                              seconds=1.0, _src=s)
        w = write_all(res, str(tmp_path / "out"))
    m = w["manifest"]
    assert m["summary"]["recovered_and_checked"] == 1 and m["summary"]["saved_to_open_and_check"] == 1
    assert sorted((f["standing"], f["check"]) for f in m["files"]) == [("checked", "full"), ("open", "structure")]
