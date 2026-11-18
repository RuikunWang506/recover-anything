"""Before recovering anything: what is this, what are the odds, what not to do.

Honest odds matter more than optimism. A deleted photo on an SD card is usually
fine. A deleted file on a laptop SSD is usually gone within minutes, because
the drive erases freed space in the background (TRIM), and no software fixes
that. People deserve to hear which case they're in before they spend hours.
"""
from __future__ import annotations

import math
import os
import random
import sys

from . import devices
from .source import Source
from .volume import APFS_UNSURE, ENCRYPTED, find_volumes, open_filesystem

DONTS = [
    "Stop using the card or drive now. Don't take new photos, copy files to it, or let a phone or camera use it.",
    "Don't format it, and don't click 'Scan and fix', 'Repair', 'First Aid', chkdsk or fsck. Repairs write to the "
    "disk and can destroy exactly what you're trying to get back.",
    "Never save recovered files back onto the same card or drive.",
]


def _entropy(b: bytes) -> float:
    n = len(b)
    if not n:
        return 0.0
    counts = [0] * 256
    for x in b:
        counts[x] += 1
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def _jpeg_like(b: bytes) -> bool:
    """A photo's compressed data looks as random as encrypted data, but every
    0xFF in it is followed by 0x00 (stuffing) or a restart marker."""
    ffs = ok = 0
    i = b.find(b"\xff")
    while 0 <= i < len(b) - 1:
        ffs += 1
        if b[i + 1] == 0 or 0xD0 <= b[i + 1] <= 0xD7:
            ok += 1
        i = b.find(b"\xff", i + 1)
    return ffs >= 4 and ok >= 0.9 * ffs


