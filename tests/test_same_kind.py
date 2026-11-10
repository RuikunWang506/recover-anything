"""Cards that hold one kind of file: a dashcam, a camcorder, a music stick, a
voice recorder. Files of the same kind fragment into each other's holes, and
a piece of another recording passes the checks a piece of junk fails. Only
something that ties the data to the recording itself (a checksum, a clock)
may let such a file be called recovered."""
import pytest

from conftest import recovered_hashes
from recover_anything.formats import more
from recover_anything.simcard import files as F


def _rd(d):
    return lambda o, n: d[o:o + n]


def _shift_clocks(d: bytes, m2ts: bool, by: int) -> bytes:
    """The same recording with its clocks started elsewhere: what another clip
    from the same camera looks like, packet for packet."""
    stride, off = (192, 4) if m2ts else (188, 0)
    out = bytearray(d)
    for q in range(0, len(d) - stride + 1, stride):
        if m2ts:
            ats = (int.from_bytes(d[q:q + 4], "big") + by) & 0x3FFFFFFF
            out[q:q + 4] = ats.to_bytes(4, "big")
        h = d[q + off:q + off + 12]
        if h[3] & 0x20 and h[4] >= 7 and h[5] & 0x10:
            v = int.from_bytes(h[6:12], "big")
            base = ((v >> 15) + by) & ((1 << 33) - 1)
            out[q + off + 6:q + off + 12] = ((base << 15) | (v & 0x7FFF)).to_bytes(6, "big")
    return bytes(out)


@pytest.mark.parametrize("m2ts", [False, True])
def test_another_clips_cluster_is_caught_by_the_clock(m2ts):
    """Regression: a cluster of another clip at the same place in its file has
    the same packet counters, so it passed. Its clock doesn't carry on."""
    a = F.make_ts(41, packets=3000, m2ts=m2ts)
    b = _shift_clocks(a, m2ts, 123_456_789)
    k = 60  # a cluster in the middle, with clock packets after it
    spliced = a[:k * 4096] + b[k * 4096:(k + 1) * 4096] + a[(k + 1) * 4096:]
    # layout_known: rule out the other reasons for doubt, so only the clock decides
    assert more.validate_ts(_rd(a), len(a), True, layout_known=True)[0] == "ok"
    assert more.validate_ts(_rd(spliced), len(spliced), True, layout_known=True)[0] != "ok"


@pytest.mark.parametrize("kind,seed", [("mts", 1), ("ts", 1), ("mp3", 1), ("mp4", 2), ("jpg", 3), ("jpg", 4),
                                       ("pdf", 2), ("wav", 2), ("raw", 2), ("gif", 1)])
def test_a_card_of_one_kind_marks_nothing_wrong(workdir, kind, seed):
    """Regression: on cards holding only dashcam clips, camcorder clips or
    songs, rebuilds stitched in pieces of other recordings and called the
    result recovered (13, 30 and 9 such files on four cards each). On a card
    of camera videos, a carved MP4 whose index listed nothing checkable was
    called recovered because its boxes lined up. On a card of photos, a run of
    another photo's clusters decoded cleanly inside a rebuilt photo (its
    thumbnail shows the difference); on another (jpg 4), a rebuilt photo's last
    pieces were other photos' clusters covering only the plain band at its
    bottom, where nothing tells them apart. PDFs, recordings and RAW files from a
    document stick, a voice recorder and a DSLR were called recovered on their
    structure alone."""
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    sc = scenarios.single_kind(workdir, kind, seed)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    assert set(got) <= sc.groups["ever written"]
    # a rebuilt song can't prove its pieces: never more than "check that it plays"
    if kind == "mp3":
        assert not [r for r in res.found if r.status == "ok" and r.meta.get("reassembled_from", 1) > 1]
