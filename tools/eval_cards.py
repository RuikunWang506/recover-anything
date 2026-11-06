"""Run randomized simulated cards and count what came back.

    python tools/eval_cards.py mixed 1-20                 # cards shared by many devices (FAT32)
    python tools/eval_cards.py mixed 1-8 --fs exFAT
    python tools/eval_cards.py one mts 1-4                # only one kind of file: mp3 ts mts flac opus mp4
                                                          # jpg amr mpg doc
    python tools/eval_cards.py one mp3 1-4 -v             # also list every file that didn't come back

For each card it prints what was recovered byte for byte, by file type, and
the number that matters most: files marked recovered that aren't byte for
byte a file that was ever written. That count must stay 0 (CLAUDE.md rule 3).
"Saved exact but not proven" counts files saved with the right bytes but
labeled "not checked", because their type can't prove it.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def seeds(text: str):
    out = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def main(argv=None) -> int:
    from recover_anything.pipeline import scan
    from recover_anything.simcard import scenarios
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["mixed", "one"])
    ap.add_argument("args", nargs="+", help="for 'one': the kind, then the seeds; for 'mixed': the seeds")
    ap.add_argument("--fs", default="FAT32", choices=["FAT32", "exFAT"])
    ap.add_argument("-v", action="store_true", help="list every file that didn't come back")
    a = ap.parse_args(argv)
    kind = a.args[0] if a.what == "one" else None
    run = seeds(a.args[1] if a.what == "one" else a.args[0])
    tot, got_c, loose_c = collections.Counter(), collections.Counter(), collections.Counter()
    wrong = frag_t = frag_g = 0
    for seed in run:
        with tempfile.TemporaryDirectory() as d:
            sc = (scenarios.single_kind(d, kind, seed, fs=a.fs) if kind
                  else scenarios.mixed_device(d, seed, fs=a.fs))
            t = time.time()
            res = scan(sc.image, workers=2)
            src = res._src
            ever = sc.groups.get("ever written", set(sc.truth))
            ok, loose = {}, set()
            for r in res.found:
                if not r.extents or r.status not in ("ok", "unverified"):
                    continue
                h = hashlib.sha256(r.read(src)).hexdigest()
                if r.status == "unverified":
                    loose.add(h)
                    continue
                if h not in ever:
                    wrong += 1
                    print("  WRONG: seed %d, %s %s (%s, %d bytes): %s" % (seed, r.kind, r.ext, r.origin, r.size, r.note))
                ok[h] = r
            for h, p in sc.truth.items():
                ext = os.path.splitext(p)[1].lower()
                tot[ext] += 1
                if h in ok:
                    got_c[ext] += 1
                elif h in loose:
                    loose_c[ext] += 1
                elif a.v:
                    print("  missed: seed %d, %s (%s)" % (seed, p, "fragmented" if h in sc.groups["fragmented"]
                                                           else "in one piece"))
            frag_t += len(sc.groups["fragmented"])
            frag_g += len(sc.groups["fragmented"] & set(ok))
            print("card %d: %d of %d recovered in %.1f s" % (seed, len(set(ok) & set(sc.truth)), len(sc.truth),
                                                               time.time() - t))
            src.close()
    print("recovered by type:", {e: "%d/%d" % (got_c[e], tot[e]) for e in sorted(tot)})
    print("saved exact but not proven:", dict(loose_c))
    print("fragmented files recovered: %d/%d" % (frag_g, frag_t))
    print("marked recovered but wrong: %d" % wrong)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