def _sample(src, start: int, end: int, samples: int = 256) -> dict:
    """Random 4 KB samples: how many hold data, and how many of those look
    like random noise (encrypted data does; so does a compressed file's middle)."""
    if end <= start:
        return {"present": 0.0, "noise": 0.0}
    rng = random.Random(7)
    hit = noise = 0
    for _ in range(samples):
        off = start + (rng.randrange((end - start) // 4096) * 4096 if end - start > 4096 else 0)
        b = src.read(off, 4096)
        if b and b.count(0) != len(b) and b.count(0xFF) != len(b):
            hit += 1
            if len(b) == 4096 and _entropy(b) > 7.9 and not _jpeg_like(b):
                noise += 1
    return {"present": hit / samples, "noise": noise / hit if hit else 0.0}


def _erased_after_delete(src, starts: list) -> tuple:
    """(checked, blank): of the deleted files' first clusters, how many are
    blank. A drive that erases freed space (TRIM on an SSD, some USB sticks and
    cards) leaves the names listed and the contents gone."""
    checked = blank = 0
    for off in starts[:64]:
        b = src.read(off, 4096)
        if not b:
            continue
        checked += 1
        if _blank(b):
            blank += 1
    return checked, blank


# Cameras and phones fill a card from the start of its data area, so a
# formatted card with a few hundred photos is blank almost everywhere but there.
HEAD_SPAN = 64 << 20
HEAD_MIN = 256 << 10
# file kinds whose first bytes are distinctive enough to count as a file start
_STRONG = {"jpeg", "isobmff", "png", "gif", "tiff", "riff", "pdf", "zip"}


def _blank(b: bytes) -> bool:
    return not b or b.count(0) == len(b) or b.count(0xFF) == len(b)


def _probe_head(src, lo: int, hi: int, unit: int) -> dict:
    """The start of the data area, read in full (up to HEAD_SPAN), and single
    reads at doubling distances past it: how much of the start holds data, how
    many files start there, and how far into the card data still reaches."""
    from .carve import identify
    unit = max(512, unit or 4096)
    end = min(hi, lo + HEAD_SPAN)
    busy = blocks = starts = 0
    reach = 0
    chunk = 1 << 20
    for off in range(lo, end, chunk):
        blob = src.read(off, min(chunk, end - off))
        if not blob:
            break
        for i in range(0, len(blob), 4096):
            blocks += 1
            if not _blank(blob[i:i + 4096]):
                busy += 1
                reach = off + i + 4096 - lo
        first = (-(off - lo)) % unit
        for i in range(first, len(blob), unit):
            if blob[i] in (0, 0xFF) and _blank(blob[i:i + 16]):
                continue
            if identify(blob[i:i + 1024]) in _STRONG:
                starts += 1
    step = HEAD_SPAN
    while lo + step < hi:
        if not _blank(src.read(lo + step, 65536)):
            reach = max(reach, step + 65536)
        step *= 2
    return {"head": round(busy / blocks, 3) if blocks else 0.0, "head_bytes": busy * 4096, "starts": starts,
            "reach": min(reach, hi - lo)}


def triage(path: str) -> dict:
    info: dict = {"source": path, "warnings": [], "donts": DONTS}
    is_file = os.path.isfile(path)
    info["is_image_file"] = is_file
    dev = None if is_file else devices.describe(path)
    info["device"] = dev
    try:
        src = Source(path)
    except PermissionError:
        info["verdict"] = "need-permission"
        info["odds"] = "unknown"
        info["summary"] = ("I need administrator rights to read this device. On macOS/Linux run the command with "
                           "sudo; on Windows run the terminal as Administrator.")
        return info
    except OSError as e:
        info["verdict"] = "unreadable"
        info["odds"] = "unknown"
        info["summary"] = "The device could not be opened (%s). If it's a failing card, try another reader." % e
        return info
    with src:
        info["size"] = src.size
        vols = find_volumes(src)
        info["volumes"] = []
        live = deleted = trashed = unopenable = 0
        deleted_starts: list = []
        from . import trash
        from .clutter import is_clutter
        for v in vols:
            entry = {"offset": v.offset, "size": v.size, "filesystem": v.fs, "label": v.label, "note": v.note}
            fsr = None
            try:
                fsr = open_filesystem(src, v)
            except Exception:
                fsr = None
            if fsr is not None:
                try:
                    for r in fsr.walk():
                        if is_clutter(r.path, r.name):
                            continue
                        if r.deleted:
                            deleted += 1
                            if r.extents and (r.expected_size or r.extents[0][1]) and len(deleted_starts) < 64:
                                deleted_starts.append(r.extents[0][0])
                        elif trash.bookkeeping(r.path, r.name):
                            continue
                        elif trash.kind(r.path, r.name):
                            trashed += 1
                        elif v.os_cannot_open:
                            unopenable += 1
                        else:
                            live += 1
                except Exception:
                    entry["note"] = (entry["note"] + " folder structure damaged").strip()
                lo = fsr.data_offset
                hi = fsr.data_offset + fsr.cluster_count * fsr.cluster_size
                unit = fsr.cluster_size
            else:
                lo, hi, unit = v.offset, v.offset + v.size, 4096
            smp = _sample(src, lo, hi)
            entry["data_present"] = round(smp["present"], 3)
            entry["looks_random"] = round(smp["noise"], 3)
            entry.update(_probe_head(src, lo, hi, unit))
            if v.fs == "APFS":
                from .fs import apfs
                locked = [n or "unnamed" for n, enc in apfs.volumes(src, v.offset) or [] if enc]
                if locked:
                    info["warnings"].append("Some volumes on this drive are encrypted (%s): deleted files on those "
                                            "can't be recovered without unlocking them." % ", ".join(locked))
            info["volumes"].append(entry)
        info["erased_check"] = dict(zip(("checked", "blank"), _erased_after_delete(src, deleted_starts)))
        info["visible_files"] = live
        info["deleted_entries"] = deleted
        info["in_card_trash"] = trashed
        info["listed_but_unopenable"] = unopenable
        present = max((v["data_present"] for v in info["volumes"]), default=0.0)
        info["data_present"] = present
        # a blank card's own folders and tables hold a few KB; a file holds more
        head = any(v["head_bytes"] >= HEAD_MIN or v["starts"] for v in info["volumes"])
        starts = sum(v["starts"] for v in info["volumes"])
        reach = max((v["reach"] for v in info["volumes"]), default=0)
        fss = {v["filesystem"] for v in info["volumes"]}

    ssd_trim = bool(dev and dev.get("ssd") and dev.get("trim") and not dev.get("removable"))
    mounts = [m for m in (dev or {}).get("mounted_at") or [] if m]
    if mounts:
        how = {"darwin": "diskutil unmountDisk %s" % path.replace("/dev/r", "/dev/"),
               "win32": "don't open it in Explorer while recovering"}.get(sys.platform, "sudo umount %s" % " ".join(mounts))
        info["warnings"].append("The card is mounted (%s), so the computer may still write small files to it "
                                "(search indexes, thumbnails). Unmount it first: %s." % (", ".join(mounts), how))
    if dev and dev.get("system_disk"):
        info["warnings"].append("This is the disk your computer is running from. Every minute it runs, it writes to "
                                "this disk. Recovering from it is risky; the safest move is to shut down and image it "
                                "from another computer, or check backups first.")
    ec = info.get("erased_check") or {}
    erased = ec.get("checked", 0) >= 4 and ec.get("blank", 0) >= 0.75 * ec["checked"]
    vols_ = info.get("volumes") or []
    looks_encrypted = bool(vols_) and all(v["filesystem"] == "unknown" for v in vols_) and \
        present >= 0.9 and all(v.get("looks_random", 0) >= 0.9 and not v.get("starts") for v in vols_)
    if "Android phone storage" in fss:
        info["odds"] = "low"
        info["verdict"] = "encrypted"
        info["summary"] = ("An Android phone formatted this card as its own internal storage ('phone storage'), and "
                           "encrypted it with a key that stays on that phone. On a computer it's unreadable, and "
                           "deleted files on it can't be found. If you still have the phone, put the card back in "
                           "it to see what's there, and check Google Photos' trash and other backups; "
                           "'recover-anything hunt' looks for copies on this computer.")
    elif fss & ENCRYPTED or APFS_UNSURE in fss:
        info["odds"] = "low"
        info["verdict"] = "encrypted"
        what = ("This volume is encrypted" if fss & ENCRYPTED else
                "This drive uses APFS, and I couldn't read whether its volumes are encrypted; most are")
        info["summary"] = ("%s. Deleted data on an encrypted volume can't be carved. Check Time Machine, iCloud, "
                           "OneDrive or other backups, and run 'recover-anything hunt' to look for other copies."
                           % what)
    elif looks_encrypted:
        info["odds"] = "low"
        info["verdict"] = "encrypted"
        info["summary"] = ("Every place I sampled holds what looks like random noise, with no filesystem and no file "
                           "in it: what an encrypted disk looks like (VeraCrypt, or a phone's or computer's own "
                           "encryption). Deleted files can't be found without unlocking it. If you know its password, "
                           "unlock it first and scan the unlocked disk. If you're sure it was never encrypted, run "
                           "'recover' anyway.")
    elif ssd_trim:
        info["odds"] = "low"
        info["verdict"] = "ssd-trim"
        info["summary"] = ("This is an internal SSD with TRIM. When files are deleted, the SSD erases them in the "
                           "background, usually within minutes. Recovery tools rarely find anything. Your best "
                           "chances are backups, the Recycle Bin/Trash, cloud trash, version history and other "
                           "copies: run 'recover-anything hunt'.")
    elif erased:
        info["odds"] = "low"
        info["verdict"] = "erased"
        info["summary"] = ("The drive still lists %d deleted file(s) by name, but where they were stored is now blank "
                           "(%d of the %d I checked): the drive erased them after they were deleted. SSDs, and some "
                           "USB sticks and cards, do this on their own (TRIM). Those files can't come back from this "
                           "drive; check backups, cloud trash and 'recover-anything hunt'." % (
                               deleted, ec["blank"], ec["checked"]))
    elif present < 0.02 and (head or deleted):
        # Random samples alone never say "erased": a camera fills a card from
        # its start, so 64 formatted photos on a 4 GB card miss every sample.
        info["odds"] = "good" if starts or deleted else "fair"
        info["verdict"] = "sparse"
        where = ("the first %s of it" % _size(reach)) if reach else "its start"
        found = (" I found %d file(s) starting there." % starts) if starts else ""
        info["summary"] = ("Most of the card is blank, but data is still there in %s, where a camera or phone "
                           "writes first.%s Deleted or formatted files are likely still there: run 'recover' "
                           "(it only reads the card); only a full scan can tell what comes back." % (where, found))
        if deleted:
            info["summary"] += " The folder structure still lists %d deleted file(s) by name." % deleted
    elif present < 0.02:
        info["odds"] = "low"
        info["verdict"] = "empty"
        info["summary"] = ("Every place I checked is blank, including the start of the card, where a camera or phone "
                           "writes first. The card looks fully erased (a 'full' or 'low-level' format, or a secure "
                           "erase). Run 'recover' anyway: it only reads the card, and only a full scan can be sure. "
                           "Then try 'recover-anything hunt' for other copies.")
    else:
        info["odds"] = "good" if present > 0.15 else "fair"
        info["verdict"] = "recoverable"
        pct = int(round(present * 100))
        info["summary"] = ("Good news: about %d%% of the space I sampled still holds data, so deleted or formatted "
                           "files are very likely still there. Make a copy (image) of it first, then recover from the "
                           "copy." % pct) if present > 0.15 else (
            "About %d%% of the space I sampled still holds data. Some files may still be recoverable." % pct)
        if deleted:
            info["summary"] += " The folder structure still lists %d deleted file(s) by name." % deleted
    ssd_ext = bool(dev and dev.get("ssd") and not ssd_trim)
    if ssd_ext and info.get("verdict") in ("recoverable", "sparse") and ec.get("checked", 0) < 4:
        info["odds"] = "fair"
        info["warnings"].append("This is a solid-state drive. Many erase deleted files soon after they're deleted "
                                "(TRIM), even over USB, so the data I found may be other files. Only a scan will "
                                "tell.")
    n = info.get("in_card_trash")
    if n and info.get("verdict") in ("recoverable", "sparse"):
        info["summary"] += (" %d file(s) deleted on a computer are still whole in the card's hidden trash folder; "
                            "'recover' copies them back." % n)
    elif n and info.get("verdict") in ("empty", "erased"):
        info["odds"] = "good"
        info["verdict"] = "in-card-trash"
        info["summary"] = ("%d file(s) deleted on a computer are still whole in the card's hidden trash folder; "
                           "'recover' copies them back. The rest of the card looks blank, so other deleted files "
                           "may be gone." % n)
    k = info.get("listed_but_unopenable")
    if k and info.get("verdict") in ("recoverable", "empty", "sparse", "in-card-trash"):
        info["odds"] = "good"
        if info["verdict"] in ("empty", "sparse"):
            info["verdict"] = "card-unopenable"
            info["summary"] = ""
        info["summary"] = ("The computer can't open this card because its start is damaged, but %d file(s) are "
                           "still listed on it and look whole: 'recover' copies them all. Don't format it, and "
                           "don't let the computer 'repair' it. %s" % (k, info["summary"])).strip()
    return info


def _size(n: int) -> str:
    return "%.1f GB" % (n / 1e9) if n >= 1e9 else "%d MB" % max(1, round(n / 1e6))


def format_triage(t: dict) -> str:
    lines = []
    dev = t.get("device") or {}
    size = t.get("size", 0)
    what = "Image file" if t.get("is_image_file") else (dev.get("model") or "Disk")
    lines.append("%s: %s (%.1f GB)" % (what, t["source"], size / 1e9))
    for v in t.get("volumes", []):
        lines.append("  %s filesystem%s at offset %d, data present in %d%% of samples%s" % (
            v["filesystem"], (" '%s'" % v["label"]) if v.get("label") else "", v["offset"],
            int(v.get("data_present", 0) * 100), (" - " + v["note"]) if v.get("note") else ""))
        if "head" in v:
            lines.append("    its first %s: %d%% holds data, %d file start(s); data found up to %s in" % (
                _size(min(HEAD_SPAN, v["size"])), int(v["head"] * 100), v["starts"], _size(v["reach"])))
    if "visible_files" in t:
        lines.append("  %d visible files, %d deleted entries still listed" % (t["visible_files"], t["deleted_entries"]))
        if t.get("listed_but_unopenable"):
            lines.append("  %d files still listed, on a card the computer can't open" % t["listed_but_unopenable"])
        if t.get("in_card_trash"):
            lines.append("  %d files in the card's trash folder (deleted on a computer, still whole)" % t["in_card_trash"])
    lines.append("")
    lines.append("Chances: %s" % t.get("odds", "unknown").upper())
    lines.append(t.get("summary", ""))
    for w in t.get("warnings", []):
        lines.append("WARNING: " + w)
    lines.append("")
    lines.append("Before anything else:")
    for d in t.get("donts", []):
        lines.append("  - " + d)
    return "\n".join(lines)
