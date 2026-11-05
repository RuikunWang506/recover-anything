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


def _check_jpeg_bytes(data: bytes, deep: bool, expected: int | None):
    st, note, pos = jpeg.check(data) if deep else jpeg.check_fast(data)
    actual = None
    if expected and st == "ok":
        m = jpeg.measure(lambda o, n: data[o:o + n], 0, len(data) + 1)
        if m.complete:
            actual = m.end
    strength = "full" if (st == "ok" and deep and note == "every block decoded") else "structure"
    return st, note, pos, actual, strength


def _jpeg_job(args):
    path, extents, deep, expected = args
    from .source import Source
    try:
        with Source(path) as src:
            total = sum(n for _, n in extents)
            if total > jpeg.MAX_JPEG:
                return "unverified", "too large to check", None, None, "none"
            data = b"".join(src.read(o, n) for o, n in extents)
        return _check_jpeg_bytes(data, deep, expected)
    except Exception as e:  # never let one file stop the run
        return "unverified", "could not be checked (%s)" % type(e).__name__, None, None, "none"


def _apply_jpeg(rec: Found, res) -> None:
    rec.status, rec.note, rec.meta["bad_at"], actual, rec.meta["check"] = res
    if actual is not None and rec.expected_size:
        rec.meta["actual_len"] = actual
        if actual + 65536 < rec.expected_size:
            # The folder entry says the file is bigger than the photo stored
            # there: another (shorter) photo reused this space.
            rec.meta["size_mismatch"] = True


ZIP_STEP = 1 << 20
ZIP_CHECK_BUDGET = 2 << 30  # uncompressed bytes checked per archive


class _ZipBad(Exception):
    pass


def _member_crc(read, at: int, csize: int, usize: int, method: int) -> int:
    """CRC-32 of one member, inflated in bounded steps: a tiny archive can
    claim to expand to gigabytes, so output is never held more than 1 MB at a
    time and never allowed past the size the archive declares."""
    import zlib
    dec = zlib.decompressobj(-15) if method == 8 else None
    crc = got = out = 0

    def take(chunk):
        nonlocal crc, out
        out += len(chunk)
        if out > usize:
            raise _ZipBad("a member is bigger than the archive says")
        crc = zlib.crc32(chunk, crc)

    while got < csize and not (dec and dec.eof):
        piece = read(at + got, min(ZIP_STEP, csize - got))
        if not piece:
            raise _ZipBad("a member is cut short")
        got += len(piece)
        if dec is None:
            take(piece)
            continue
        buf = piece
        while buf and not dec.eof:
            take(dec.decompress(buf, ZIP_STEP))
            buf = dec.unconsumed_tail
    while dec is not None and not dec.eof:
        chunk = dec.decompress(b"", ZIP_STEP)
        if not chunk:
            break
        take(chunk)
    if out != usize:
        raise _ZipBad("a member's size doesn't match the archive")
    return crc & 0xFFFFFFFF


