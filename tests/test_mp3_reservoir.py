"""MP3's bit reservoir: each frame's data begins where the frames before left
room, so a frame of another recording often can't follow (it points further
back than that room). A frame that points exactly at the room is tied to the
ones before, but frames of any recording point at 0 and at an encoder's
ceiling, so a join counts only at a value the recording seldom points at,
and only where nothing else could have come next."""
import os
import random

import pytest

from conftest import SMALL, recovered_hashes, sha
from recover_anything import reassemble_stream as RS
from recover_anything.formats import more
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card


def _frames(d):
    """(offset, length, (main_data_begin, bits, room)) of each frame."""
    pos = 10 + more._synchsafe(d[6:10]) if d[:3] == b"ID3" else 0
    while pos + 4 <= len(d):
        f = more.mp3_frame(d[pos:pos + 4])
        if f is None:
            return
        yield pos, f[0], more.mp3_side(d[pos:pos + 40], f[0])
        pos += f[0]


def test_reservoir_rules():
    r = more.Reservoir()
    assert r.step((0, 300 * 8, 382)) == "start" and r.room == 82
    assert r.step((82, 400 * 8, 382)) == "tie"  # begins exactly where the frame before ended
    assert r.step((60, 100 * 8, 382)) == "gap"  # legal (bytes left unused), but no proof
    room = r.room
    assert r.step((room + 1, 8, 382)) == "bad"  # further back than the room left
    assert r.room == room
    assert r.step((0, 383 * 8, 382)) == "bad"  # more data than fits before its own end


def test_side_information_of_an_mpeg2_mono_frame():
    """Voice recorders write MPEG-2 mono: 8-bit pointer, one granule."""
    hdr = bytes([0xFF, 0xF3, 0x90, 0xC0])  # MPEG-2 layer III, 64 kbps, 22.05 kHz, mono, no CRC
    n = more.mp3_frame(hdr)[0]
    val = (100 << 64) | (1234 << 51)
    assert more.mp3_side(hdr + val.to_bytes(9, "big"), n) == (100, 1234, n - 4 - 9)


def test_simulated_songs_never_break_the_rules():
    for seed in range(1, 9):
        r = more.Reservoir()
        assert all(r.step(s) != "bad" for _, _, s in _frames(F.make_mp3(seed, seconds=10)))


def test_a_frame_of_another_recording_ends_a_carved_song():
    """Regression: a carved song ran on through another recording's frames, so
    the two came back as one."""
    a = F.make_mp3(3, seconds=8, id3=False, xing=False)
    r = more.Reservoir()
    fr = list(_frames(a))
    for _, _, s in fr[:120]:
        r.step(s)
    cut = fr[120][0]
    for seed in range(20, 40):  # a song whose frames point further back than that room somewhere
        b = F.make_mp3(seed, seconds=8, id3=False, xing=False)
        at = next((o for o, _, s in list(_frames(b))[50:] if s[0] > r.room), None)
        if at is not None:
            break
    data = a[:cut] + b[at:]
    res = more.measure_mp3(lambda o, n: data[o:o + n], 0, len(data))
    assert res.end == cut and not res.complete, (res.end, cut, res.reason)
    assert res.reason == "a frame of another recording follows here"


def test_the_walker_runs_outside_a_rebuild():
    """Regression: the walker needed the cluster size only a rebuild gives it,
    and the error stopped every rebuild after it."""
    d = F.make_mp3(2, seconds=5)
    w = RS._Mp3(lambda o, n: d[o:o + n])
    assert w.advance(len(d)) == "need"  # past its last frame: is a tag next?
    assert w.tail and w.last == list(_frames(d))[-1][0]


