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


def _refuse(msg: str) -> int:
    print("Refusing: " + msg, file=sys.stderr)
    return 2


def _source_problem(src: str):
    from .devices import is_device_path
    if os.path.isdir(src):
        return ("%s is a folder. Point me at the device itself (run `recover-anything devices`) or at a disk image "
                "file." % src)
    if not is_device_path(src) and not os.path.isfile(src):
        return "not found: %s" % src
    return None


def _output_ok(a, src: str, out: str) -> bool:
    from .devices import DEFINITE, check_output
    ok, why = check_output(src, out)
    if ok:
        return True
    definite = any(k in why for k in DEFINITE)
    if getattr(a, "trust_output_location", False) and not definite:
        print("Warning: %s Continuing because you passed --trust-output-location." % why, file=sys.stderr)
        return True
    print("Refusing to save to %s: %s Saving onto the card or drive you're recovering from would overwrite the very "
          "files you want back. Choose a folder on a different drive%s." % (
              out, why, "" if definite else
              " (or, if you are certain it is a different drive, add --trust-output-location)"), file=sys.stderr)
    return False


def _give_back_to_user(paths, trees=()) -> None:
    """Under sudo, hand what this run created back to the person who ran it.
    Only the given paths are touched (never files that were already there),
    and symlinks are never followed. `trees` are folders this run created
    whole (copied folders), handed back with everything inside."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not hasattr(os, "chown") or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        uid, gid = int(uid), int(gid or uid)
    except ValueError:
        return
    todo = [paths] if isinstance(paths, str) else list(paths)
    for t in trees:
        if os.path.isdir(t) and not os.path.islink(t):
            for root, dirs, files in os.walk(t):
                todo += [os.path.join(root, x) for x in dirs + files]
    for p in todo:
        try:
            os.chown(p, uid, gid, follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass


def cmd_triage(a):
    from .devices import normalize_source
    from .triage import format_triage, triage
    src = normalize_source(a.source)
    problem = _source_problem(src)
    if problem:
        return _refuse(problem)
    t = triage(src)
    if a.json:
        print(json.dumps(t, indent=1, default=str))
    else:
        print(format_triage(t))
    return 0


def cmd_image(a):
    from .devices import normalize_source
    from .imaging import ImageError, make_image
    src = normalize_source(a.source)
    problem = _source_problem(src)
    if problem:
        return _refuse(problem)
    if not _output_ok(a, src, os.path.dirname(os.path.abspath(a.dest)) or "."):
        return 2
    bar = Bar(a.json)
    try:
        res = make_image(src, a.dest, progress=lambda d, t, s: bar("Copying the card", d, t))
    except ImageError as e:
        return _refuse(str(e))
    _give_back_to_user(p for p in (a.dest, a.dest + ".map.json", a.dest + ".ddrescue-map") if os.path.exists(p))
    if a.json:
        print(json.dumps(res))
    else:
        bad = res.get("unreadable_bytes", 0)
        print("Image saved to %s (%s)%s" % (a.dest, _human(res["bytes"]),
                                            ("; %s could not be read and were filled with zeros" % _human(bad))
                                            if bad else ""))
    return 0


def cmd_recover(a):
    from .devices import is_device_path, is_system_disk, normalize_source
    from .imaging import ImageError, identity, make_image, short_id
    from .organize import free_space, makedirs_tracked, plan_size, write_all
    from .pipeline import scan
    from .report import text_summary, write_html
    from .source import Source

    src_path = normalize_source(a.source)
    out = os.path.abspath(a.output)
    problem = _source_problem(src_path)
    if problem:
        return _refuse(problem)
    if not _output_ok(a, src_path, out):
        return 2
    is_dev = is_device_path(src_path)
    if is_dev and is_system_disk(src_path) and not a.i_understand_system_disk:
        print("This is the disk your computer runs from. Recovering from a running system disk is unreliable "
              "and every minute writes over deleted data. Shut down and image it from another computer, or "
              "rerun with --i-understand-system-disk.", file=sys.stderr)
        return 2
    created: list = []
    makedirs_tracked(out, created)
    bar = Bar(a.json)
    work = src_path
    if is_dev and not a.direct:
        with Source(src_path) as s:
            size = s.size
            ident = identity(s) if size else None
        if not size:
            return _refuse("couldn't read the size of %s (is it connected and readable?)" % src_path)
        if free_space(out) > size * 1.05 + (256 << 20):
            base = os.path.join(out, "card-%s" % short_id(ident))
            img, k = base + ".img", 2
            print("Making a safe copy of the card first (%s). Later steps read the copy, not the card." % _human(size),
                  file=sys.stderr)
            while True:
                try:
                    make_image(src_path, img, progress=lambda dd, t, s2: bar("Copying the card", dd, t))
                    created += [p for p in (img, img + ".map.json", img + ".ddrescue-map") if os.path.exists(p)]
                    break
                except ImageError as e:
                    if "already exists" not in str(e) or k > 9:
                        return _refuse(str(e))
                    img, k = "%s-%d.img" % (base, k), k + 1  # a different card that looks alike: new copy
            work = img
        else:
            print("Not enough free space to copy the whole card first, so I'll read it directly (still read-only).",
                  file=sys.stderr)
    kinds = set(a.only.split(",")) if a.only else None
    if kinds:
        kinds = {{"photos": "photo", "videos": "video", "documents": "document"}.get(k, k) for k in kinds}
    res = scan(work, kinds=kinds, include_live=a.include_live, reassemble=not a.no_reassemble,
               workers=a.workers, progress=bar, fast=a.fast, deep=a.deep)
    res.source = src_path
    res.image = work if work != src_path else None
    need = plan_size(res.found)
    if need > free_space(out):
        print("Not enough space in %s: need about %s. Free some space or choose another drive." % (out, _human(need)),
              file=sys.stderr)
        return 3
    bar("Saving files")
    w = write_all(res, out, progress=lambda dd, t: bar("write", dd, t), created=created)
    report = write_html(res, w["written"], out)
    created.append(report)
    _give_back_to_user(created)
    if a.json:
        print(json.dumps({"output": out, "report": report, "manifest": os.path.join(out, "recovery.json"),
                          "files": len(w["written"]), "failed_to_save": len(w.get("failed", [])),
                          "seconds": round(res.seconds, 1), "image": res.image,
                          "visible_files_not_copied": getattr(res, "live_count", 0) if not a.include_live else 0,
                          "warnings": res.warnings}, indent=1))
    else:
        print()
        print(text_summary(res, w["written"], out))
        for path, why in w.get("failed", []):
            print("Could not save %s: %s" % (path, why))
        if res.image:
            print("A full copy of the card is in %s. Keep it until you've checked your files; it lets you try "
                  "again without touching the card." % res.image)
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
        from .hunt import SaveError
        made: list = []
        try:
            files = save(leads, a.save, a.project, created=made)
        except SaveError as e:
            return _refuse(str(e))
        # copied folders were created whole by this run, so everything in them is handed back
        _give_back_to_user(made + files, trees=[f for f in files if os.path.isdir(f)])
        print("\nSaved %d copies to %s (originals untouched)." % (len(files), a.save), file=sys.stderr)
    return 0


def cmd_selftest(a):
    from .selftest import run, stress
    if a.stress:
        return stress(a.stress, keep=a.keep, as_json=a.json)
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
    s.add_argument("--trust-output-location", action="store_true", help=argparse.SUPPRESS)
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
    s.add_argument("--trust-output-location", action="store_true",
                   help="only if the safety check can't identify the output drive and you are SURE it is not the card")
    s.set_defaults(fn=cmd_recover)

    s = sub.add_parser("hunt", help="look for other copies: trash, git, editor history, agent logs, snapshots")
    s.add_argument("query", nargs="?", help="file name, pattern (*.psd) or text inside the file")
    s.add_argument("--project", help="only look for files that lived under this folder")
    s.add_argument("--save", help="copy everything restorable into this folder")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_hunt)

    s = sub.add_parser("selftest", help="build simulated SD cards, wreck them, and measure recovery")
    s.add_argument("--quick", action="store_true")
    s.add_argument("--stress", type=int, metavar="N", help="run N randomized multi-round cards instead (the README's stress numbers)")
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
