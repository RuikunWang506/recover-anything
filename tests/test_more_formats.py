"""Music, voice memos, dashcam and camcorder clips, screen recordings, phone
databases, compressed logs and scans: found, checked honestly, and filed in
the right folder."""
import os

import pytest

from conftest import SMALL, recovered_hashes, sha
from recover_anything.formats import more
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card

FILES = {
    "Music/song.mp3": lambda: F.make_mp3(1),
    "Music/live.flac": lambda: F.make_flac(1),
    "Recordings/memo.opus": lambda: F.make_ogg(1),
    "DashCam/REC0001.TS": lambda: F.make_ts(1),
    "AVCHD/BDMV/STREAM/00000.MTS": lambda: F.make_ts(2, m2ts=True),
    "Videos/clip.mkv": lambda: F.make_mkv(2),
    "Backup/sms.sqlite": lambda: F.make_sqlite(1),
    "Backup/logs.gz": lambda: F.make_gzip(1, lines=40000),
}


def _rd(d):
    return lambda o, n: d[o:o + n]


@pytest.mark.parametrize("mode", ["deleted", "formatted"])
def test_every_new_type_comes_back_exact_with_its_name(workdir, mode):
    data = {k: f() for k, f in FILES.items()}
    img = os.path.join(workdir, "mixed.img")
    with Fat32Card(img, **SMALL) as card:
        for k, v in data.items():
            card.write_file(k, v)
        if mode == "deleted":
            for k in data:
                card.delete(k)
        else:
            card.format()
    got = recovered_hashes(scan(img, workers=1))
    for path, v in data.items():
        assert sha(v) in got, path
        name = got[sha(v)].name
        assert name and name[1:] == path.rsplit("/", 1)[1][1:]  # a deleted 8.3 name loses its first letter


