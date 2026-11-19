"""FLAC and AVI files marked recovered with another file's data at the end.

FLAC (seed 45): a frame doesn't store its length. Every frame but the last
is confirmed by its checksum and the next frame's header; after the last
one nothing follows, and the first place its checksum closed counted as the
end. Over a few kB of another file's frames it closes somewhere by chance
(1 byte in 65536). Its subframes give its exact length, which is all that
counts now. And a frame that runs past the stream's total length is another
file's: an encoder makes the last frame exactly as long as what's left.

AVI (seed 41): the index ('idx1') at the end was checked only where its
entries pointed at chunks; an entry pointing at none passed, so the start
of another video, read where the index went on elsewhere, passed too."""
import hashlib
import os
import struct

import pytest

from recover_anything.formats import checks, more
from recover_anything.simcard.files import make_flac

DATA = os.path.join(os.path.dirname(__file__), "data")


def _wrong(tmp_path, kind, seed):
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    sc = scenarios.single_kind(str(tmp_path), kind, seed)
    res = scan(sc.image, workers=2)  # (as tools/eval_cards.py, which found them)
    ever = sc.groups.get("ever written", set(sc.truth))
    wrong = [(r.origin, r.size, r.note) for r in res.found if r.status == "ok" and r.extents
             and hashlib.sha256(r.read(res._src)).hexdigest() not in ever]
    res._src.close()
    return wrong


@pytest.mark.parametrize("kind,seed", [("flac", 45), ("avi", 41)])
def test_cards_mark_nothing_wrong_as_recovered(tmp_path, kind, seed):
    assert _wrong(tmp_path, kind, seed) == []


def _frames(d):
    """(position, header) of every frame, and the STREAMINFO block."""
    pos, info = 4, None
    while True:
        n = int.from_bytes(d[pos + 1:pos + 4], "big")
        if d[pos] & 0x7F == 0:
            info = d[pos + 4:pos + 38]
        last, pos = d[pos] & 0x80, pos + 4 + n
        if last:
            break
    out = []
    while pos < len(d):
        hd = more._flac_header(d[pos:pos + 16])
        out.append((pos, hd))
        nxt = pos + 20
        while nxt < len(d):
            h = more._flac_header(d[nxt:nxt + 16])
            if d[nxt:nxt + 2] == b"\xff\xf8" and h and h[1] == hd[1] + 1:
                break
            nxt += 1
        pos = nxt
    return out, info


def test_frame_lengths_of_a_real_encoders_file():
    """macOS's own encoder (afconvert): LPC and fixed predictors, stereo."""
    d = open(os.path.join(DATA, "afconvert-stereo.flac"), "rb").read()
    frames, info = _frames(d)
    ends = [p for p, _ in frames[1:]] + [len(d)]
    assert len(frames) > 3
    for (p, hd), e in zip(frames, ends):
        assert more.flac_frame_len(d[p:], hd[2], hd[0], more.flac_bps(info)) == e - p


def test_the_last_frame_ends_where_its_subframes_do():
    d = open(os.path.join(DATA, "afconvert-stereo.flac"), "rb").read()
    m = more.measure_flac(lambda o, n: d[o:o + n], 0, len(d))
    assert m.complete and m.end == len(d)
    junk = d + bytes(range(256)) * 64                 # what follows on the card
    m = more.measure_flac(lambda o, n: junk[o:o + n], 0, len(junk))
    assert m.complete and m.end == len(d)
    frames, _ = _frames(d)
    cut = frames[-1][0] + 300                         # the last frame broken, other data after it
    rng = __import__("random").Random(5)
    for _ in range(40):
        bad = d[:cut] + bytes(rng.getrandbits(8) for _ in range(20000))
        assert not more.measure_flac(lambda o, n: bad[o:o + n], 0, len(bad)).complete


def test_a_frame_past_the_streams_length_is_another_files():
    a, b = make_flac(1, seconds=1), make_flac(2, seconds=2)   # a ends with a short frame
    fa, _ = _frames(a)
    fb, _ = _frames(b)
    k = len(fa) - 1
    assert fa[k][1][0] < fb[k][1][0]                   # the premise: b's frame k is a full one
    spliced = a[:fa[k][0]] + b[fb[k][0]:fb[k + 1][0]]
    m = more.measure_flac(lambda o, n: spliced[o:o + n], 0, len(spliced))
    assert not m.complete


def test_an_avi_index_entry_that_points_at_no_chunk():
    chunks = {104: (b"00dc", 500), 612: (b"01wb", 40)}
    movi = 100
    good = struct.pack("<4sIII", b"00dc", 16, 4, 500) + struct.pack("<4sIII", b"01wb", 0, 512, 40)
    assert checks.avi_index_bad(good, chunks, movi, True) is None
    other = b"RIFF\x10\x02\x00\x00AVI LIST\xc0\x00\x00\x00hdrlavih8\x00\x00\x00"   # another video's start
    assert checks.avi_index_bad(good[:16] + other[:16], chunks, movi, True) is not None
    assert checks.avi_index_bad(good[:16] + other[:16], chunks, movi, False) is None  # (not all walked)
