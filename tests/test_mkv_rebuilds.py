"""Split MKV and WebM videos on a card of WebM screen recordings (card 1).

A Matroska file's first cluster is often megabytes, so when it's split early,
all that measures whole from its start is its header: under 4 KB, which the
carver used to drop, so the rebuilder never saw the video (screen032.webm).
The header is kept for the rebuilder now, and dropped again if nothing more is
found (whatever its label: a 71-byte one was saved as "unverified").

Inside a frame's data many pieces pass, and the rebuilder took the first:
screen041.webm went 2873 -> 3717 -> 4170 through another file's clusters,
where the video goes 2877 -> 4170. It keeps the one with the fewest jumps now."""
import hashlib

import pytest


@pytest.fixture(scope="module")
def card(tmp_path_factory):
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    sc = scenarios.single_kind(str(tmp_path_factory.mktemp("webm")), "webm", 1)
    res = scan(sc.image, workers=2)
    saved = {hashlib.sha256(r.read(res._src)).hexdigest(): r for r in res.found if r.extents}
    yield sc, res, saved
    res._src.close()


def _wrong(res, saved, written):
    return [(r.origin, r.size, r.note) for h, r in saved.items() if r.status == "ok" and h not in written]


def test_a_video_split_inside_its_first_cluster_is_rebuilt(card):
    sc, res, saved = card
    back = [r for h, r in saved.items() if r.meta.get("head_only") and r.meta.get("reassembled_from")
            and h in sc.truth]
    assert back                                        # screen032.webm, byte for byte
    assert not [r for r in res.found if r.meta.get("head_only") and r.size < 4096]  # (no bare headers saved)
    assert _wrong(res, saved, sc.groups.get("ever written", set(sc.truth))) == []


def test_the_rebuild_with_the_fewest_jumps_is_kept(card):
    sc, res, saved = card
    h = next(h for h, p in sc.truth.items() if p.endswith("screen041.webm"))
    assert h in saved and saved[h].meta.get("reassembled_from") == 2
