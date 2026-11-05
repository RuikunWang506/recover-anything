"""Decide whether each found file is whole, partial or corrupt."""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

from .formats import jpeg, others
from .records import Found


class ExtentReader:
    """read(off, n) over a file made of several pieces of the source."""

    def __init__(self, src, extents):
        self.src, self.extents = src, extents
        self.length = sum(n for _, n in extents)

    def read(self, off: int, n: int) -> bytes:
        out, pos = [], 0
        end = min(off + n, self.length)
        for eo, en in self.extents:
            if pos + en > off and pos < end:
                s = max(off, pos) - pos
                e = min(end, pos + en) - pos
                out.append(self.src.read(eo + s, e - s))
            pos += en
            if pos >= end:
                break
        return b"".join(out)


def _jpeg_job(args):
    path, extents, deep = args
    from .source import Source
    with Source(path) as src:
        data = b"".join(src.read(o, n) for o, n in extents)
    return jpeg.check(data) if deep else jpeg.check_fast(data)


def validate_one(src, rec: Found, deep: bool = True) -> None:
    if rec.kind == "jpeg":
        data = rec.read(src)
        rec.status, rec.note, pos = jpeg.check(data) if deep else jpeg.check_fast(data)
        rec.meta["bad_at"] = pos
        return
    r = ExtentReader(src, rec.extents)
    if rec.kind == "isobmff":
        st, why, pos = others.validate_isobmff(r.read, r.length)
        if st == "unverified":
            m = others.measure_isobmff(r.read, 0, r.length)
            st = "ok" if m.complete else "partial"
            why = "structure checked" if m.complete else m.reason
        rec.status, rec.note = st, why
        rec.meta["bad_at"] = pos
        return
    fn = {"png": others.measure_png, "gif": others.measure_gif, "riff": others.measure_riff,
          "tiff": others.measure_tiff, "pdf": others.measure_pdf, "zip": others.measure_zip}.get(rec.kind)
    if fn is None:
        rec.status = "unverified"
        rec.note = rec.note or "no check exists for this file type"
        return
    m = fn(r.read, 0, r.length)
    if m.complete and m.end <= r.length:
        rec.status, rec.note = "ok", "structure checked"
    else:
        rec.status, rec.note = "partial", m.reason


DEEP_LIMIT = 8 << 30  # above this much JPEG data, 'auto' checks a sample first
SAMPLE = 300


def _check_jpegs(src, jpegs, workers, progress, deep, done0=0, total=None):
    total = total or len(jpegs)
    path = getattr(src, "path", None)
    if workers > 1 and path and len(jpegs) > 4:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, (r, res) in enumerate(zip(jpegs, ex.map(_jpeg_job, [(path, r.extents, deep) for r in jpegs],
                                                               chunksize=2))):
                r.status, r.note, r.meta["bad_at"] = res
                if progress:
                    progress(done0 + i + 1, total)
    else:
        for i, r in enumerate(jpegs):
            validate_one(src, r, deep)
            if progress:
                progress(done0 + i + 1, total)


def validate_all(src, recs: list[Found], workers: int | None = None, progress=None, mode="auto") -> str:
    """Check every found file. JPEG mode: 'deep' decodes every block, 'fast'
    checks structure only, 'auto' goes deep unless there is a lot of data, in
    which case it deep-checks a random sample and goes deep everywhere if the
    sample finds anything the fast check missed. Returns the mode used."""
    if mode is True:
        mode = "auto"
    jpegs = [r for r in recs if r.kind == "jpeg"]
    rest = [r for r in recs if r.kind != "jpeg"]
    for r in rest:
        validate_one(src, r)
    if not jpegs:
        return mode
    workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2)))
    if mode == "auto" and sum(r.size for r in jpegs) <= DEEP_LIMIT:
        mode = "deep"
    if mode in ("deep", "fast"):
        _check_jpegs(src, jpegs, workers, progress, mode == "deep")
        return mode
    import random
    _check_jpegs(src, jpegs, workers, progress, False)
    fine = [r for r in jpegs if r.status == "ok"]
    sample = random.Random(1).sample(fine, min(SAMPLE, len(fine)))
    before = {id(r) for r in sample}
    _check_jpegs(src, sample, workers, None, True)
    if any(r.status != "ok" for r in sample if id(r) in before):
        rest_ok = [r for r in fine if r not in sample]
        _check_jpegs(src, rest_ok, workers, progress, True)
        return "deep"
    return "sampled"
