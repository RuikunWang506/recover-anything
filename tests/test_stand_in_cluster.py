"""Another deleted file's cluster standing in for one a video lost.

Inside a video frame's data nothing is checked, so a rebuild places a cluster
there only by counting: whatever free cluster comes next fits. When a newer
file (still on the card) took one of a deleted video's clusters inside a
frame, the rebuild skips that file, and another deleted file's cluster right
before or after the jump fills the place. Every check still passed and the
video was marked recovered with another file's data in it: MP4 always
("every frame position checked"), MKV/WebM when the stand-in came before the
jump (only the cluster after one had to hold a checked header). A jump now
counts only where something is checked in the clusters on both sides of it;
otherwise the video is saved as "check that it plays"."""
import hashlib
import os
import struct

import pytest

from recover_anything import reassemble_stream as RS
from recover_anything.formats import others
from recover_anything.simcard.fat32 import Fat32Card
from recover_anything.simcard.files import make_mkv
from recover_anything.simcard.media import make_video

CS = 4096


def _mkv_checks(v):
    w = RS._Mkv(lambda o, n: v[o:o + n])
    w.grain, w.audit = CS, []
    w.advance(len(v))
    return w.audit


def _mp4_checks(v):
    for typ, body, end in others._boxes(lambda o, n: v[o:o + n], 0, len(v)):
        if typ == b"mdat":
            p, out = body, []
            while p < end:
                out.append(p)
                p += 4 + struct.unpack(">I", v[p:p + 4])[0]
            return out


def _video(kind, seed):
    """(video, a cluster of another one's frame data, the video's cluster to lose)"""
    if kind == "mkv":
        a, b, checks = make_mkv(seed, webm=True), make_mkv(seed + 100, webm=True), _mkv_checks
    else:
        a, b, checks = make_video(seed), make_video(seed + 100), _mp4_checks
    at = checks(a)
    inside = [at[i + 1] // CS - 1 for i in range(len(at) - 1) if at[i + 1] // CS - at[i] // CS >= 3]
    return a, b[20 * CS:21 * CS], inside[len(inside) // 2]  # (a cluster wholly inside a frame)


def _card(path, video, other, p, jump_before):
    """The video, deleted; its cluster p, inside a frame, is now a newer file's
    (still on the card). Another deleted file's cluster comes right before that
    one, right after the video's piece (jump_before=False) or after a cluster
    still in use (True). Returns the hashes of everything ever written."""
    written = {video, other}
    with Fat32Card(path, size=300 * 1024**2, cluster_size=CS) as card:
        def put(data):  # written and then deleted: no folder entry left
            cl = card._alloc((len(data) + CS - 1) // CS)
            for i, c in enumerate(cl):
                card._pwrite(card.cluster_offset(c), data[i * CS:(i + 1) * CS])
            return cl

        def free(cl):
            for c in cl:
                card.fat[c] = 0
            card._flush_fat(cl)

        hold = card._alloc(p)
        if jump_before:
            g = os.urandom(CS)
            written.add(g)
            card.write_file("g.bin", g)
        oc = put(other)
        free(hold)
        vc = put(video)                                 # the video goes around it
        assert vc[p] == oc[0] + 1
        free(oc + vc)
        hold = card._alloc(p + 1)                       # everything free before the video's cluster p
        newer = b"a newer file " * 315
        written.add(newer)
        assert card.write_file("newer.txt", newer) == [vc[p]]
        free(hold)
    return {hashlib.sha256(d).hexdigest() for d in written}


@pytest.mark.parametrize("jump_before", [False, True])
@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("kind", ["mkv", "isobmff"])
def test_a_lost_clusters_stand_in_is_not_recovered(tmp_path, kind, seed, jump_before):
    from recover_anything.pipeline import scan
    img = str(tmp_path / "c.img")
    video, other, p = _video(kind, seed)
    written = _card(img, video, other, p, jump_before)
    res = scan(img, workers=1)
    rebuilt = [r for r in res.found if r.kind == kind and r.meta.get("reassembled_from")]
    assert rebuilt and all(r.status == "unverified" for r in rebuilt)   # saved, "check that it plays"
    assert not [r for r in res.found if r.status == "ok" and r.extents
                and hashlib.sha256(r.read(res._src)).hexdigest() not in written]
    res._src.close()
