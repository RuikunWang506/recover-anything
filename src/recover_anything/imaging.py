"""Make a read-only copy (image) of a card before doing anything else.

Working from an image means the card is read once, sequentially, which is the
gentlest thing you can do to failing hardware, and every later attempt is free.
GNU ddrescue is used when installed (best for failing drives); otherwise a
built-in copier skips unreadable sectors and records them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from .source import Source


def make_image(source: str, dest: str, progress=None, use_ddrescue: bool = True) -> dict:
    if os.path.exists(dest) and os.path.realpath(dest) == os.path.realpath(source):
        raise ValueError("source and destination are the same")
    map_path = dest + ".map.json"
    ddr = shutil.which("ddrescue") if use_ddrescue else None
    if ddr:
        log = dest + ".ddrescue-map"
        for args in (["-n"], ["-r2"]):
            subprocess.run([ddr, *args, source, dest, log], check=False)
        size = os.path.getsize(dest)
        return {"tool": "ddrescue", "bytes": size, "map": log}
    state = {"done": 0, "bad": []}
    if os.path.exists(map_path) and os.path.exists(dest):
        try:
            with open(map_path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            pass
    t0 = time.time()
    with Source(source) as src:
        total = src.size
        mode = "r+b" if os.path.exists(dest) and state["done"] else "wb"
        with open(dest, mode) as out:
            if mode == "wb":
                out.truncate(total)
            pos = state["done"]
            step = 4 << 20
            while pos < total:
                n = min(step, total - pos)
                before = len(src.bad_ranges)
                data = src.read_uncached(pos, n)
                if data.count(0) != len(data):
                    out.seek(pos)
                    out.write(data)
                state["bad"] += [list(r) for r in src.bad_ranges[before:]]
                pos += n
                state["done"] = pos
                if (pos // step) % 64 == 0 or pos >= total:
                    out.flush()
                    with open(map_path, "w") as f:
                        json.dump(state, f)
                if progress:
                    progress(pos, total, time.time() - t0)
    with open(map_path, "w") as f:
        json.dump(state, f)
    bad = sum(n for _, n in state["bad"])
    return {"tool": "built-in", "bytes": total, "unreadable_bytes": bad, "map": map_path}
