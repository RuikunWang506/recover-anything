"""Rebuild one randomized stress card, recover it, and explain every miss.

    python tools/debug_card.py 11            # seed, as in `recover-anything selftest --stress`
    python tools/debug_card.py 11 8192       # seed and cluster size
    python tools/debug_card.py 11 --explain IMG_0050.JPG --keep /tmp/card11

For each file that didn't come back byte for byte (or each file named with
--explain) it prints where the file really sat on the card (its cluster
runs), which file last wrote the clusters around every break, and what the
recovery produced at that spot. This is the loop that found and fixed the
last stress-card misses; see docs/HANDOFF.md.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time

from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.simcard.fat32 import Fat32Card


def _runs(clusters):
    out, start, prev = [], clusters[0], clusters[0]
    for c in clusters[1:]:
        if c != prev + 1:
            out.append((start, prev))
            start = c
        prev = c
    out.append((start, prev))
    return out


def build(seed: int, cluster_size: int | None, work: str):
    """The stress card for `seed`, plus {file name: clusters} for every file ever written."""
    i = seed - 6  # selftest --stress starts at seed 6
    cs = cluster_size or (32768, 16384, 8192)[i % 3]
    layout: dict[str, list[int]] = {}
    original = Fat32Card.write_file

    def recording(self, path, data, *a, **k):
        clusters = original(self, path, data, *a, **k)
        layout[os.path.basename(path)] = list(clusters)
        return clusters

    Fat32Card.write_file = recording
    try:
        sc = scenarios.stress(work, seed, cluster_size=cs, mixed=i % 2 == 0)
    finally:
        Fat32Card.write_file = original
    return sc, layout, cs


def explain(sc, res, layout, names) -> None:
    geom = next((v.geometry for v in res.volumes if getattr(v, "geometry", None)), None)
    if geom is None:
        print("no filesystem geometry: nothing to map clusters with")
        return
    owner = {}
    for name, clusters in layout.items():
        for c in clusters:
            owner[c] = name  # the last writer wins, as on the card
    by_start = {}
    for r in res.found:
        if r.extents:
            by_start.setdefault(geom.cluster_at(r.start), []).append(r)
    for name in names:
        clusters = layout.get(name)
        if not clusters:
            print("\n%s: never written on this card" % name)
            continue
        runs = _runs(clusters)
        print("\n%s: %d clusters in %d piece(s): %s" % (name, len(clusters), len(runs),
                                                       ", ".join("%d-%d" % r for r in runs)))
        for a, b in runs:
            before = [owner.get(c, "free") for c in range(a - 2, a)]
            after = [owner.get(c, "free") for c in range(b + 1, b + 3)]
            print("  piece %d-%d: before it %s, after it %s" % (a, b, before, after))
        for r in by_start.get(clusters[0], []):
            print("  found at its first cluster: %s %s, %d bytes in %d extent(s): %s" % (
                r.kind, r.status, r.size, len(r.extents), r.note))
            got = [geom.cluster_at(o) for o, _ in r.extents]
            print("    extents start at clusters %s" % got[:8])
        if clusters[0] not in by_start:
            print("  nothing was found at its first cluster")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed", type=int)
    ap.add_argument("cluster_size", type=int, nargs="?")
    ap.add_argument("--explain", nargs="*", default=[], help="also explain these files (names like IMG_0050.JPG)")
    ap.add_argument("--keep", help="keep the card image in this folder")
    a = ap.parse_args(argv)
    work = a.keep or tempfile.mkdtemp(prefix="ra-card-")
    os.makedirs(work, exist_ok=True)
    try:
        t = time.time()
        sc, layout, cs = build(a.seed, a.cluster_size, work)
        print("card: seed %d, %d KB clusters, %d files that survived the format (built in %.0fs)" % (
            a.seed, cs // 1024, len(sc.truth), time.time() - t))
        t = time.time()
        res = scan(sc.image)
        print("scanned in %.0fs" % (time.time() - t))
        got = set()
        for r in res.found:
            if r.status in ("ok", "unverified", "partial", "corrupt") and r.extents:
                try:
                    got.add(hashlib.sha256(r.read(res._src)).hexdigest())
                except OSError:
                    pass
        missing = sorted(sc.truth[h] for h in sc.truth if h not in got)
        print("recovered byte for byte: %d / %d" % (len(sc.truth) - len(missing), len(sc.truth)))
        if missing:
            print("missed: " + ", ".join(missing))
        explain(sc, res, layout, missing + [n for n in a.explain if n not in missing])
        res._src.close()
        return 0 if not missing else 1
    finally:
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
