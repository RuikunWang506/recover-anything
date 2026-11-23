"""A split MKV or WebM video whose first piece measures under 4 KB.

A Matroska file's first cluster is often megabytes, so when it's split early,
all that measures whole from its start is its header: under 4 KB, which the
carver used to drop, so the rebuilder never saw the video (WebM card 1:
screen032.webm). The header is kept for the rebuilder now, and dropped again
if nothing more is found (whatever its label: a 71-byte one was saved as
"unverified")."""
import hashlib


def _wrong(res, written):
    return [(r.origin, r.size, r.note) for r in res.found if r.status == "ok" and r.extents
            and hashlib.sha256(r.read(res._src)).hexdigest() not in written]


def test_a_video_split_inside_its_first_cluster_is_rebuilt(tmp_path):
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    sc = scenarios.single_kind(str(tmp_path), "webm", 1)
    res = scan(sc.image, workers=2)
    ever = sc.groups.get("ever written", set(sc.truth))
    back = [r for r in res.found if r.meta.get("head_only") and r.meta.get("reassembled_from")
            and hashlib.sha256(r.read(res._src)).hexdigest() in sc.truth]
    assert back                                        # screen032.webm, byte for byte
    assert not [r for r in res.found if r.meta.get("head_only") and r.size < 4096]  # (no bare headers saved)
    assert _wrong(res, ever) == []
    res._src.close()

