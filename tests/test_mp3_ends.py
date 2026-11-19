"""Where an MP3 ends. It has no end marker, so its end counts as proven only
when nothing past it could still be its own. Three ways that went wrong on
cards holding only music, each a file marked recovered but cut short or
running into another song:

- seeds 47 and 50: the last frame's header fit in its cluster but its side
  information ran into the next one, another file's; the reservoir check
  failed on those bytes, and the stop counted as "ends inside a cluster";
- seed 45: the sound ended exactly at a cluster boundary, where its Info
  header said; the song's 128-byte ID3v1 tag was in a piece not found;
- seed 46: a rebuilt song ran on from its own clusters into the next song's
  (no tag, no Info frame there to say so), and stopped inside a cluster
  where a frame of a third recording failed the reservoir check: a frame
  of the same kind right there says nothing about where this one ends;
- seed 27: from cluster 30 on, a song carved whole was another song with no
  tag or Info frame, starting at that cluster; the first song's frame had
  ended exactly there, so the two lined up, and the other song's frames ran
  on to the very length the first one's Info header gave."""
import hashlib

import pytest

from recover_anything.formats import more
from recover_anything.simcard.files import make_mp3


@pytest.mark.parametrize("seed", [27, 45, 46, 47, 50])
def test_music_cards_mark_nothing_wrong_as_recovered(tmp_path, seed):
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    sc = scenarios.single_kind(str(tmp_path), "mp3", seed)
    res = scan(sc.image, workers=2)  # (as tools/eval_cards.py, which found them)
    ever = sc.groups.get("ever written", set(sc.truth))
    wrong = [(r.origin, r.size, r.note) for r in res.found if r.status == "ok" and r.extents
             and hashlib.sha256(r.read(res._src)).hexdigest() not in ever]
    res._src.close()
    assert wrong == []


def test_another_songs_info_frame_ends_a_recording():
    """An encoder writes a Xing or Info header only as a song's first frame.
    Its side information is blank, so the reservoir check lets it through."""
    a, b = make_mp3(1, xing=False), make_mp3(2, id3=False, xing=True)
    data = a + b
    m = more.measure_mp3(lambda o, n: data[o:o + n], 0, len(data))
    assert m.end == len(a)


def test_sound_that_ends_at_a_cluster_boundary_may_lack_its_tag():
    song = make_mp3(3, xing=True, id3v1=False)
    read = lambda o, n: song[o:o + n]  # noqa: E731
    ph = -len(song) % 4096                         # the song ends exactly where a cluster does
    assert more.validate_mp3(read, len(song), False, grid=(ph, 4096))[0] == "unverified"
    assert more.validate_mp3(read, len(song), False, grid=(ph + 512, 4096))[0] == "ok"


def test_a_song_with_no_tag_may_start_at_a_cluster_boundary_inside_another():
    """Its first frame points 0 bytes back, like any song's first frame."""
    a, b = make_mp3(3, xing=True), make_mp3(5, id3=False, xing=False)  # (a uses its reservoir)
    pos, frames = 10 + more._synchsafe(a[6:10]), []
    while pos + 4 <= len(a):
        frames.append(pos)
        pos += more.mp3_frame(a[pos:pos + 4])[0]
    cut = next(p for p in frames[len(frames) // 2:] if more.mp3_side(a[p:p + 40], 417)[0] != 0)
    cs = 4096
    ph = -cut % cs                                     # that frame starts at a cluster boundary
    whole = lambda d: (lambda o, n: d[o:o + n])  # noqa: E731
    assert not more.mp3_unmarked_start(whole(a), len(a), ph, cs)
    spliced = a[:cut] + b                              # the file broke there, and another song begins
    assert more.mp3_unmarked_start(whole(spliced), len(spliced), ph, cs)