def test_carved_without_names(workdir):
    """No folder entries at all (camera format recreated the folders)."""
    data = {k: f() for k, f in FILES.items()}
    img = os.path.join(workdir, "carve.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("filler.bin", b"\x00" * 65536)  # the recreated folders land here
        for k, v in data.items():
            card.write_file(k, v)
        card.format(recreate_dirs=["Music", "Backup", "DashCam", "Videos", "Recordings", "AVCHD"])
    got = recovered_hashes(scan(img, workers=1))
    for path, v in data.items():
        assert sha(v) in got, path


def test_gzip_whose_end_meets_the_output_step():
    """Regression: when inflating stopped at the 1 MB output step exactly as the
    stream ended, the end was placed 8 bytes early and the checksum failed."""
    d = F.make_gzip(10000, lines=30000)
    m = more.measure_gzip(_rd(d), 0, len(d) + 4096)
    assert m.complete and m.end == len(d)


def test_spliced_cluster_is_caught():
    """Another file's data in the middle must not pass as part of the file."""
    for make in (F.make_flac, F.make_ogg, F.make_mp3, F.make_ts, F.make_mkv):
        d = make(5)
        other = make(6)
        k = len(d) // 4096 // 2
        assert k >= 3
        bad = d[:k * 4096] + other[k * 4096:(k + 1) * 4096] + d[(k + 1) * 4096:]
        kind = {F.make_flac: "flac", F.make_ogg: "ogg", F.make_mp3: "mp3", F.make_ts: "ts", F.make_mkv: "mkv"}[make]
        st = more.KINDS[kind][2](_rd(bad), len(bad), True)[0]
        assert st != "ok", kind


def test_no_checksum_types_are_not_called_recovered_when_deleted():
    """BMP pictures and VP9 WebM video have no way to prove their contents."""
    for d, fn in ((F.make_bmp(1), more.validate_bmp), (F.make_mkv(1, webm=True), more.validate_mkv)):
        assert fn(_rd(d), len(d), True)[0] == "unverified"
        assert fn(_rd(d), len(d), True, layout_known=True)[0] == "ok"


def test_a_stream_end_is_proven_only_from_the_last_checked_header():
    """Regression: an MP3 whose last frame ran across a cluster boundary into
    another file's cluster was called complete because the bytes after it
    were in the same (foreign) cluster."""
    d = F.make_mp3(7, id3=True, xing=False)
    m = more.measure_mp3(_rd(d), 0, len(d))
    cut = m.last + 100  # inside the last frame
    phase = 4096 - (m.last + 50) % 4096  # a cluster boundary falls inside that frame
    st = more.validate_mp3(_rd(d[:cut]), cut, False, after=b"\0" * 8, grid=(phase, 4096))
    assert st[0] != "ok"


def test_the_middle_of_a_recording_is_not_a_whole_file():
    """Regression: a frame found mid-song at a sector boundary was carved and
    called recovered. Without a tag or a length header nothing marks a start."""
    d = F.make_mp3(8, id3=False, xing=False, id3v1=True)
    m = more.measure_mp3(_rd(d), 0, len(d))
    assert m.complete  # it ends in a TAG...
    assert more.validate_mp3(_rd(d), len(d), False)[0] == "unverified"  # ...but its start isn't marked


def test_a_truncated_matroska_is_not_complete():
    """Regression: a segment longer than the data read was clipped and looked complete."""
    d = F.make_mkv(3)
    cut = len(d) * 3 // 4
    assert not more.measure_mkv(_rd(d[:cut]), 0, cut).complete


def test_recordings_go_to_the_audio_folder(workdir):
    """MP3, FLAC, Ogg and WAV have their own folder, and --only audio picks them."""
    from recover_anything.carve import group_of
    from recover_anything.organize import write_all
    for kind, ext in (("mp3", ".mp3"), ("flac", ".flac"), ("ogg", ".opus"), ("riff", ".wav")):
        assert group_of(kind, ext) == "audio"
    assert group_of("ogg", ".ogv") == "video"
    img = os.path.join(workdir, "audio.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("Music/a.mp3", F.make_mp3(1))
        card.write_file("Backup/b.gz", F.make_gzip(1))
        card.format()
    res = scan(img, workers=1, kinds={"audio"})
    assert [r.kind for r in res.found] == ["mp3"]
    out = os.path.join(workdir, "out")
    write_all(scan(img, workers=1), out)
    assert os.listdir(os.path.join(out, "Audio")) == ["a.mp3"]


@pytest.mark.parametrize("seed", [2, 9])
def test_mixed_device_card_marks_nothing_wrong(workdir, seed):
    """A card shared by many devices, fragmented and formatted: every file
    called recovered is byte for byte one that was really written."""
    from recover_anything.simcard import scenarios
    sc = scenarios.mixed_device(workdir, seed, size=320 * 1024 * 1024)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    assert set(got) <= sc.groups["ever written"]
    assert len(set(got) & set(sc.truth)) >= len(sc.truth) // 2


def test_a_bare_mp3_whose_end_cant_be_proven_is_saved_but_not_called_recovered(workdir):
    """No tag at the start and no length header: its last frame runs across a
    cluster boundary, so the bytes after that boundary can't be proven its own."""
    bare = F.make_mp3(2, id3=False, xing=False, id3v1=True)
    img = os.path.join(workdir, "bare.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("Music/bare.mp3", bare)
        card.delete("Music/bare.mp3")
    res = scan(img, workers=1)
    mine = [r for r in res.found if r.extents and sha(r.read(res._src)) == sha(bare)]
    assert mine and mine[0].status == "unverified" and mine[0].name in ("_ARE.MP3", "bare.mp3")
    # and no second, partial copy carved from the middle of the same song
    assert [r for r in res.found if r.kind == "mp3"] == mine


def test_a_newer_heic_in_an_old_videos_space_is_not_that_video(workdir):
    """Regression: an old deleted MP4's folder entry pointed at clusters a newer
    HEIC photo now uses. Both are ISO BMFF, so the HEIC was checked by
    structure under the video's name and called recovered."""
    from recover_anything.simcard.media import make_video
    img = os.path.join(workdir, "clash.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("DCIM/MVI_0001.MP4", make_video(1, frames=80))
        card.delete("DCIM/MVI_0001.MP4")
        heic = F.make_heic(3, size=1_200_000)
        card.write_file("DCIM/IMG_0002.HEIC", heic)
        card.format()
    res = scan(img, workers=1)
    old = [r for r in res.found if r.name == "MVI_0001.MP4"]
    assert old and old[0].status == "lost"
    # (the photo itself is checked tile by tile now: recovered, but only as itself)
    oks = [r for r in res.found if r.status == "ok" and r.kind == "isobmff"]
    assert oks and all(r.ext == ".heic" and r.name != "MVI_0001.MP4" and r.read(res._src) == heic for r in oks)


def test_fragmented_recordings_and_archives_are_rebuilt(workdir):
    """Delete every other file, then save bigger ones that fill the holes (so
    each is split), then camera-format: no names, no chains. Checksummed
    types must come back whole from their pieces."""
    img = os.path.join(workdir, "frag.img")
    makers = [lambda i: F.make_flac(i, seconds=3), lambda i: F.make_gzip(i, lines=9000),
              lambda i: F.make_ogg(i, seconds=40), lambda i: F.make_mp3(i, seconds=12)]
    keep = {}
    with Fat32Card(img, **SMALL) as card:
        card.write_file("filler.bin", b"\0" * 65536)
        for i in range(12):
            card.write_file("A/f%02d.bin" % i, F.make_bmp(i, width=200, height=100))
        for i in range(0, 12, 2):
            card.delete("A/f%02d.bin" % i)
        for i in range(8):
            d = makers[i % 4](100 + i)
            cl = card.write_file("B/g%02d" % i, d)
            if any(b != a + 1 for a, b in zip(cl, cl[1:])):
                keep[sha(d)] = i % 4
        card.format(recreate_dirs=["A", "B"])
    assert {0, 1, 2} <= set(keep.values()), "test setup should fragment a FLAC, a gzip and an Ogg"
    res = scan(img, workers=1)
    got = recovered_hashes(res)
    for h, k in keep.items():
        if k in (1, 3):
            # gzip: garbage often decodes as "copy earlier text", so only the final checksum
            # catches it and can't say where; an MP3 may honestly stay "check that it plays"
            continue
        assert h in got, "kind %d not rebuilt" % k
        assert got[h].meta.get("reassembled_from", 1) > 1


def test_a_walker_rewinds_to_exactly_where_it_was():
    """Regression: a snapshot shared the walker's set of open Ogg streams, so
    rewinding after a failed trial started from a later state."""
    from recover_anything.reassemble_stream import _Ogg, _Ts
    for d, W in ((F.make_ogg(4), _Ogg), (F.make_ts(4), _Ts)):
        w = W(lambda o, n, d=d: d[o:o + n])
        snap = w.snapshot()
        w.advance(len(d) // 2)
        w.restore(snap)
        fresh = W(lambda o, n, d=d: d[o:o + n])
        assert w.advance(len(d)) == fresh.advance(len(d)) and w.pos == fresh.pos


def test_a_newer_clip_in_a_deleted_clips_space_is_not_that_clip(workdir):
    """Regression: a deleted clip's folder entry pointed at clusters a newer,
    longer clip now uses. Its first part is a perfectly good stream, so it
    passed as the old clip. The packets run on past the entry's size inside
    the same cluster, which a whole old clip can't do."""
    img = os.path.join(workdir, "reuse.img")
    old, new = F.make_ts(30, packets=2000, m2ts=True), F.make_ts(31, packets=3500, m2ts=True)
    with Fat32Card(img, **SMALL) as card:
        card.write_file("AVCHD/00005.MTS", old)
        card.delete("AVCHD/00005.MTS")
        card.write_file("AVCHD/00031.MTS", new)
        card.format()
    assert (len(old) % 4096) not in (0,) and 4096 - len(old) % 4096 >= 16
    res = scan(img, workers=1)
    r = [r for r in res.found if (r.name or "").endswith("0005.MTS")]
    assert r and r[0].status != "ok"
    assert sha(new) in recovered_hashes(res)
