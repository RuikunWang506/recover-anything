"""Make a read-only copy (image) of a card before doing anything else.

Working from an image means the card is read once, sequentially, which is the
gentlest thing you can do to failing hardware, and every later attempt is free.
GNU ddrescue is used when installed (best for failing drives); otherwise a
built-in copier skips unreadable sectors and records them.

Every image is tied to the card it came from (size plus a fingerprint of its
first and last megabyte), so a half-finished copy is only ever resumed from
the same card, never silently reused for a different one.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

from .devices import is_device_path, normalize_source
from .source import Source


class ImageError(Exception):
    pass


BLOCK = 4096


def identity(src: Source, samples: int = 256) -> dict:
    """Fingerprint a card: its size plus blocks sampled evenly across it.
    (The first and last megabyte alone are identical on most cards of the same
    size: a partition table and empty space.)"""
    size = src.size
    h = hashlib.sha256()
    if size:
        step = max(BLOCK, (size // samples) // BLOCK * BLOCK)
        for off in list(range(0, size, step))[:samples] + [max(0, size - BLOCK)]:
            h.update(src.read(off, BLOCK))
    return {"source": src.path, "size": size, "sample": h.hexdigest()}


def short_id(ident: dict) -> str:
    return hashlib.sha256(("%(size)d:%(sample)s" % ident).encode()).hexdigest()[:10]


def same_content(src: Source, image_path: str, blocks: int = 256) -> bool:
    """Compare random blocks of the card and an existing image directly."""
    import random
    try:
        if os.path.getsize(image_path) != src.size:
            return False
        rng = random.Random()
        with open(image_path, "rb") as f:
            for _ in range(blocks):
                off = rng.randrange(0, max(1, src.size // BLOCK)) * BLOCK
                f.seek(off)
                if f.read(BLOCK) != src.read(off, BLOCK):
                    return False
        return True
    except OSError:
        return False


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def make_image(source: str, dest: str, progress=None, use_ddrescue: bool = True) -> dict:
    source = normalize_source(source)
    if is_device_path(dest):
        raise ImageError("the destination %s is a device; images must be saved as a file" % dest)
    if _same_file(source, dest):
        raise ImageError("the destination is the source itself")
    map_path = dest + ".map.json"
    with Source(source) as src:
        if src.size <= 0:
            raise ImageError("couldn't read the size of %s (is it connected and readable?)" % source)
        ident = identity(src)
    state = {"identity": ident, "done": 0, "bad": []}
    if os.path.exists(dest):
        prev = None
        try:
            with open(map_path) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
        same = prev and {k: prev.get("identity", {}).get(k) for k in ("size", "sample")} == {
            k: ident[k] for k in ("size", "sample")}
        if same and prev.get("complete"):
            with Source(source) as src:
                same = same_content(src, dest)
        if not same:
            raise ImageError("%s already exists and was not copied from this card; choose another name" % dest)
        state = prev
        if state.get("done", 0) >= ident["size"] and state.get("complete"):
            return {"tool": state.get("tool", "built-in"), "bytes": ident["size"], "map": map_path, "resumed": True,
                    "unreadable_bytes": sum(n for _, n in state.get("bad", [])), "identity": ident}
    ddr = shutil.which("ddrescue") if use_ddrescue else None
    if ddr:
        log = dest + ".ddrescue-map"
        for args in (["-n"], ["-r2"]):
            r = subprocess.run([ddr, *args, source, dest, log], check=False)
            if r.returncode not in (0,):
                raise ImageError("ddrescue failed (exit %d); the copy is incomplete" % r.returncode)
        state.update(tool="ddrescue", done=ident["size"], complete=True)
        with open(map_path, "w") as f:
            json.dump(state, f)
        return {"tool": "ddrescue", "bytes": os.path.getsize(dest), "map": log, "identity": ident}
    t0 = time.time()
    with Source(source) as src:
        total = src.size
        resume = os.path.exists(dest) and state["done"] > 0
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        if not resume:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(dest, flags, 0o644)
        with os.fdopen(fd, "r+b") as out:
            if _same_file(source, dest):
                raise ImageError("the destination is the source itself")
            if not resume:
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
    state["complete"] = True
    state["tool"] = "built-in"
    with open(map_path, "w") as f:
        json.dump(state, f)
    bad = sum(n for _, n in state["bad"])
    return {"tool": "built-in", "bytes": total, "unreadable_bytes": bad, "map": map_path, "identity": ident}


def load_bad_ranges(image_path: str) -> list:
    """Unreadable ranges recorded when an image was made (filled with zeros)."""
    try:
        with open(image_path + ".map.json") as f:
            return [tuple(r) for r in json.load(f).get("bad", [])]
    except (OSError, ValueError):
        return []
