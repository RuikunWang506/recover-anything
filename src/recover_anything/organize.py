"""Write recovered files somewhere safe, named and sorted the way people expect."""
from __future__ import annotations

import datetime as _dt
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


def _safe(name: str) -> str:
    name = _BAD.sub("_", name).strip(" .")
    return name[:180] or "file"


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def plan_size(found) -> int:
    return sum(r.size for r in found if r.status in ("ok", "partial", "unverified"))


def write_all(res, out_dir: str, progress=None, keep_partial: bool = True) -> dict:
    src = res._src
    os.makedirs(out_dir, exist_ok=True)
    seen: dict[str, str] = {}
    written = []
    rank = {"ok": 0, "unverified": 1, "partial": 2}
    todo = [r for r in res.found if r.status in rank and r.extents and (keep_partial or r.status != "partial")]
    todo.sort(key=lambda r: (rank[r.status], r.name is None, r.start))
    for i, r in enumerate(todo):
        reader = ExtentReader(src, r.extents)
        when = capture_time(r.kind, reader.read, reader.length) if r.kind != "other" else None
        when = when or r.fs_time
        group = GROUP.get(r.kind, "other")
        if r.status == "partial":
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
        os.makedirs(folder, exist_ok=True)
        dest = _unique(os.path.join(folder, _safe(name)))
        h = hashlib.sha256()
        tmp = dest + ".part"
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
        digest = h.hexdigest()
        if digest in seen:
            os.remove(tmp)
            r.meta["duplicate_of"] = seen[digest]
            continue
        os.replace(tmp, dest)
        seen[digest] = dest
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
            for r in res.found if r.status in ("lost", "corrupt") and r.name
        ],
    }
    with open(os.path.join(out_dir, "recovery.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return {"written": written, "manifest": manifest}


def free_space(path: str) -> int:
    p = os.path.abspath(path)
    while not os.path.exists(p):
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free