def _deleted_split(workdir, song, between=None):
    """A song deleted by accident: it filled a hole and went on past a song
    still on the card (and past `between`, deleted too, if given)."""
    img = os.path.join(workdir, "music.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("MUSIC/01.mp3", F.make_mp3(101, seconds=6))
        card.write_file("hole.bin", random.Random(1).randbytes(4096 * 20))
        card.write_file("MUSIC/03.mp3", F.make_mp3(203, seconds=8))
        if between:
            card.write_file("junk.bin", between)
        card.delete("hole.bin")
        cl = card.write_file("MUSIC/02.mp3", song)
        if between:
            card.delete("junk.bin")
        card.delete("MUSIC/02.mp3")
    assert any(b != a + 1 for a, b in zip(cl, cl[1:]))
    return scan(img, workers=1)


def _found(res, data):
    src = res._src
    return [r for r in res.found if sha(r.read(src)) == sha(data)]


def test_a_deleted_split_song_tied_at_a_rare_value_is_recovered(workdir):
    song = F.make_mp3(8, seconds=25)
    res = _deleted_split(workdir, song)
    assert sha(song) in recovered_hashes(res)


def test_a_join_another_cluster_could_have_filled_is_not_proof(workdir):
    """The same song, but a deleted file lies between its pieces: the search
    chose the second piece among clusters that also fit, so the tie alone
    isn't proof. Saved byte for byte, labeled for checking."""
    song = F.make_mp3(8, seconds=25)
    res = _deleted_split(workdir, song, between=random.Random(5).randbytes(4096 * 6))
    found = _found(res, song)
    assert found and all(r.status == "unverified" for r in found)


@pytest.mark.parametrize("seed", [2, 5, 12, 15])
def test_a_tie_at_a_common_value_is_not_proof(workdir, seed):
    """Ties at 0 (seeds 2, 5 and 12) or at the encoder's ceiling, 511 (seed
    15): frames of any recording point there."""
    song = F.make_mp3(seed, seconds=25)
    res = _deleted_split(workdir, song)
    found = _found(res, song)
    assert found and all(r.status == "unverified" for r in found)


def test_a_split_song_keeps_its_end_tag(workdir):
    """Regression: when the tag at a song's end ran into a cluster of zeros,
    the rebuild stopped short of it."""
    song = F.make_mp3(8, seconds=25, id3v1=True)
    assert len(song) % 4096 < 128  # the tag's last bytes sit alone in the last cluster
    res = _deleted_split(workdir, song)
    assert _found(res, song)


def test_a_rebuilt_song_after_a_format_keeps_its_end_tag(workdir):
    """Regression: a rebuilt song without a length stopped at its ID3v1 tag and
    was called recovered 128 bytes short."""
    img = os.path.join(workdir, "fmt.img")
    song = F.make_mp3(14, seconds=20, xing=False, id3v1=True)
    with Fat32Card(img, **SMALL) as card:
        card.write_file("MUSIC/01.mp3", F.make_mp3(101, seconds=6))
        card.write_file("hole.bin", random.Random(1).randbytes(4096 * 20))
        card.write_file("MUSIC/03.mp3", F.make_mp3(203, seconds=8))
        card.delete("hole.bin")
        card.write_file("02.mp3", song)  # (in the root folder, which the format wipes: no size known)
        card.format()
    res = scan(img, workers=1)
    assert _found(res, song)
    assert all(r.size != len(song) - 128 or r.status != "ok" for r in res.found if r.ext == ".mp3")


def test_one_files_trouble_never_stops_the_others_rebuilds(workdir, monkeypatch):
    img = os.path.join(workdir, "two.img")
    one, two = F.make_flac(1, seconds=2), F.make_flac(2, seconds=2)  # 43 clusters each
    with Fat32Card(img, **SMALL) as card:
        card.write_file("a.bin", random.Random(1).randbytes(30000))
        card.write_file("hole1.bin", random.Random(2).randbytes(4096 * 30))
        card.write_file("b.bin", random.Random(3).randbytes(30000))
        card.write_file("hole2.bin", random.Random(4).randbytes(4096 * 30))
        card.write_file("c.bin", random.Random(5).randbytes(30000))
        card.delete("hole1.bin")
        card.delete("hole2.bin")
        cl1 = card.write_file("one.flac", one)
        cl2 = card.write_file("two.flac", two)
        card.format()
    assert any(b != a + 1 for a, b in zip(cl1, cl1[1:])) and any(b != a + 1 for a, b in zip(cl2, cl2[1:]))
    calls = []
    real = RS._settle

    def settle(*args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("trouble with one file")
        return real(*args)

    monkeypatch.setattr(RS, "_settle", settle)
    got = recovered_hashes(scan(img, workers=1))
    assert len(calls) >= 2
    assert sha(one) in got or sha(two) in got
