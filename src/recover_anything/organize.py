"""Write recovered files somewhere safe, named and sorted the way people expect."""
from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import json
import os
import re
import shutil

from .carve import GROUP
from .exif import capture_time
from .validate import ExtentReader

FOLDER = {"photo": "Photos", "video": "Videos", "document": "Documents"}
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def _safe(name: str, max_bytes: int = 200) -> str:
    name = _BAD.sub("_", name).strip(" .") or "file"
    stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    ext = ext[:16]
    while len((stem + ext).encode("utf-8")) > max_bytes and stem:
        stem = stem[:-1]
    return (stem or "file") + ext


def makedirs_tracked(path: str, created: list | None) -> None:
    """os.makedirs, noting every folder it actually created."""
    missing, p = [], os.path.abspath(path)
    while not os.path.isdir(p):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(path, exist_ok=True)
    if created is not None:
        created.extend(reversed(missing))


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def _write_one(reader, r, folder, name, seen):
    dest = _unique(os.path.join(folder, _safe(name)))
    h = hashlib.sha256()
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as f:
            pos, step = 0, 8 << 20
            while pos < reader.length:
                chunk = reader.read(pos, step)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                pos += len(chunk)
            if r.kind == "jpeg" and r.status == "partial":
                f.write(b"\xff\xd9")  # lets viewers show the part that exists
                h.update(b"\xff\xd9")
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    digest = h.hexdigest()
    if digest in seen:
        os.remove(tmp)
        return "duplicate", seen[digest]
    os.replace(tmp, dest)
    return dest, digest


def plan_size(found) -> int:
    return sum(r.size for r in found if r.status in ("ok", "partial", "unverified", "corrupt"))


def write_all(res, out_dir: str, progress=None, keep_partial: bool = True, created: list | None = None) -> dict:
    """Save every recovered file. Paths this creates (folders and files) are
    appended to `created`, so ownership can be handed back for exactly those."""
    src = res._src
    created = [] if created is None else created
    makedirs_tracked(out_dir, created)
    seen: dict[str, str] = {}
    written = []
    rank = {"ok": 0, "unverified": 1, "partial": 2, "corrupt": 3}
    failed = []
    todo = [r for r in res.found if r.status in rank and r.extents and (keep_partial or r.status != "partial")]
    todo.sort(key=lambda r: (rank[r.status], r.name is None, r.start))
    for i, r in enumerate(todo):
        reader = ExtentReader(src, r.extents)
        when = capture_time(r.kind, reader.read, reader.length) if r.kind != "other" else None
        when = when or r.fs_time
        group = GROUP.get(r.kind, "other")
        if r.status in ("partial", "corrupt"):
            top = "Partly recovered"
        elif r.status == "unverified" and r.kind == "other":
            top = "Other files"
        else:
            top = FOLDER.get(group, "Other files")
        sub = when.strftime("%Y-%m") if (when and top in ("Photos", "Videos")) else ""
        ext = r.ext or ""
        if r.name:
            name = r.name
            if r.kind == "jpeg" and not name.lower().endswith((".jpg", ".jpeg", ".jpe", ".jfif")):
                name += ".jpg"
        elif when:
            label = {"photo": "Photo", "video": "Video"}.get(group, "File")
            name = "%s %s%s" % (label, when.strftime("%Y-%m-%d %H.%M.%S"), ext)
        else:
            name = "Recovered %010d%s" % (r.start // 512, ext)
        folder = os.path.join(out_dir, top, sub) if sub else os.path.join(out_dir, top)
        fallback = "Recovered %010d%s" % (r.start // 512, _safe(ext, 16) if ext else "")
        dest = digest = None
        for attempt, nm in enumerate((name, fallback)):
            try:
                makedirs_tracked(folder, created)
                dest, digest = _write_one(reader, r, folder, nm, seen)
                break
            except OSError as e:
                if attempt == 1 or getattr(e, "errno", None) in (errno.EFBIG, errno.ENOSPC):
                    why = ("too big for the output drive's filesystem (FAT32 drives can't hold files over 4 GB)"
                           if getattr(e, "errno", None) == errno.EFBIG else str(e))
                    failed.append((os.path.join(top, nm), why))
                    dest = None
                    break
        if dest is None:
            continue
        if dest == "duplicate":
            r.meta["duplicate_of"] = digest
            continue
        seen[digest] = dest
        created.append(dest)
        if when:
            try:
                ts = when.timestamp()
                os.utime(dest, (ts, ts))
            except (OverflowError, OSError, ValueError):
                pass
        r.meta["written_to"] = dest
        r.meta["sha256"] = digest
        r.meta["when"] = when.isoformat() if when else None
        written.append(r)
        if progress:
            progress(i + 1, len(todo))
    manifest = {
        "tool": "recover-anything",
        "source": res.source,
        "image": getattr(res, "image", None),
        "source_size": res.size,
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "volumes": [{"offset": v.offset, "size": v.size, "filesystem": v.fs, "note": v.note} for v in res.volumes],
        "warnings": res.warnings,
        "files": [
            {
                "file": os.path.relpath(r.meta["written_to"], out_dir),
                "status": r.status,
                "how": r.origin,
                "original_name": r.name,
                "original_folder": r.path,
                "type": r.kind,
                "bytes": r.size,
                "sha256": r.meta.get("sha256"),
                "taken": r.meta.get("when"),
                "pieces": len(r.extents),
                "source_offsets": [[o, n] for o, n in r.extents],
                "note": r.note,
            }
            for r in written
        ],
        "not_recoverable": [
            {"original_name": r.name, "original_folder": r.path, "reason": r.note}
            for r in res.found if r.status == "lost" and r.name
        ],
    }
    manifest["could_not_save"] = [{"file": f, "reason": why} for f, why in failed]
    with open(os.path.join(out_dir, "recovery.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    created.append(os.path.join(out_dir, "recovery.json"))
    return {"written": written, "manifest": manifest, "failed": failed, "created": created}


def free_space(path: str) -> int:
    p = os.path.abspath(path)
    while not os.path.exists(p):
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free
