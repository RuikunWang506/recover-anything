"""Command line: recover-anything <devices|triage|image|recover|hunt|selftest|install-skill>."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from . import __version__


def _human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0
    return str(n)


class Bar:
    """Single-line progress on stderr; silent when not a terminal or in --json mode."""

    def __init__(self, quiet=False):
        self.quiet = quiet or not sys.stderr.isatty()
        self.stage = None
        self.last = 0.0

    def __call__(self, stage, done=None, total=None):
        names = {"scan": "Searching the card", "check": "Checking files", "reassemble": "Rebuilding fragments",
                 "write": "Saving files"}
        label = names.get(stage, stage)
        if self.quiet:
            if stage not in names and not self.quiet_logged(stage):
                print("- " + label, file=sys.stderr)
            return
        now = time.time()
        if done is not None and total and now - self.last < 0.2 and done < total:
            return
        self.last = now
        pct = " %3d%%" % (100 * done // total) if done is not None and total else ""
        sys.stderr.write("\r\033[K%s%s" % (label, pct))
        if done is None or (total and done >= total):
            sys.stderr.write("\n")
        sys.stderr.flush()

    _logged: set = set()

    def quiet_logged(self, stage):
        if stage in self._logged:
            return True
        self._logged.add(stage)
        return False


# --------------------------------------------------------------- commands
def cmd_devices(a):
    from .devices import list_devices
    devs = list_devices()
    if a.json:
        print(json.dumps(devs, indent=1))
        return 0
    if not devs:
        print("No disks found (or this system doesn't let me list them). You can still pass a device path or an "
              "image file directly.")
        return 0
    print("%-24s %10s  %-6s %-5s %-26s %s" % ("DEVICE", "SIZE", "BUS", "TYPE", "MODEL", "MOUNTED AT"))
    for d in devs:
        kind = "SSD" if d.get("ssd") and not d.get("removable") else ("card" if d.get("removable") else "disk")
        flag = "  <- your system disk" if d.get("system_disk") else ""
        print("%-24s %10s  %-6s %-5s %-26s %s%s" % (d["path"], _human(d["size"]), d.get("bus", "")[:6], kind,
                                                    (d.get("model") or "")[:26], ", ".join(d.get("mounted_at") or []),
                                                    flag))
    print("\nMemory cards usually show up as 'card' with a size matching the card.")
    return 0


def cmd_triage(a):
    from .triage import format_triage, triage
    t = triage(a.source)
    if a.json:
        print(json.dumps(t, indent=1, default=str))
    else:
        print(format_triage(t))
    return 0


def cmd_image(a):
    from .devices import output_is_on_source
    from .imaging import make_image
    if output_is_on_source(a.source, os.path.dirname(os.path.abspath(a.dest)) or "."):
        print("Refusing: the image would be saved on the card you're recovering from.", file=sys.stderr)
        return 2
    bar = Bar(a.json)
    res = make_image(a.source, a.dest, progress=lambda d, t, s: bar("Copying the card", d, t))
    if a.json:
        print(json.dumps(res))
    else:
        bad = res.get("unreadable_bytes", 0)
        print("Image saved to %s (%s)%s" % (a.dest, _human(res["bytes"]),
                                            ("; %s could not be read and were filled with zeros" % _human(bad))
                                            if bad else ""))
    return 0


def cmd_recover(a):
    from .devices import describe, output_is_on_source
    from .imaging import make_image
    from .organize import free_space, plan_size, write_all
    from .pipeline import scan
    from .report import text_summary, write_html
    from .source import Source, is_block_device

    src_path = a.source
    out = os.path.abspath(a.output)
    if not os.path.exists(src_path) and not src_path.startswith("\\\\"):
        print("Not found: %s" % src_path, file=sys.stderr)
        return 2
    if output_is_on_source(src_path, out):
        print("Refusing: %s is on the card/drive you're recovering from. Saving there would overwrite the very "
              "files you want back. Pick a folder on a different drive." % out, file=sys.stderr)
        return 2
    os.makedirs(out, exist_ok=True)
    bar = Bar(a.json)
    is_dev = is_block_device(src_path)
    if is_dev:
        d = describe(src_path) or {}
        if d.get("system_disk") and not a.i_understand_system_disk:
            print("This is the disk your computer runs from. Recovering from a running system disk is unreliable "
                  "and every minute writes over deleted data. Shut down and image it from another computer, or "
                  "rerun with --i-understand-system-disk.", file=sys.stderr)
            return 2
    work = src_path
    if is_dev and not a.direct:
        with Source(src_path) as s:
            size = s.size
        if free_space(out) > size * 1.05 + (256 << 20):
            img = os.path.join(out, "card-image.img")
            print("Making a safe copy of the card first (%s). Later steps read the copy, not the card." % _human(size),
                  file=sys.stderr)
            make_image(src_path, img, progress=lambda dd, t, s2: bar("Copying the card", dd, t))
            work = img
        else:
            print("Not enough free space to copy the whole card first, so I'll read it directly (still read-only).",
                  file=sys.stderr)
    kinds = set(a.only.split(",")) if a.only else None
    if kinds:
        kinds = {{"photos": "photo", "videos": "video", "documents": "document"}.get(k, k) for k in kinds}
    res = scan(work, kinds=kinds, include_live=a.include_live, reassemble=not a.no_reassemble,
               workers=a.workers, progress=bar, fast=a.fast, deep=a.deep)
    need = plan_size(res.found)
    if need > free_space(out):
        print("Not enough space in %s: need about %s. Free some space or choose another drive." % (out, _human(need)),
              file=sys.stderr)
        return 3
    bar("Saving files")
    w = write_all(res, out, progress=lambda dd, t: bar("write", dd, t))
    report = write_html(res, w["written"], out)
    if a.json:
        print(json.dumps({"output": out, "report": report, "manifest": os.path.join(out, "recovery.json"),
                          "files": len(w["written"]), "seconds": round(res.seconds, 1),
                          "visible_files_not_copied": getattr(res, "live_count", 0) if not a.include_live else 0,
                          "warnings": res.warnings}, indent=1))
    else:
        print()
        print(text_summary(res, w["written"], out))
        if work != src_path:
            print("A full copy of the card is in %s. Keep it until you've checked your files; it lets you try "
                  "again without touching the card." % work)
        live = getattr(res, "live_count", 0)
        if live and not a.include_live:
            print("(%d files are still visible on the card and were not copied; add --include-live to copy them too.)"
                  % live)
    return 0


def cmd_hunt(a):
    from .hunt import CLOUD, hunt, save
    leads = hunt(a.query, a.project)
    if a.json:
        print(json.dumps([{"where": l.where, "original": l.original, "detail": l.detail,
                           "when": l.when.isoformat() if l.when else None, "size": l.size,
                           "restorable": l.restorable, "complete": l.complete, "how": l.how} for l in leads],
                         indent=1))
    else:
        if not leads:
            print("No copies found on this computer%s." % (" for '%s'" % a.query if a.query else ""))
        for i, l in enumerate(leads, 1):
            when = l.when.strftime("%Y-%m-%d %H:%M") if l.when else ""
            mark = "+" if l.restorable else "?"
            print("%s [%d] %-22s %s" % (mark, i, l.where, l.original))
            print("      %s%s%s" % (l.detail, ("  " + when) if when else "",
                                    ("  (" + _human(l.size) + ")") if l.size else ""))
            if l.how and not l.restorable:
                print("      how: %s" % l.how)
        print("\nAlso check (I can't see these from here):")
        for name, url, why in CLOUD:
            print("  - %s: %s%s" % (name, why, (" " + url) if url else ""))
    if a.save and leads:
        files = save(leads, a.save)
        print("\nSaved %d copies to %s (originals untouched)." % (len(files), a.save), file=sys.stderr)
    return 0


def cmd_selftest(a):
    from .selftest import run
    return run(quick=a.quick, keep=a.keep, as_json=a.json)


SKILL_TARGETS = {
    "claude": os.path.join("~", ".claude", "skills", "recover-anything"),
    "codex": os.path.join("~", ".codex", "skills", "recover-anything"),
}


def cmd_install_skill(a):
    here = os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")
    targets = SKILL_TARGETS if a.agent == "all" else {a.agent: SKILL_TARGETS[a.agent]}
    if a.dest:
        targets = {"custom": a.dest}
    for name, t in targets.items():
        d = os.path.expanduser(t)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(here, os.path.join(d, "SKILL.md"))
        print("Installed for %s: %s" % (name, os.path.join(d, "SKILL.md")))
    print("Restart your agent, then ask it: \"I deleted photos from my SD card, can you get them back?\"")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="recover-anything",
                                description="Get back deleted and formatted photos, videos and files, safely. "
                                            "Never writes to the card.")
    p.add_argument("--version", action="version", version="recover-anything " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("devices", help="list disks and memory cards")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("triage", help="what is this, what are the odds, what not to do")
    s.add_argument("source", help="device (e.g. /dev/sdb, /dev/rdisk4, \\\\.\\PhysicalDrive2) or image file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_triage)

    s = sub.add_parser("image", help="make a safe copy (image) of a card or disk")
    s.add_argument("source")
    s.add_argument("dest")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_image)

    s = sub.add_parser("recover", help="find and save everything recoverable")
    s.add_argument("source")
    s.add_argument("-o", "--output", required=True, help="folder on a DIFFERENT drive")
    s.add_argument("--only", help="photos,videos,documents")
    s.add_argument("--include-live", action="store_true", help="also copy files that are still visible")
    s.add_argument("--direct", action="store_true", help="read the device directly instead of imaging it first")
    s.add_argument("--no-reassemble", action="store_true", help="skip rebuilding fragmented photos")
    s.add_argument("--fast", action="store_true",
                   help="check photos by structure instead of decoding every block (for very large cards)")
    s.add_argument("--deep", action="store_true", help="always decode every block of every photo, however long it takes")
    s.add_argument("--workers", type=int, default=None)
    s.add_argument("--json", action="store_true")
    s.add_argument("--i-understand-system-disk", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_recover)

    s = sub.add_parser("hunt", help="look for other copies: trash, git, editor history, agent logs, snapshots")
    s.add_argument("query", nargs="?", help="file name, pattern (*.psd) or text inside the file")
    s.add_argument("--project", help="only look for files that lived under this folder")
    s.add_argument("--save", help="copy everything restorable into this folder")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_hunt)

    s = sub.add_parser("selftest", help="build simulated SD cards, wreck them, and measure recovery")
    s.add_argument("--quick", action="store_true")
    s.add_argument("--keep", help="keep the simulated cards in this folder")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("install-skill", help="teach your AI agent (Claude Code, Codex, ...) to use this tool")
    s.add_argument("--agent", choices=["claude", "codex", "all"], default="all")
    s.add_argument("--dest", help="custom skills folder")
    s.set_defaults(fn=cmd_install_skill)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 1
    try:
        return a.fn(a)
    except PermissionError as e:
        print("Permission denied: %s" % e, file=sys.stderr)
        src = getattr(a, "source", "") or ""
        if src and (src.startswith("\\\\.\\") or src.startswith("/dev/")):
            print("Reading a disk directly needs administrator rights: use sudo on macOS/Linux, or an "
                  "Administrator terminal on Windows.", file=sys.stderr)
        return 13
    except KeyboardInterrupt:
        print("\nStopped. Nothing was written to the source.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
