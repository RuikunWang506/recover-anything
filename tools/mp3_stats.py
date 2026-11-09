"""How real MP3 encoders use Layer III's bit reservoir, read-only.

    find /Applications -iname '*.mp3' > /tmp/mp3s.txt
    python tools/mp3_stats.py /tmp/mp3s.txt

For every frame: does it break the rule a rebuild relies on (pointing further
back than the room the frames before left: "bad"), tie (point exactly there)
or leave a gap? And at a join, how often would a frame of ANOTHER recording
with the same sample rate tie there, for each rule on which tie values count
(reassemble_stream._Mp3.tied counts values in at most 1/32 of a song's frames).
"""
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from recover_anything.formats import more  # noqa: E402


def frames(d: bytes):
    pos = 10 + more._synchsafe(d[6:10]) + (10 if d[5] & 0x10 else 0) if d[:3] == b"ID3" else 0
    key = None
    while pos + 4 <= len(d):
        f = more.mp3_frame(d[pos:pos + 4])
        if f is None or (key is not None and f[1] != key):
            if key is None:  # some files have bytes before the first frame
                pos += 1
                continue
            return
        n, key = f
        yield key, more.mp3_side(d[pos:pos + 40], n)
        pos += n


def main(argv) -> int:
    paths = [p for p in open(argv[1]).read().split("\n") if p] if len(argv) > 1 else []
    if not paths:
        print(__doc__)
        return 1
    songs = []  # (key, [(room before, main_data_begin, verdict)], histogram)
    for path in paths:
        try:
            d = open(path, "rb").read()
        except OSError:
            continue
        res, rows, key = more.Reservoir(), [], None
        for key, side in frames(d):
            if side is None:
                continue
            room = res.room
            v = res.step(side)
            if v == "bad":
                res.room = None
            rows.append((room, side[0], v))
        if len(rows) >= 5:
            songs.append((key, rows, collections.Counter(m for _, m, _ in rows)))
    verdicts = collections.Counter(v for _, rows, _ in songs for _, _, v in rows)
    print("%d files, %d frames: %s" % (len(songs), sum(verdicts.values()), dict(verdicts)))
    print("files that never use the reservoir (every frame points at 0):",
          sum(1 for _, rows, h in songs if h[0] == len(rows)))
    by_key = collections.defaultdict(list)
    for s in songs:
        by_key[s[0]].append(s)
    for share in (1.0, 1 / 16, 1 / 32, 1 / 64):
        joins = proven = 0
        risk = []
        for group in by_key.values():
            for a in group:
                others = [b for b in group if b is not a]
                if not others:
                    continue
                total = sum(a[2].values())
                for room, mdb, v in a[1]:
                    if room is None:
                        continue
                    joins += 1
                    if v != "tie" or mdb == 0 or a[2][mdb] > share * total:
                        continue
                    proven += 1
                    # a frame of a random other recording, at a random place in it
                    risk.append(sum(b[2][mdb] / sum(b[2].values()) for b in others) / len(others))
        risk.sort()
        print("ties counted at values in at most %5.1f%% of a song's frames: %d%% of joins tied; "
              "another recording's frame ties there %.2f%% of the time (99th percentile %.1f%%, worst %.0f%%)" % (
                  100 * share, 100 * proven // max(1, joins), 100 * sum(risk) / max(1, len(risk)),
                  100 * risk[int(len(risk) * 0.99)] if risk else 0, 100 * risk[-1] if risk else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
