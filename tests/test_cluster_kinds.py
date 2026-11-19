"""The video and stream rebuilders find the next cluster a file could go on in
with one search over a map of what each cluster holds, judged once per scan
(`reassemble_stream.ClusterKinds`). They used to step through free clusters
one at a time, judging each again for every file: 266 of the 284 seconds a
512 MB mixed card took. The answers must be the same as before."""
import random

from recover_anything import reassemble_stream as RS
from recover_anything.simcard.media import make_photo


def _old_judge(data: bytes) -> int:
    if data.count(0) == len(data):
        return RS.EMPTY
    ffs = data.count(b"\xff")
    ok = data.count(b"\xff\x00") + sum(data.count(bytes([0xFF, m])) for m in range(0xD0, 0xD8))
    return RS.PICTURE if ffs >= 6 and ok >= ffs - 1 else RS.OTHER


def test_the_judgement_is_the_old_one():
    rng = random.Random(1)
    blobs = [make_photo(i) for i in range(3)]
    samples = [b[i:i + 4096] for b in blobs for i in range(0, len(b) - 4096, 777)]
    samples += [bytes(rng.getrandbits(8) for _ in range(4096)) for _ in range(200)]
    samples += [bytes(4096), b"\xff\xff" * 2048, b"\x00" * 4095 + b"\xff",
                b"\xff\x00" * 6 + b"\xff\xff\x00" + b"\x11" * 4081, b"\xff\x00" * 6 + b"\x22" * 4083 + b"\xff"]
    kinds = [RS.ClusterKinds.judge(d) for d in samples]
    assert kinds == [_old_judge(d) for d in samples]
    assert {RS.EMPTY, RS.PICTURE, RS.OTHER} <= set(kinds)


class _Geom:
    first_cluster, cluster_size, data_offset = 2, 4096, 0
    last_cluster = 2 + 300 - 1

    def offset(self, c):
        return (c - 2) * 4096


class _Src:
    def __init__(self, data):
        self.data = data

    def read(self, off, n):
        return self.data[off:off + n]


def test_next_free_gives_what_walking_one_cluster_at_a_time_did():
    rng = random.Random(2)
    photo = make_photo(5)
    pics = [photo[i:i + 4096] for i in range(1024, len(photo) - 4096, 4096)]
    clusters = [rng.choice((bytes(4096), rng.choice(pics), bytes(rng.getrandbits(8) for _ in range(4096))))
                for _ in range(300)]
    src, g = _Src(b"".join(clusters)), _Geom()
    blocked = bytearray(rng.random() < 0.3 for _ in range(g.last_cluster + 1))
    kinds = RS.ClusterKinds(src, g)
    for any_cluster in (False, True):
        bad = (RS.EMPTY,) if any_cluster else (RS.EMPTY, RS.PICTURE)
        rb = RS._Rebuild.__new__(RS._Rebuild)
        rb.lastc, rb.any_cluster, rb.kinds = g.last_cluster, any_cluster, kinds
        from recover_anything.reassemble import _or
        rb.free = rb.free_soft = _or(blocked, kinds.unusable(any_cluster))
        for _ in range(200):
            c, taken = rng.randrange(2, g.last_cluster), set(rng.sample(range(2, g.last_cluster), 20))
            want, n = None, c
            while True:  # the old walk
                n = blocked.find(0, n + 1, g.last_cluster + 1)
                if n < 0:
                    break
                if n not in taken and RS.ClusterKinds.judge(clusters[n - 2]) not in bad:
                    want = n
                    break
            assert rb.next_free(c, taken) == want
