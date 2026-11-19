"""A recording broken by fragmentation ends inside a frame (or packet) that
runs on into whatever follows: often the next recording, starting at that
cluster. Its header lay inside the first recording's measured span, so the
carver skipped it as "a frame in the middle of a recording already found",
and the whole next recording was lost. Only headers before the start of the
broken recording's last checked frame are skipped now."""
from recover_anything import carve
from recover_anything.simcard.files import make_mp3, make_ts
from recover_anything.source import Source


def _carve(path):
    with Source(path) as src:
        found, _ = carve.carve(src, 0, src.size, 512, 0, set(), carve.Claimed())
    return found


def _layout(tmp_path, a, b, kind):
    """a's first 32 KB, then b at that cluster, then the rest of a."""
    cut = 32768
    assert len(a) > cut + 8192
    img = tmp_path / ("%s.img" % kind)
    pad = bytes(-len(b) % 4096)
    img.write_bytes(a[:cut] + b + pad + a[cut:] + bytes(-len(a[cut:]) % 4096))
    return str(img), cut


def test_the_next_mp3_inside_a_broken_ones_last_frame_is_carved(tmp_path):
    a, b = make_mp3(1, seconds=20), make_mp3(2, seconds=20)
    path, cut = _layout(tmp_path, a, b, "mp3")
    starts = {r.start: r for r in _carve(path) if r.kind == "mp3"}
    assert 0 in starts
    assert starts[0].size > cut                     # the premise: the first runs on past the cut
    assert cut in starts, "the second recording should be carved where it starts"


def test_the_next_ts_inside_a_broken_ones_last_packet_is_carved(tmp_path):
    a, b = make_ts(1, packets=600), make_ts(2, packets=600)
    path, cut = _layout(tmp_path, a, b, "ts")
    starts = {r.start for r in _carve(path) if r.kind == "ts"}
    assert 0 in starts and cut in starts


# ---- a dashcam's recordings: nothing marked recovered may be wrong ---------
import hashlib  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize("seed", [43, 44])
def test_dashcam_cards_mark_nothing_wrong_as_recovered(tmp_path, seed):
    """Two ways one extra 188-byte packet passed as a recording's own:
    seed 43, the leftover packet of the deleted clip whose clusters the new
    one reuses (same start, so they line up; the walk stopped at the next
    leftover's clock jump, inside the cluster, which counted as the end);
    seed 44, a 0x47 byte of another file where a packet would start, with a
    PID the recording never had (a new PID has no counter to check)."""
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    sc = scenarios.single_kind(str(tmp_path), "ts", seed)
    res = scan(sc.image, workers=1)
    ever = sc.groups.get("ever written", set(sc.truth))
    wrong = [(r.origin, r.size, r.note) for r in res.found if r.status == "ok" and r.extents
             and hashlib.sha256(r.read(res._src)).hexdigest() not in ever]
    res._src.close()
    assert wrong == []
