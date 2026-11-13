"""Before recovering anything: what is this, what are the odds, what not to do.

Honest odds matter more than optimism. A deleted photo on an SD card is usually
fine. A deleted file on a laptop SSD is usually gone within minutes, because
the drive erases freed space in the background (TRIM), and no software fixes
that. People deserve to hear which case they're in before they spend hours.
"""
from __future__ import annotations

import os
import random
import sys

from . import devices
from .source import Source
from .volume import ENCRYPTED, find_volumes, open_filesystem

DONTS = [
    "Stop using the card or drive now. Don't take new photos, copy files to it, or let a phone or camera use it.",
    "Don't format it, and don't click 'Scan and fix', 'Repair', 'First Aid', chkdsk or fsck. Repairs write to the "
    "disk and can destroy exactly what you're trying to get back.",
    "Never save recovered files back onto the same card or drive.",
]


def _sample_nonempty(src, start: int, end: int, samples: int = 256) -> float:
    if end <= start:
        return 0.0
    rng = random.Random(7)
    hit = 0
    for _ in range(samples):
        off = start + (rng.randrange((end - start) // 4096) * 4096 if end - start > 4096 else 0)
        b = src.read(off, 4096)
        if b and b.count(0) != len(b) and b.count(0xFF) != len(b):
            hit += 1
    return hit / samples


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
        live = deleted = trashed = 0
        from . import trash
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
                        if r.deleted:
                            deleted += 1
                        elif trash.bookkeeping(r.path, r.name):
                            continue
                        elif trash.kind(r.path, r.name):
                            trashed += 1
                        else:
                            live += 1
                except Exception:
                    entry["note"] = (entry["note"] + " folder structure damaged").strip()
                lo = fsr.data_offset
                hi = fsr.data_offset + fsr.cluster_count * fsr.cluster_size
            else:
                lo, hi = v.offset, v.offset + v.size
            entry["data_present"] = round(_sample_nonempty(src, lo, hi), 3)
            info["volumes"].append(entry)
        info["visible_files"] = live
        info["deleted_entries"] = deleted
        info["in_card_trash"] = trashed
        present = max((v["data_present"] for v in info["volumes"]), default=0.0)
        info["data_present"] = present
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
    if fss & ENCRYPTED or "APFS" in fss:
        info["odds"] = "low"
        info["verdict"] = "encrypted"
        info["summary"] = ("This volume is encrypted (or uses APFS, which usually is). Deleted data on it cannot be "
                           "carved. Check Time Machine, iCloud, OneDrive or other backups, and run "
                           "'recover-anything hunt' to look for other copies.")
    elif ssd_trim:
        info["odds"] = "low"
        info["verdict"] = "ssd-trim"
        info["summary"] = ("This is an internal SSD with TRIM. When files are deleted, the SSD erases them in the "
                           "background, usually within minutes. Recovery tools rarely find anything. Your best "
                           "chances are backups, the Recycle Bin/Trash, cloud trash, version history and other "
                           "copies: run 'recover-anything hunt'.")
    elif present < 0.02:
        info["odds"] = "low"
        info["verdict"] = "empty"
        info["summary"] = ("Almost every place I sampled is blank. The card looks fully erased (a 'full' or "
                           "'low-level' format, or a secure erase). There may be nothing left to recover; a full "
                           "scan will confirm.")
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
    n = info.get("in_card_trash")
    if n and info.get("verdict") == "recoverable":
        info["summary"] += (" %d file(s) deleted on a computer are still whole in the card's hidden trash folder; "
                            "'recover' copies them back." % n)
    elif n and info.get("verdict") == "empty":
        info["odds"] = "good"
        info["verdict"] = "in-card-trash"
        info["summary"] = ("%d file(s) deleted on a computer are still whole in the card's hidden trash folder; "
                           "'recover' copies them back. The rest of the card looks blank, so other deleted files "
                           "may be gone." % n)
    return info


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
    if "visible_files" in t:
        lines.append("  %d visible files, %d deleted entries still listed" % (t["visible_files"], t["deleted_entries"]))
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
