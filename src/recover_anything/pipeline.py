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
    """Two directory entries pointing at the same cluster: the older one was
    deleted and its space reused. Keep the entry that was still in use."""
    by_start: dict[int, list[Found]] = {}
    for r in recs:
        if r.status != "lost" and r.extents:
            by_start.setdefault(r.start, []).append(r)
    for group in by_start.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (not r.meta.get("live", False), bool(r.meta.get("was_deleted", r.deleted)),
                                  r.origin != "filesystem"))
        keep = group[0]
        for other in group[1:]:
            other.status = "lost"
            other.note = "its space was reused by %s" % (keep.name or "another file")


def _drop_embedded(recs: list[Found]) -> None:
    """A header inside a file that verified is part of that file (a JPEG
    inside a ZIP, a preview inside a RAW), or its space was reused."""
    from bisect import bisect_left
    spans = sorted((o, o + n, id(r), r) for r in recs if r.status == "ok" for o, n in r.extents)
    if not spans:
        return
    starts = [s[0] for s in spans]
    best_end, best_owner = [], []
    cur_end, cur_owner = -1, None
    for o, e, _i, owner in spans:
        if e > cur_end:
            cur_end, cur_owner = e, owner
        best_end.append(cur_end)
        best_owner.append(cur_owner)
    for r in recs:
        if not r.extents or r.status in ("lost", "embedded"):
            continue
        i = bisect_left(starts, r.start) - 1
        if i < 0 or best_end[i] <= r.start or best_owner[i] is r:
            continue
        owner = best_owner[i]
        if r.origin == "carved":
            r.status = "embedded"
            r.note = "part of %s" % (owner.name or "another recovered file")
        else:
            r.status = "lost"
            r.note = "its space now belongs to %s" % (owner.name or "another recovered file")


def scan(path: str, kinds=None, include_live: bool = False, reassemble: bool = True, workers=None,
         progress=None, fast: bool = False, deep: bool = False) -> ScanResult:
    t0 = time.time()
    prog = Progress(progress)
    src = Source(path)
    res = ScanResult(source=path, size=src.size)
    prog("Looking at how the card is laid out")
    vols = find_volumes(src)
    all_found: list[Found] = []
    for vol in vols:
        vr = VolumeReport(vol.offset, vol.size, vol.fs, vol.note)
        res.volumes.append(vr)
        if vol.note:
            res.warnings.append(vol.note)
        if vol.fs in ENCRYPTED:
            res.warnings.append("This volume is encrypted (%s). Carving encrypted data finds nothing: unlock it "
                                "with its password or recovery key first." % vol.fs)
            continue
        fsr = None
        try:
            fsr = open_filesystem(src, vol)
        except Exception as e:  # damaged filesystem: fall back to carving
            res.warnings.append("Could not read the %s filesystem (%s); searching the raw data instead." % (vol.fs, e))
        recs: list[Found] = []
        if fsr is not None:
            vr.cluster_size = fsr.cluster_size
            vr.geometry = fsr.geometry
            vr.fs_reader = fsr
            prog("Reading the folder structure")
            try:
                recs = list(fsr.walk())
            except Exception as e:
                res.warnings.append("The folder structure is damaged (%s)." % e)
            prog("Looking for folders that survived a format")
            try:
                recs += list(fsr.lost_folder_records())
            except Exception as e:
                res.warnings.append("Lost-folder search failed (%s)." % e)
            for r in recs:
                r.volume = vr
                want = EXT_KIND.get(r.ext)
                head = src.read(r.start, 16) if r.extents else b""
                got = carver.identify(head)
                if want and got != want and r.deleted and r.meta.get("high_word_zero"):
                    if _fix_high_word(src, fsr, r, want):
                        got = want
                    elif r.meta.get("start_allocated"):
                        r.status, r.note = "lost", "a newer file now occupies its space"
                        continue
                if got:
                    r.kind = got
                    if got == "jpeg":
                        r.ext = r.ext if r.ext in (".jpg", ".jpeg", ".jpe", ".jfif") else ".jpg"
                elif want:
                    r.kind = want
                    r.status = "lost"
                    r.note = "its space has been reused by newer data"
                else:
                    r.kind = "other"
                if not r.deleted:
                    r.meta["live"] = True
            _dedupe_starts(recs)
            region = (fsr.data_offset, fsr.data_offset + fsr.cluster_count * fsr.cluster_size)
            align, base = fsr.cluster_size, fsr.data_offset
        else:
            region = (vol.offset, vol.offset + vol.size)
            align, base = 512, vol.offset
        prog("Searching every cluster for photos, videos and documents")
        claimed = carver.Claimed()
        for r in recs:
            if r.meta.get("live") and r.meta.get("chain") == "fat":
                for o, n in r.extents:
                    claimed.add(o, o + n)
        known = {r.start for r in recs if r.status != "lost" and r.extents}
        carved, headers = carver.carve(src, region[0], region[1], align, base, known, claimed,
                                       progress=lambda d, t: prog("scan", d, t), kinds=kinds)
        for r in carved:
            r.volume = vr
        vr.headers = headers
        all_found += recs + carved
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
    if reassemble:
        from .reassemble import reassemble_all
        prog("Putting fragmented photos back together")
        reassemble_all(src, res, all_found, progress=lambda d, t: prog("reassemble", d, t))
    _drop_embedded(all_found)
    res.found = [r for r in all_found if r.status != "embedded" and (include_live or not r.meta.get("live"))]
    res.live_count = sum(1 for r in all_found if r.meta.get("live"))
    res.bad_ranges = list(src.bad_ranges)
    res.seconds = time.time() - t0
    res._src = src
    return res
