"""`recover-anything selftest`: build simulated SD cards, wreck them the way people
do, run the real recovery, and check every recovered file byte for byte.

Anyone can reproduce the numbers in the README with this one command.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time


def _hash_tree(root: str) -> dict:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith((".html", ".json", ".img", ".part")):
                continue
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[hashlib.sha256(fh.read()).hexdigest()] = p
    return out


def run(quick: bool = False, keep: str | None = None, as_json: bool = False) -> int:
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("selftest needs Pillow to generate test photos: pip install pillow", file=sys.stderr)
        return 2
    from .organize import write_all
    from .pipeline import scan
    from .simcard import scenarios as S

    n = 20 if quick else 63
    plan = [
        ("FAT32", lambda d: S.deleted(d, n)),
        ("FAT32", lambda d: S.formatted(d, n)),
        ("FAT32", lambda d: S.formatted_then_used(d, n, 20 if not quick else 8)),
        ("FAT32", lambda d: S.fragmented(d, 60 if not quick else 20)),
    ]
    if not quick:
        plan += [
            ("exFAT", lambda d: S.deleted(d, n, fs="exFAT")),
            ("exFAT", lambda d: S.formatted(d, n, fs="exFAT")),
            ("exFAT", lambda d: S.fragmented(d, 60, fs="exFAT")),
        ]
    work = keep or tempfile.mkdtemp(prefix="recover-anything-selftest-")
    os.makedirs(work, exist_ok=True)
    rows = []
    ok_all = True
    try:
        for fs, build in plan:
            sc = build(work)
            out = os.path.join(work, "out-" + os.path.basename(sc.image)[:-4])
            shutil.rmtree(out, ignore_errors=True)
            t0 = time.time()
            res = scan(sc.image)
            write_all(res, out)
            secs = time.time() - t0
            got = _hash_tree(out)
            hits = {h for h in sc.truth if h in got}
            wrong = [p for h, p in got.items() if h not in sc.truth and "Partly recovered" not in p]
            row = {"scenario": sc.title, "filesystem": fs, "recovered": len(hits), "recoverable": len(sc.truth),
                   "overwritten": sc.overwritten, "seconds": round(secs, 1), "unexpected_files": len(wrong),
                   "groups": {g: [len(hs & hits), len(hs & set(sc.truth))] for g, hs in sc.groups.items()},
                   "named": sum(1 for h in hits if os.path.basename(got[h]).upper() == sc.truth[h].replace("(new) ", ""))}
            if sc.name == "reused":
                old = sc.groups["old photos"] & set(sc.truth)
                row["recoverable"] = len(old)
                row["recovered"] = len(old & hits)
            rows.append(row)
            ok_all &= row["recovered"] == row["recoverable"] and not wrong
            if not keep:
                os.remove(sc.image)
                shutil.rmtree(out, ignore_errors=True)
            if not as_json:
                print("  done: %s (%s) in %.0fs" % (sc.title, fs, secs), file=sys.stderr)
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    if as_json:
        print(json.dumps(rows, indent=1))
    else:
        print()
        print("| Simulated card | FS | Recovered byte for byte | Original names | Notes | Time |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            notes = []
            if r["overwritten"]:
                notes.append("%d were physically overwritten by the new photos" % r["overwritten"])
            if "fragmented" in r["groups"]:
                a, b = r["groups"]["fragmented"]
                notes.append("%d of %d fragmented photos rebuilt" % (a, b))
            if r["unexpected_files"]:
                notes.append("%d unexpected files" % r["unexpected_files"])
            print("| %s | %s | %d / %d (%d%%) | %d | %s | %.0f s |" % (
                r["scenario"], r["filesystem"], r["recovered"], r["recoverable"],
                100 * r["recovered"] // max(1, r["recoverable"]), r["named"], "; ".join(notes) or "", r["seconds"]))
        print()
        print("PASS" if ok_all else "Some files were not recovered (see above).")
    return 0 if ok_all else 1
