"""The recovery pipeline: filesystem pass -> carving -> validation -> reassembly."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import carve as carver
from .records import Found
from .source import Source
from .validate import validate_all
from .volume import ENCRYPTED, find_volumes, open_filesystem

EXT_KIND = {
    ".jpg": "jpeg", ".jpeg": "jpeg", ".jpe": "jpeg", ".jfif": "jpeg",
    ".mp4": "isobmff", ".mov": "isobmff", ".m4v": "isobmff", ".heic": "isobmff", ".heif": "isobmff",
    ".3gp": "isobmff", ".avif": "isobmff", ".cr3": "isobmff", ".m4a": "isobmff", ".lrv": "isobmff",
    ".png": "png", ".gif": "gif", ".tif": "tiff", ".tiff": "tiff", ".cr2": "tiff", ".nef": "tiff",
    ".arw": "tiff", ".dng": "tiff", ".orf": "tiff", ".rw2": "tiff", ".pef": "tiff", ".srw": "tiff",
    ".avi": "riff", ".wav": "riff", ".webp": "riff", ".pdf": "pdf",
    ".zip": "zip", ".docx": "zip", ".xlsx": "zip", ".pptx": "zip", ".epub": "zip",
}


@dataclass
class VolumeReport:
    offset: int
    size: int
    fs: str
    note: str = ""
    cluster_size: int = 0
    geometry: object = None
    fs_reader: object = None


@dataclass
class ScanResult:
    source: str
    size: int
    volumes: list = field(default_factory=list)
    found: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    seconds: float = 0.0
    bad_ranges: list = field(default_factory=list)


class Progress:
    def __init__(self, fn=None):
        self.fn = fn

    def __call__(self, stage: str, done=None, total=None):
        if self.fn:
            self.fn(stage, done, total)


def _fix_high_word(src, fsr, rec: Found, want: str) -> bool:
    """FAT32 deletes on some systems wipe the top 16 bits of the start cluster.
    Try the possible high words and keep the one whose data looks right."""
    lo = rec.meta.get("start_cluster", 0) & 0xFFFF
    for hi in range(1, ((fsr.cluster_count + 2) >> 16) + 1):
        c = (hi << 16) | lo
        if c >= fsr.cluster_count + 2:
            break
        if fsr.is_allocated(c):
            continue
        head = src.read(fsr.cluster_offset(c), 16)
        if carver.identify(head) == want:
            rec.extents = fsr.contiguous_extents(c, rec.expected_size)
            rec.meta["start_cluster"] = c
            rec.meta["fixed_high_word"] = hi
            return True
    return False


def _dedupe_starts(recs: list[Found]) -> None:
    """Before checking: a deleted entry pointing at a cluster that a still-
    visible file now starts at was overwritten by it."""
    by_start: dict[int, list[Found]] = {}
    for r in recs:
        if r.status != "lost" and r.extents:
            by_start.setdefault(r.start, []).append(r)
    for group in by_start.values():
        live = [r for r in group if r.meta.get("live")]
        if live and len(group) > 1:
            for other in group:
                if other not in live:
                    other.status = "lost"
                    other.note = "its space was reused by %s" % (live[0].name or "another file")


def _dedupe_after_check(recs: list[Found]) -> None:
    """After checking: several deleted entries claim the same start cluster
    (space reused again and again). Keep the one whose own data is there: it
    checks out and its recorded size matches what is actually stored."""
    by_start: dict[int, list[Found]] = {}
    for r in recs:
        if r.status not in ("lost", "embedded") and r.extents and r.origin in ("filesystem", "lost-folder"):
            by_start.setdefault(r.start, []).append(r)
    for group in by_start.values():
        if len(group) < 2:
            continue

        def key(r):
            # Before reassembly "partial" and "corrupt" both just mean "not in
            # one piece", so they rank the same: a fragmented photo must not
            # lose to a stale entry that happens to cover its first fragment.
            actual = r.meta.get("actual_len") or r.size
            gap = abs((r.expected_size or actual) - actual)
            return (r.status != "ok", bool(r.meta.get("size_mismatch")),
                    bool(r.meta.get("was_deleted", r.deleted)), gap)
        group.sort(key=key)
        keep = group[0]
        if keep.meta.get("size_mismatch") and keep.meta.get("actual_len"):
            keep.extents = _trim(keep.extents, keep.meta["actual_len"])
            keep.note = "the name may belong to an earlier file stored in the same place"
        for other in group[1:]:
            other.status = "lost"
            other.note = "its space was reused by %s" % (keep.name or "another file")


def _trim(extents, length):
    out, rem = [], length
    for o, n in extents:
        if rem <= 0:
            break
        out.append((o, min(n, rem)))
        rem -= n
    return out


def _logical(rec: Found, off: int):
    """Offset within the file of the source byte `off`, or None."""
    pos = 0
    for eo, en in rec.extents:
        if eo <= off < eo + en:
            return pos + off - eo
        pos += en
    return None


class _Owner:
    """Answers "is the byte at this offset part of this file?" for a file that
    checked out, from what was actually verified or what its own structure
    points to (a RAW's previews, an AVI's frames, a ZIP's members, the extra
    pictures of a multi-picture JPEG). Work is done lazily, once per file."""

    def __init__(self, src, rec: Found):
        from .validate import ExtentReader
        self.rec, self.reader = rec, ExtentReader(src, rec.extents)
        self.header = self.main_end = self.verified = self.refs = None

    def claims(self, at: int) -> bool:
        from .formats import jpeg
        rec, r = self.rec, self.reader
        if rec.kind == "jpeg":
            if self.header is None:
                try:
                    self.header = jpeg.parse_header(r.read, 0)
                except Exception:
                    self.header = False
            h = self.header
            if not h:
                return False
            if at < h.entropy_start or at in h.mpf_starts:
                return True  # inside its header segments (a thumbnail) or an indexed extra picture
            if self.main_end is None:
                # another file's start can't hide inside a picture that parses to its end marker
                m = jpeg.measure(r.read, 0, r.length + 1)
                self.main_end = (m.header.main_end or 0) if m.header else 0
            return at < self.main_end
        if self.verified is None:
            self.verified, self.refs = self._contents()
        return at < self.verified or at in self.refs

    def _contents(self):
        from .formats import others
        from .validate import validate_zip
        rec, r = self.rec, self.reader
        kind, check = rec.kind, rec.meta.get("check")
        try:
            if kind == "png" and check == "full":
                return r.length, set()  # every chunk's checksum matched
            if kind == "zip":
                if check == "full":
                    return r.length, set()  # every member's checksum matched
                refs: set = set()
                validate_zip(r.read, r.length, refs)
                return 0, refs
            if kind == "tiff":
                return 0, set(others.measure_tiff(r.read, 0, r.length).refs)
            if kind == "riff":
                return 0, set(others.measure_riff(r.read, 0, r.length, want_refs=True).refs)
            if kind == "isobmff":
                refs = set()
                others.validate_isobmff(r.read, r.length, refs)
                return 0, refs
        except Exception:
            pass
        return 0, set()


def _drop_embedded(src, recs: list[Found]) -> None:
    """A file found inside another file that checked out is either part of it
    (a preview inside a RAW, a photo stored in a ZIP, a frame of a Motion-JPEG
    video) or its space was reused. Only bytes that were actually verified, or
    an offset the container's own structure points to, can claim a file: a RAW
    is checked by structure only, and a fragmented one "contains" whatever
    happens to follow its first piece on the card."""
    import heapq
    spans = sorted((o, o + n, i, r) for i, r in enumerate(recs) if r.status == "ok" and r.extents
                   for o, n in r.extents)
    if not spans:
        return
    owners: dict[int, _Owner] = {}
    active: list = []
    j = 0
    for r in sorted((r for r in recs if r.extents and r.status not in ("lost", "embedded")), key=lambda r: r.start):
        q = r.start
        while j < len(spans) and spans[j][0] < q:
            _s0, e0, i, owner = spans[j]
            heapq.heappush(active, (e0, i, j, owner))
            j += 1
        while active and active[0][0] <= q:
            heapq.heappop(active)
        for _e, i, _k, owner in sorted(active, key=lambda t: t[1]):
            if owner is r:
                continue
            at = _logical(owner, q)
            if not at:
                continue
            if i not in owners:
                owners[i] = _Owner(src, owner)
            if not owners[i].claims(at):
                continue
            if r.origin == "carved":
                r.status = "embedded"
                r.note = "part of %s" % (owner.name or "another recovered file")
            elif r.status != "ok":
                r.status = "lost"
                r.note = "its space now belongs to %s" % (owner.name or "another recovered file")
            # a named file that checks out on its own is kept: its folder entry says it starts here
            break


def _lenient(head: bytes, want: str) -> bool:
    """Looser signature checks for files the filesystem names: an old QuickTime
    file may start with 'moov' or 'mdat' instead of 'ftyp', a JPEG may start
    with an unusual APPn segment."""
    from .formats.others import TOP_LEVEL
    if want == "jpeg":
        return head[:3] == b"\xff\xd8\xff"
    if want == "isobmff":
        return len(head) >= 8 and head[4:8] in TOP_LEVEL
    if want == "riff":
        return head[:4] == b"RIFF"
    return False


def _identify_record(src, fsr, r: Found) -> None:
    want = EXT_KIND.get(r.ext)
    head = src.read(r.start, 16) if r.extents else b""
    got = carver.identify(head)
    if want and got != want and r.deleted and r.meta.get("high_word_zero"):
        if _fix_high_word(src, fsr, r, want):
            got = want
            head = src.read(r.start, 16)
        elif r.meta.get("start_allocated"):
            r.kind = want
            r.status, r.note = "lost", "a newer file now occupies its space"
            return
    if want and got is None and _lenient(head, want):
        got = want
    live = not r.deleted
    if live:
        r.meta["live"] = True
    if want and got and got != want and not live:
        # The folder entry says one thing, the data at its start is another
        # file: this file's own beginning was overwritten.
        r.kind = want
        r.status, r.note = "lost", "its space now holds a different file"
        return
    if got:
        r.kind = got
        if got == "jpeg" and r.ext not in (".jpg", ".jpeg", ".jpe", ".jfif"):
            r.ext = r.ext or ".jpg"
    elif want and not live:
        r.kind = want
        r.status = "lost"
        r.note = "its space has been reused by newer data"
    else:
        r.kind = "other"  # still copied (live files are intact by definition)


def _volume_at(vols, off: int):
    for v in vols:
        if v.offset <= off < v.offset + v.size:
            return v
    return None


def _mark_unreadable(found: list[Found], bad: list) -> None:
    """Files that include sectors the card couldn't read are not 'ok'."""
    if not bad:
        return
    from bisect import bisect_right
    bad = sorted((int(o), int(o) + int(n)) for o, n in bad)
    starts = [b[0] for b in bad]
    for r in found:
        if r.status not in ("ok", "unverified"):
            continue
        for o, n in r.extents:
            i = bisect_right(starts, o + n - 1) - 1
            if i >= 0 and bad[i][1] > o:
                r.status = "partial"
                r.note = "part of it could not be read from the card (filled with zeros)"
                break