def validate_zip(read, length: int, refs: set | None = None) -> tuple[str, str]:
    """Check every member's CRC (stored and deflated members). If `refs` is
    given, the offset of every member's data is added to it."""
    import struct
    import zlib
    tail = read(max(0, length - 65558), min(length, 65558))
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or i + 22 > len(tail):
        return "corrupt", "no end-of-archive record"
    _, _, _, _, total, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[i:i + 22])
    if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        return "ok", "structure checked (ZIP64)"
    cd = read(cd_off, min(cd_size, 64 << 20))
    pos, checked, skipped = 0, 0, 0
    cbudget, ubudget = 512 << 20, ZIP_CHECK_BUDGET
    for _ in range(min(total, 100000)):
        if cd[pos:pos + 4] != b"PK\x01\x02" or pos + 46 > len(cd):
            return "corrupt", "archive directory damaged"
        (method, crc, csize, usize, nlen, xlen, clen, lho) = struct.unpack("<10xH4xIIIHHH8xI", cd[pos:pos + 46])
        pos += 46 + nlen + xlen + clen
        lh = read(lho, 30)
        if len(lh) < 30 or lh[:4] != b"PK\x03\x04":
            return "corrupt", "a member is missing (byte %d)" % lho
        ln, lx = struct.unpack("<HH", lh[26:30])
        data_at = lho + 30 + ln + lx
        if refs is not None:
            refs.add(data_at)
        if (method not in (0, 8) or 0xFFFFFFFF in (csize, usize) or csize > cbudget or usize > ubudget
                or (method == 0 and csize != usize)):
            skipped += 1
            continue
        cbudget -= csize
        ubudget -= usize
        try:
            got = _member_crc(read, data_at, csize, usize, method)
        except _ZipBad as e:
            return "corrupt", "%s (byte %d)" % (e, data_at)
        except zlib.error:
            return "corrupt", "a member's data is damaged (byte %d)" % data_at
        if got != crc:
            return "corrupt", "a member's checksum doesn't match (byte %d)" % data_at
        checked += 1
    if skipped:
        return "ok", "%d members checksum-verified; %d could not be checked" % (checked, skipped)
    return "ok", "%d members checksum-verified" % checked


def validate_one(src, rec: Found, deep: bool = True) -> None:
    try:
        _validate_one(src, rec, deep)
    except Exception as e:  # never let one file stop the run
        rec.status = "unverified"
        rec.note = "could not be checked (%s)" % type(e).__name__
        rec.meta["check"] = "none"


def _validate_one(src, rec: Found, deep: bool = True) -> None:
    if rec.kind == "jpeg":
        if rec.size > jpeg.MAX_JPEG:
            rec.status, rec.note, rec.meta["check"] = "unverified", "too large to check", "none"
            return
        _apply_jpeg(rec, _check_jpeg_bytes(rec.read(src), deep, rec.expected_size))
        return
    r = ExtentReader(src, rec.extents)
    if rec.kind == "isobmff":
        st, why, pos = others.validate_isobmff(r.read, r.length)
        rec.meta["check"] = "full" if st == "ok" else "structure"
        if st == "unverified":
            m = others.measure_isobmff(r.read, 0, r.length)
            st = "ok" if m.complete else "partial"
            why = "structure checked" if m.complete else m.reason
        rec.status, rec.note = st, why
        rec.meta["bad_at"] = pos
        return
    if rec.kind == "zip":
        st, why = validate_zip(r.read, r.length)
        rec.status, rec.note = st, why
        rec.meta["check"] = "full" if why.endswith(" members checksum-verified") else "structure"
        return
    fn = {"png": others.measure_png, "gif": others.measure_gif, "riff": others.measure_riff,
          "tiff": others.measure_tiff, "pdf": others.measure_pdf}.get(rec.kind)
    if fn is None:
        rec.status = "unverified"
        rec.note = rec.note or "no check exists for this file type"
        rec.meta["check"] = "none"
        return
    m = fn(r.read, 0, r.length)
    if m.complete and m.end <= r.length:
        rec.status, rec.note = "ok", "checksums verified" if rec.kind == "png" else "structure checked"
        rec.meta["check"] = "full" if rec.kind == "png" else "structure"
    else:
        rec.status, rec.note = "partial", m.reason
        rec.meta["check"] = "structure"


DEEP_LIMIT = 8 << 30  # above this much JPEG data, 'auto' checks a sample first
SAMPLE = 300


def _check_jpegs(src, jpegs, workers, progress, deep, done0=0, total=None):
    total = total or len(jpegs)
    path = getattr(src, "path", None)
    if workers > 1 and path and len(jpegs) > 4:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            jobs = [(path, r.extents, deep, r.expected_size) for r in jpegs]
            for i, (r, res) in enumerate(zip(jpegs, ex.map(_jpeg_job, jobs, chunksize=2))):
                _apply_jpeg(r, res)
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
