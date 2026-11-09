"""Split MKV and WebM videos are rebuilt element by element, every frame
checked by its codec. A join only counts when a checked header falls in the
cluster right after it, and where no other cluster could have come next."""
import os
import random

from conftest import SMALL, recovered_hashes, sha
from recover_anything import reassemble_stream as RS
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card


def test_the_walker_walks_a_whole_video():
    for seed, webm in ((1, True), (2, True), (3, False)):
        d = F.make_mkv(seed, clusters=3, webm=webm)
        w = RS._Mkv(lambda o, n: d[o:o + n])
        assert w.advance(len(d)) == "end" and w.end == len(d), seed


def test_a_frame_with_another_files_data_stops_the_walk():
    d = bytearray(F.make_mkv(4, clusters=3, webm=True))
    at = d.find(b"\xa3", 70000)  # (somewhere in the second half of the first cluster's frames)
    d[at:at + 4096] = random.Random(4).randbytes(4096)
    w = RS._Mkv(lambda o, n: bytes(d[o:o + n]))
    assert w.advance(len(d)) == "bad" and w.unit <= at


def _split(workdir, video, hole, between=None, name="clip.webm"):
    """A video deleted by accident: it filled a hole of `hole` clusters and went
    on past a file still on the card (and past `between`, deleted too)."""
    img = os.path.join(workdir, "v.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("a.bin", random.Random(1).randbytes(30000))
        card.write_file("hole.bin", random.Random(2).randbytes(4096 * hole))
        card.write_file("b.bin", random.Random(3).randbytes(30000))
        if between:
            card.write_file("junk.bin", between)
        card.delete("hole.bin")
        cl = card.write_file("Movies/" + name, video)
        if between:
            card.delete("junk.bin")
        card.delete("Movies/" + name)
    assert any(b != a + 1 for a, b in zip(cl, cl[1:]))
    return scan(img, workers=1)


def _found(res, data):
    src = res._src
    return [r for r in res.found if sha(r.read(src)) == sha(data)]


def test_a_split_webm_deleted_by_accident_is_rebuilt(workdir):
    video = F.make_mkv(5, clusters=3, webm=True)
    res = _split(workdir, video, hole=30)
    assert sha(video) in recovered_hashes(res)
    assert recovered_hashes(res)[sha(video)].meta.get("reassembled_from") == 2


def test_a_join_another_cluster_could_have_filled_is_not_proof(workdir):
    """A deleted file lies between the pieces, so the search chose the second
    piece among clusters that also fit: saved, labeled for checking. And its
    clusters, tried before the video's own last ones inside a frame's data
    (nothing checks there), mustn't stand in for them (regression: they did,
    until the search tried the place where the clusters stopped following on first)."""
    video = F.make_mkv(5, clusters=3, webm=True)
    res = _split(workdir, video, hole=30, between=random.Random(6).randbytes(4096 * 5))
    found = _found(res, video)
    assert found and all(r.status == "unverified" for r in found)


def test_a_join_inside_a_frames_data_is_not_proof(workdir):
    """The first piece ends inside a key frame: nothing after the join is
    checked until that frame ends, in a later cluster."""
    video = F.make_mkv(5, clusters=3, webm=True)
    res = _split(workdir, video, hole=4)
    found = _found(res, video)
    assert found and all(r.status == "unverified" for r in found)


def test_a_video_cut_short_is_not_called_recovered(workdir):
    """Regression: a rebuild that stopped inside a cluster called the video
    complete there, as for a stream that doesn't give its length; an MKV or
    WebM does, so stopping short of it is a break, not its end."""
    img = os.path.join(workdir, "cut.img")
    video = F.make_mkv(11, clusters=3, webm=True)
    with Fat32Card(img, **SMALL) as card:
        cl = card.write_file("clip.webm", video)
        card.format()
        for c in cl[len(cl) // 2:]:  # its second half reused by something else since
            card._pwrite(card.cluster_offset(c), random.Random(c).randbytes(card.cluster_size))
    res = scan(img, workers=1)
    vids = [r for r in res.found if r.kind == "mkv"]
    assert vids and all(r.status != "ok" for r in vids)
