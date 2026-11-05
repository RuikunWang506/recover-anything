"""Hunt: look everywhere else a lost file might still exist.

Carving only works when the deleted bytes are still on the disk. On SSDs,
phones and encrypted drives they usually aren't. But copies hide in a dozen
places: the trash, git's object store, your editor's local history, the logs of
the AI agent that deleted it (which hold every file it read or wrote), system
snapshots and thumbnail caches. This module checks them all, read-only.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import glob
import json
import os
import re
import struct
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field

def _real_home() -> str:
    """Under sudo, look in the invoking user's home, not root's."""
    user = os.environ.get("SUDO_USER")
    if user and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            import pwd
            return pwd.getpwnam(user).pw_dir
        except (ImportError, KeyError):
            pass
    return os.path.expanduser("~")


HOME = _real_home()


@dataclass
class Lead:
    where: str                 # 'Trash', 'Git', 'Editor history', 'Claude Code log', ...
    original: str              # the path or name it had
    detail: str                # human explanation
    when: _dt.datetime | None = None
    size: int | None = None
    content: bytes | None = None    # recovered content, when we have it in hand
    copy_from: str | None = None    # or a file we can copy
    how: str = ""              # instructions when we can't restore it ourselves
    complete: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def restorable(self) -> bool:
        return self.content is not None or self.copy_from is not None


def _match(query: str | None, path: str, project: str | None = None) -> bool:
    if project:
        try:
            p = os.path.realpath(path)
            if not (p == os.path.realpath(project) or p.startswith(os.path.realpath(project).rstrip(os.sep) + os.sep)):
                return False
        except (OSError, ValueError):
            return False
    if not query:
        return True
    q = query.lower()
    base = os.path.basename(path).lower()
    if any(ch in q for ch in "*?["):
        return fnmatch.fnmatch(base, q) or fnmatch.fnmatch(path.lower(), q)
    return q in path.lower()


def _ts(v) -> _dt.datetime | None:
    try:
        if isinstance(v, str):
            return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        if isinstance(v, (int, float)):
            return _dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v)
    except (ValueError, OSError, OverflowError):
        return None
    return None


# ----------------------------------------------------------------- trash
def trash(query, project=None) -> list[Lead]:
    leads = []
    if sys.platform.startswith("linux"):
        base = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")), "Trash")
        for info in glob.glob(os.path.join(base, "info", "*.trashinfo")):
            try:
                txt = open(info, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"^Path=(.*)$", txt, re.M)
            d = re.search(r"^DeletionDate=(.*)$", txt, re.M)
            orig = urllib.parse.unquote(m.group(1)) if m else ""
            f = os.path.join(base, "files", os.path.basename(info)[:-10])
            if orig and _match(query, orig, project) and os.path.exists(f):
                leads.append(Lead("Trash", orig, "In the trash", _ts(d.group(1)) if d else None,
                                  os.path.getsize(f) if os.path.isfile(f) else None, copy_from=f))
    elif sys.platform == "darwin":
        for f in glob.glob(os.path.join(HOME, ".Trash", "*")) + glob.glob("/Volumes/*/.Trashes/%d/*" % os.getuid()):
            if _match(query, f, None):
                leads.append(Lead("Trash", os.path.basename(f), "In the Trash (original folder not recorded)",
                                  _dt.datetime.fromtimestamp(os.path.getmtime(f)), copy_from=f))
    elif sys.platform == "win32":
        for drive in "CDEFGHIJ":
            for meta in glob.glob(drive + r":\$Recycle.Bin\*\$I*"):
                try:
                    raw = open(meta, "rb").read()
                    ver = struct.unpack("<Q", raw[:8])[0]
                    size = struct.unpack("<Q", raw[8:16])[0]
                    ft = struct.unpack("<Q", raw[16:24])[0]
                    if ver == 2:
                        n = struct.unpack("<I", raw[24:28])[0]
                        orig = raw[28:28 + 2 * n].decode("utf-16-le").rstrip("\x00")
                    else:
                        orig = raw[24:24 + 520].decode("utf-16-le").split("\x00")[0]
                except (OSError, struct.error, UnicodeDecodeError):
                    continue
                content = meta.replace("$I", "$R", 1)
                when = _dt.datetime(1601, 1, 1) + _dt.timedelta(microseconds=ft // 10) if ft else None
                if _match(query, orig, project) and os.path.exists(content):
                    leads.append(Lead("Recycle Bin", orig, "In the Recycle Bin", when, size, copy_from=content))
    return leads


# ------------------------------------------------------------------- git
def _git(repo, *args, binary=False):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return b"" if binary else ""
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def git(query, project=None) -> list[Lead]:
    repo = project or os.getcwd()
    top = _git(repo, "rev-parse", "--show-toplevel").strip()
    if not top:
        return []
    leads = []
    # Files that existed in any commit reachable from branches or the reflog
    # (including commits lost to reset --hard or a rebase).
    seen = set()
    log = _git(top, "log", "--all", "--reflog", "--format=@%H %ct", "--name-only", "--diff-filter=AMD")
    cur = None
    for line in log.splitlines():
        if line.startswith("@"):
            h, ct = line[1:].split()
            cur = (h, int(ct))
            continue
        if not line or not cur:
            continue
        path = line.strip()
        full = os.path.join(top, path)
        if path in seen or not _match(query, full, None):
            continue
        if os.path.exists(full) and not query:
            continue
        # newest version that still has content
        for spec in ("%s:%s" % (cur[0], path), "%s^:%s" % (cur[0], path)):
            blob = _git(top, "show", spec, binary=True)
            if blob:
                seen.add(path)
                state = "still exists" if os.path.exists(full) else "deleted from the folder"
                leads.append(Lead("Git history", full, "Committed in %s (%s)" % (cur[0][:10], state),
                                  _dt.datetime.fromtimestamp(cur[1]), len(blob), content=blob,
                                  how="git show %s > \"%s\"" % (spec, path)))
                break
    # Staged-but-never-committed content survives as dangling blobs.
    fsck = _git(top, "fsck", "--unreachable", "--no-reflogs")
    blobs = [ln.split()[2] for ln in fsck.splitlines() if ln.startswith("unreachable blob")]
    stashes = _git(top, "stash", "list").strip().splitlines()
    for i, s in enumerate(stashes):
        leads.append(Lead("Git stash", top, "%s (may hold uncommitted changes)" % s, how="git stash show -p stash@{%d}" % i,
                          complete=False))
    q = (query or "").strip("*")
    if blobs and q:
        shown = 0
        for b in blobs[:3000]:
            data = _git(top, "cat-file", "-p", b, binary=True)
            if q.encode() not in data:
                continue
            shown += 1
            leads.append(Lead("Git lost content", "%s (blob %s)" % (top, b[:10]),
                              "Content that was 'git add'-ed but never committed", None, len(data), content=data,
                              how="git cat-file -p %s" % b))
            if shown >= 50:
                break
    if blobs and not q:
        leads.append(Lead("Git lost content", top, "%d unreachable blobs (staged content that was never "
                          "committed). Search them with: recover-anything hunt 'some text from the file'" % len(blobs),
                          how="git fsck --unreachable --no-reflogs", complete=False))
    return leads


# ------------------------------------------------------- editor history
_EDITORS = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Trae", "Kiro"]


def _editor_roots():
    if sys.platform == "darwin":
        base = os.path.join(HOME, "Library", "Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA", "")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config"))
    return [(e, os.path.join(base, e, "User", "History")) for e in _EDITORS]


def editor_history(query, project=None) -> list[Lead]:
    leads = []
    for editor, root in _editor_roots():
        for ej in glob.glob(os.path.join(root, "*", "entries.json")):
            try:
                meta = json.load(open(ej, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            res = meta.get("resource", "")
            path = urllib.parse.unquote(urllib.parse.urlparse(res).path) if res.startswith("file:") else res
            if sys.platform == "win32" and re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            if not _match(query, path, project):
                continue
            entries = sorted(meta.get("entries", []), key=lambda e: e.get("timestamp", 0))
            if not entries:
                continue
            last = entries[-1]
            f = os.path.join(os.path.dirname(ej), last.get("id", ""))
            if os.path.isfile(f):
                state = "still exists" if os.path.exists(path) else "deleted"
                leads.append(Lead("%s local history" % editor, path, "%d saved versions (%s)" % (len(entries), state),
                                  _ts(last.get("timestamp")), os.path.getsize(f), copy_from=f,
                                  extra={"versions": len(entries)}))
    jb = {"darwin": os.path.join(HOME, "Library", "Caches", "JetBrains"),
          "win32": os.path.join(os.environ.get("LOCALAPPDATA", ""), "JetBrains")}.get(
        sys.platform, os.path.join(HOME, ".cache", "JetBrains"))
    for lh in glob.glob(os.path.join(jb, "*", "LocalHistory")):
        leads.append(Lead("JetBrains local history", lh, "Open the project, right-click the folder, "
                          "Local History > Show History", complete=False,
                          how="IDE: right-click folder > Local History > Show History"))
    return leads


# -------------------------------------------------------- agent logs
def _strip_line_numbers(text: str) -> str:
    out = []
    for line in text.split("\n"):
        m = re.match(r"^\s*\d+(\u2192|\t)(.*)$", line)
        out.append(m.group(2) if m else line)
    return "\n".join(out)


def claude_code(query, project=None) -> list[Lead]:
    """Rebuild files from Claude Code transcripts: every Write, every Edit, and
    every full Read is in ~/.claude/projects/*/*.jsonl."""
    roots = [os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude")), "projects")]
    files: dict[str, dict] = {}
    for root in roots:
        for jl in sorted(glob.glob(os.path.join(root, "*", "*.jsonl")) + glob.glob(os.path.join(root, "*", "*", "*.jsonl"))):
            try:
                fh = open(jl, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"Write"' not in line and '"Edit"' not in line and '"MultiEdit"' not in line \
                            and '"filePath"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    when = _ts(d.get("timestamp"))
                    msg = d.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict) or c.get("type") != "tool_use":
                                continue
                            inp = c.get("input") or {}
                            fp = inp.get("file_path")
                            if not fp:
                                continue
                            st = files.setdefault(fp, {"text": None, "when": None, "ops": 0, "complete": False})
                            st["ops"] += 1
                            if c.get("name") == "Write" and isinstance(inp.get("content"), str):
                                st.update(text=inp["content"], when=when, complete=True, session=jl)
                            elif c.get("name") == "Edit" and st["text"] is not None:
                                old, new = inp.get("old_string", ""), inp.get("new_string", "")
                                if old and old in st["text"]:
                                    st["text"] = st["text"].replace(old, new) if inp.get("replace_all") else \
                                        st["text"].replace(old, new, 1)
                                    st["when"] = when
                            elif c.get("name") == "MultiEdit" and st["text"] is not None:
                                for ed in inp.get("edits") or []:
                                    old, new = ed.get("old_string", ""), ed.get("new_string", "")
                                    if old and old in st["text"]:
                                        st["text"] = st["text"].replace(old, new, 1)
                                st["when"] = when
                    tur = d.get("toolUseResult")
                    if isinstance(tur, dict):
                        fobj = tur.get("file") if isinstance(tur.get("file"), dict) else None
                        if fobj and fobj.get("filePath") and isinstance(fobj.get("content"), str):
                            full = fobj.get("startLine", 1) in (0, 1) and (
                                not fobj.get("totalLines") or fobj.get("numLines") == fobj.get("totalLines"))
                            if full:
                                st = files.setdefault(fobj["filePath"], {"text": None, "when": None, "ops": 0,
                                                                         "complete": False})
                                st.update(text=fobj["content"], when=when, complete=True, session=jl)
                        elif tur.get("filePath") and isinstance(tur.get("originalFile"), str):
                            st = files.setdefault(tur["filePath"], {"text": None, "when": None, "ops": 0,
                                                                    "complete": False})
                            if st["text"] is None:
                                st.update(text=tur["originalFile"], when=when, complete=True, session=jl)
                                old, new = tur.get("oldString", ""), tur.get("newString", "")
                                if old and old in st["text"]:
                                    st["text"] = st["text"].replace(old, new, 1)
    leads = []
    for fp, st in files.items():
        if st["text"] is None or not _match(query, fp, project):
            continue
        if not query and os.path.exists(fp):
            continue
        state = "still exists" if os.path.exists(fp) else "deleted"
        leads.append(Lead("Claude Code log", fp, "Rebuilt from the agent's transcript (%s)" % state, st["when"],
                          len(st["text"].encode()), content=st["text"].encode(), complete=st["complete"],
                          extra={"session": st.get("session")}))
    return leads


def codex(query, project=None) -> list[Lead]:
    """Files created through Codex CLI's apply_patch ('*** Add File:')."""
    root = os.path.join(os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex")), "sessions")
    found: dict[str, tuple] = {}
    for jl in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            text = open(jl, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r"\*\*\* Add File: ([^\n\\]+)((?:\\n\+[^\n]*?)+)(?=\\n\*\*\*)", text):
            path = m.group(1).strip()
            body = m.group(2)
            lines = [ln[1:] for ln in json.loads('"%s"' % body.replace('"', '\\"')).split("\n") if ln.startswith("+")]
            found[path] = ("\n".join(lines) + "\n", jl)
    leads = []
    for path, (content, jl) in found.items():
        if _match(query, path, project) and (query or not os.path.exists(path)):
            leads.append(Lead("Codex log", path, "Created by Codex (later edits are not replayed)",
                              _dt.datetime.fromtimestamp(os.path.getmtime(jl)), len(content.encode()),
                              content=content.encode(), complete=False))
    return leads


# ------------------------------------------------------------ snapshots
def snapshots(query, project=None) -> list[Lead]:
    leads = []
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["tmutil", "listlocalsnapshots", "/"], capture_output=True, text=True,
                                 timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        snaps = [ln for ln in out.splitlines() if "com.apple" in ln]
        if snaps:
            leads.append(Lead("Time Machine", "local snapshots", "%d local snapshots exist (latest: %s)" % (
                len(snaps), snaps[-1].strip()), how="Open the folder in Finder, then Enter Time Machine", complete=False))
    elif sys.platform == "win32":
        try:
            out = subprocess.run(["vssadmin", "list", "shadows"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        n = out.count("Shadow Copy Volume")
        if n:
            leads.append(Lead("Shadow copies", "Previous Versions", "%d shadow copies exist" % n,
                              how="Right-click the folder > Properties > Previous Versions", complete=False))
        fh = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "FileHistory")
        if os.path.isdir(fh):
            leads.append(Lead("File History", fh, "Windows File History is set up",
                              how="Control Panel > File History > Restore personal files", complete=False))
    else:
        for d, label in (("/.snapshots", "Snapper/btrfs snapshots"), ("/timeshift/snapshots", "Timeshift snapshots"),
                         (os.path.join(HOME, ".zfs", "snapshot"), "ZFS snapshots")):
            if os.path.isdir(d):
                try:
                    n = len(os.listdir(d))
                except OSError:
                    n = 0
                if n:
                    leads.append(Lead(label, d, "%d snapshots" % n, how="Browse %s for older copies" % d,
                                      complete=False))
    return leads


# ------------------------------------------------------- thumbnails
def thumbnails(query, project=None) -> list[Lead]:
    """Desktop thumbnail caches keep small previews of photos that are gone."""
    leads = []
    if sys.platform.startswith("linux"):
        for png in glob.glob(os.path.join(HOME, ".cache", "thumbnails", "*", "*.png")):
            try:
                head = open(png, "rb").read(4096)
            except OSError:
                continue
            m = re.search(rb"Thumb::URI\x00([^\x00]+)", head)
            if not m:
                continue
            uri = m.group(1).decode("utf-8", "replace")
            path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
            if not os.path.exists(path) and _match(query, path, project):
                leads.append(Lead("Thumbnail cache", path, "A small preview of the deleted image", None,
                                  os.path.getsize(png), copy_from=png, complete=False))
    elif sys.platform == "win32":
        tc = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Explorer")
        if glob.glob(os.path.join(tc, "thumbcache_*.db")):
            leads.append(Lead("Thumbnail cache", tc, "Windows keeps small previews of photos it has shown",
                              how="Use a thumbcache viewer on thumbcache_*.db", complete=False))
    return leads


CLOUD = [
    ("Google Photos", "https://photos.google.com/trash", "Trash keeps backed-up photos for a while after deletion"),
    ("iCloud Photos", "https://www.icloud.com/photos/", "Check the Recently Deleted album"),
    ("Google Drive", "https://drive.google.com/drive/trash", "Trash, and 'Manage versions' on a file"),
    ("OneDrive", "https://onedrive.live.com/?v=recyclebin", "Recycle bin, and Version history on a file"),
    ("Dropbox", "https://www.dropbox.com/deleted_files", "Deleted files and version history"),
    ("Messages you sent", "", "Photos you sent over WhatsApp, iMessage, email or Slack are still in those chats"),
]

CHECKS = [("Trash", trash), ("Git", git), ("Editor history", editor_history), ("Claude Code", claude_code),
          ("Codex", codex), ("Snapshots", snapshots), ("Thumbnails", thumbnails)]


def hunt(query: str | None = None, project: str | None = None) -> list[Lead]:
    leads: list[Lead] = []
    for _name, fn in CHECKS:
        try:
            leads += fn(query, project)
        except Exception as e:  # one broken source must not stop the others
            leads.append(Lead(_name, "", "check failed: %s" % e, complete=False))
    leads.sort(key=lambda l: (not l.restorable, -(l.when.timestamp() if l.when else 0)))
    return leads


class SaveError(Exception):
    pass


def save(leads: list[Lead], out_dir: str, project: str | None = None) -> list[str]:
    """Copy every restorable lead into out_dir, never into the folder where the
    files were lost (writing there could overwrite what is still recoverable)."""
    out = os.path.realpath(os.path.abspath(out_dir))
    if project:
        proj = os.path.realpath(os.path.abspath(project))
        if out == proj or out.startswith(proj.rstrip(os.sep) + os.sep):
            raise SaveError("choose a folder outside %s: saving inside it could overwrite what is still "
                            "recoverable there" % project)
    for lead in leads:
        if lead.original and os.path.isabs(lead.original):
            d = os.path.dirname(os.path.realpath(lead.original))
            if out == d:
                raise SaveError("choose a different folder: %s is where the lost files lived" % out_dir)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for i, lead in enumerate(leads):
        if not lead.restorable:
            continue
        try:
            written.append(_save_one(lead, out_dir))
        except OSError as e:
            lead.extra["save_error"] = str(e)
    return written


def _save_one(lead: Lead, out_dir: str) -> str:
    name = os.path.basename(lead.original.split(" (blob")[0].rstrip("/\\")) or "file"
    sub = os.path.join(out_dir, re.sub(r"[^\w .-]", "_", lead.where))
    os.makedirs(sub, exist_ok=True)
    dest = os.path.join(sub, name)
    k = 2
    while os.path.exists(dest):
        b, e = os.path.splitext(name)
        dest = os.path.join(sub, "%s (%d)%s" % (b, k, e))
        k += 1
    if lead.content is not None:
        with open(dest, "wb") as f:
            f.write(lead.content)
    else:
        import shutil
        if os.path.isdir(lead.copy_from):
            shutil.copytree(lead.copy_from, dest)
        else:
            shutil.copy2(lead.copy_from, dest)
    return dest