def scan(path: str, kinds=None, include_live: bool = False, reassemble: bool = True, workers=None,
         progress=None, fast: bool = False, deep: bool = False) -> ScanResult:
    t0 = time.time()
    prog = Progress(progress)
    src = Source(path)
    res = ScanResult(source=path, size=src.size)
    prog("Looking at how the card is laid out")
    try:
        vols = find_volumes(src)
    except Exception as e:
        vols = []
        res.warnings.append("The partition table is unreadable (%s); searching the raw data." % e)
    all_found: list[Found] = []
    skip_ranges = []
    for vol in vols:
        vr = VolumeReport(vol.offset, vol.size, vol.fs, vol.note)
        res.volumes.append(vr)
        if vol.note:
            res.warnings.append(vol.note)
        if vol.fs in ENCRYPTED:
            res.warnings.append("This volume is encrypted (%s). Carving encrypted data finds nothing: unlock it "
                                "with its password or recovery key first." % vol.fs)
            skip_ranges.append((vol.offset, vol.offset + vol.size))
            continue
        fsr = None
        try:
            fsr = open_filesystem(src, vol)
        except Exception as e:  # damaged filesystem: fall back to carving
            res.warnings.append("Could not read the %s filesystem (%s); searching the raw data instead." % (vol.fs, e))
        if fsr is None:
            continue
        vr.cluster_size = fsr.cluster_size
        vr.geometry = fsr.geometry
        vr.fs_reader = fsr
        recs: list[Found] = []
        prog("Reading the folder structure")
        try:
            for r in fsr.walk():
                recs.append(r)
        except Exception as e:
            res.warnings.append("Part of the folder structure is damaged (%s)." % e)
        prog("Looking for folders that survived a format")
        try:
            for r in fsr.lost_folder_records():
                recs.append(r)
        except Exception as e:
            res.warnings.append("Part of the lost-folder search failed (%s)." % e)
        for r in recs:
            r.volume = vr
            try:
                _identify_record(src, fsr, r)
            except Exception as e:
                r.kind, r.note = "other", "could not be identified (%s)" % e
        _dedupe_starts(recs)
        all_found += recs
    if not res.volumes:
        res.volumes.append(VolumeReport(0, src.size, "unknown", ""))
    # One carving pass over the whole device, on every 512-byte sector: this
    # also finds files from an earlier format with a different layout.
    prog("Searching every sector for photos, videos and documents")
    claimed = carver.Claimed()
    for r in all_found:
        if r.meta.get("live") and r.meta.get("chain") in ("fat", "contiguous"):
            for o, n in r.extents:
                claimed.add(o, o + n)
    known = {r.start for r in all_found if r.status != "lost" and r.extents}
    regions, pos = [], 0
    for a, b in sorted(skip_ranges):
        if a > pos:
            regions.append((pos, a))
        pos = max(pos, b)
    if pos < src.size:
        regions.append((pos, src.size))
    headers_all = []
    for a, b in regions:
        carved, headers = carver.carve(src, a, b, 512, 0, known, claimed,
                                       progress=lambda d, t: prog("scan", d, t), kinds=kinds)
        headers_all += headers
        all_found += carved
    for vr in res.volumes:
        vr.headers = [h for h in headers_all if vr.offset <= h[0] < vr.offset + vr.size]
    for r in all_found:
        if not hasattr(r, "volume"):
            r.volume = _volume_at(res.volumes, r.start)
    to_check = [r for r in all_found if r.status != "lost" and r.kind != "other" and r.extents]
    prog("Checking every file it found")
    res.check_mode = validate_all(src, to_check, workers=workers, progress=lambda d, t: prog("check", d, t),
                                  mode="fast" if fast else "deep" if deep else "auto")
    if res.check_mode == "sampled":
        res.warnings.append("There was a lot to check, so photos were checked by structure, plus a full block-by-block "
                            "check of a random sample (which found no problems). Run with --deep to check every block "
                            "of every photo.")
    elif res.check_mode == "fast":
        res.warnings.append("Fast mode: photos were checked by structure, not decoded block by block.")
    _dedupe_after_check(all_found)
    for r in all_found:
        if r.status == "ok" and r.meta.get("size_mismatch") and r.meta.get("actual_len"):
            r.extents = _trim(r.extents, r.meta["actual_len"])
    if reassemble:
        from .reassemble import reassemble_all
        prog("Putting fragmented photos and videos back together")
        try:
            reassemble_all(src, res, all_found, progress=lambda d, t: prog("reassemble", d, t))
        except Exception as e:
            res.warnings.append("Rebuilding fragmented files stopped early (%s)." % e)
    _drop_embedded(src, all_found)
    for r in all_found:
        # A photo with foreign data mixed in: keep the part before the damage.
        if r.kind == "jpeg" and r.status == "corrupt" and r.meta.get("bad_at"):
            r.extents = _trim(r.extents, max(512, int(r.meta["bad_at"])))
            r.status = "partial"
            r.note = "only the top part of the picture could be recovered"
    bad = list(src.bad_ranges)
    try:
        from .imaging import load_bad_ranges
        bad += load_bad_ranges(path)
    except Exception:
        pass
    _mark_unreadable(all_found, bad)
    res.found = [r for r in all_found if r.status != "embedded" and (include_live or not r.meta.get("live"))]
    if kinds:  # --only: the files the folders name are filtered too, not just carved ones
        res.found = [r for r in res.found if carver.group_of(r.kind, r.ext) in kinds]
    res.live_count = sum(1 for r in all_found if r.meta.get("live"))
    res.bad_ranges = bad
    res.seconds = time.time() - t0
    res._src = src
    return res
