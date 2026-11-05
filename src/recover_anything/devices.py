"""Find disks and cards, describe them, and make sure we never save onto them.

The save guard compares *device identity*, not path strings: which physical
disk holds the output folder, and which physical disk is the source. It fails
closed. If the source is a device and we cannot prove the output folder lives
on a different disk, we refuse.
"""
from __future__ import annotations

import glob
import json
import os
import plistlib
import re
import subprocess
import sys


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------- normalizing
_WIN_DRIVE = re.compile(r"^[\\/]{2}[.?][\\/]physicaldrive(\d+)$", re.I)
_WIN_VOLUME = re.compile(r"^[\\/]{2}[.?][\\/]([a-z]):[\\/]?$", re.I)


def normalize_source(path: str) -> str:
    """Canonical spelling for a device path (Windows paths are case-insensitive
    and accept forward slashes)."""
    if sys.platform == "win32" or path.startswith(("\\\\", "//")):
        m = _WIN_DRIVE.match(path)
        if m:
            return "\\\\.\\PhysicalDrive%d" % int(m.group(1))
        m = _WIN_VOLUME.match(path)
        if m:
            return "\\\\.\\%s:" % m.group(1).upper()
    return path


def is_device_path(path: str) -> bool:
    p = normalize_source(path)
    if p.startswith("\\\\.\\"):
        return True
    try:
        import stat
        mode = os.stat(p).st_mode
        return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)
    except OSError:
        return False


def _existing_ancestor(path: str) -> str:
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


# ------------------------------------------------------------------ where
class Where:
    """Everything that writes into a folder (or reads of a source) can land on."""

    def __init__(self):
        self.disks: set = set()   # physical whole disks
        self.files: set = set()   # (st_dev, st_ino) of image files that back it
        self.remote = False       # a network share or a RAM disk is part of it
        self.unknown = None       # why some layer could not be worked out


def _file_key(path: str):
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


# Filesystems that store nothing on a local disk.
NET_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "afs", "ceph", "glusterfs", "lustre", "gpfs", "davfs",
          "9p", "virtiofs", "vboxsf", "vmhgfs", "fuse.sshfs", "fuse.rclone", "fuse.s3fs", "fuse.gcsfuse",
          "fuse.vmhgfs-fuse", "fuse.davfs2", "fuse.smbnetfs", "fuse.curlftpfs", "fuse.goofys", "fuse.juicefs"}
RAM_FS = {"tmpfs", "ramfs"}
_LOOPBACK = re.compile(r"^(localhost(\.localdomain)?|127(\.\d+){3}|::1|0\.0\.0\.0|::ffff:127(\.\d+){3})$", re.I)


def _net_host(fstype: str, source: str) -> str:
    if fstype.startswith("nfs") or fstype == "fuse.sshfs":
        return source.split(":", 1)[0].split("@")[-1] if ":" in source else ""
    m = re.match(r"^[\\/]{2}([^\\/]+)", source)  # //host/share
    return m.group(1) if m else ""


def _is_this_machine(host: str) -> bool:
    """A 'network' share served by this very computer may be the card itself."""
    h = host.strip("[]").lower()
    if not h:
        return False
    if _LOOPBACK.match(h):
        return True
    import socket
    try:
        names = {socket.gethostname().lower(), socket.getfqdn().lower()}
        names |= {n.split(".")[0] for n in names}
        if h in names or h.split(".")[0] in names:
            return True
        mine = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        return False
    return h in mine


# ------------------------------------------------------------------ Linux
def _unescape_mount(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _linux_mounts() -> list[tuple[str, str, str, str]]:
    """(mount point, fstype, source, super options) from /proc/self/mountinfo, in mount order."""
    out = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if "-" not in parts:
                    continue
                sep = parts.index("-")
                if sep < 5 or sep + 2 >= len(parts):
                    continue
                out.append((_unescape_mount(parts[4]), parts[sep + 1], _unescape_mount(parts[sep + 2]),
                            parts[sep + 3] if sep + 3 < len(parts) else ""))
    except OSError:
        pass
    return out


def _linux_mount_of(p: str):
    best = None
    for m in _linux_mounts():
        mp = m[0]
        if p == mp or p.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) >= len(best[0]):  # later entries win (stacked mounts)
                best = m
    return best


def _linux_sys_walk(real: str, where: Where, depth: int, follow_files: bool = True) -> None:
    """Walk a /sys/devices block node: partitions up to their disk, device-mapper
    / RAID / LUKS 'slaves' down to theirs, and loop devices to the file behind
    them (and, if follow_files, to the disk that file is on)."""
    if depth > 12 or not os.path.isdir(real):
        where.unknown = where.unknown or "a storage layer could not be followed"
        return
    slaves = os.path.join(real, "slaves")
    try:
        sl = os.listdir(slaves) if os.path.isdir(slaves) else []
    except OSError:
        sl = []
    if sl:
        for x in sl:
            _linux_sys_walk(os.path.realpath(os.path.join(slaves, x)), where, depth + 1, follow_files)
        return
    if os.path.exists(os.path.join(real, "partition")):
        _linux_sys_walk(os.path.dirname(real), where, depth + 1, follow_files)
        return
    name = os.path.basename(real)
    where.disks.add(name)
    backing = os.path.join(real, "loop", "backing_file")
    if os.path.exists(backing):
        try:
            with open(backing, encoding="utf-8", errors="replace") as f:
                fpath = f.read().rstrip("\n")
        except OSError:
            fpath = ""
        key = _file_key(fpath)
        if key is None:
            where.unknown = where.unknown or "the image file behind %s could not be found" % name
            return
        where.files.add(key)
        if follow_files:
            _linux_where(fpath, where, depth + 1)


def _linux_add_devnum(major: int, minor: int, where: Where, depth: int, follow_files: bool = True) -> None:
    node = "/sys/dev/block/%d:%d" % (major, minor)
    if not os.path.exists(node):
        where.unknown = where.unknown or "device %d:%d is not in /sys" % (major, minor)
        return
    _linux_sys_walk(os.path.realpath(node), where, depth, follow_files)


def _linux_add_dev(dev: str, where: Where, depth: int) -> None:
    try:
        rs = os.stat(dev).st_rdev
    except OSError:
        where.unknown = where.unknown or "%s could not be found" % dev
        return
    _linux_add_devnum(os.major(rs), os.minor(rs), where, depth)


def _linux_btrfs_members(where: Where, depth: int) -> None:
    """A btrfs filesystem can span several disks; mountinfo names only one."""
    for fsdir in glob.glob("/sys/fs/btrfs/*/devices"):
        try:
            members = os.listdir(fsdir)
        except OSError:
            continue
        found = Where()
        for x in members:
            _linux_sys_walk(os.path.realpath(os.path.join(fsdir, x)), found, depth + 1)
        if found.disks & where.disks:
            where.disks |= found.disks
            where.files |= found.files


def _overlay_dirs(options: str) -> list[str]:
    out = []
    for opt in re.split(r"(?<!\\),", options):
        k, _, v = opt.partition("=")
        if k in ("upperdir", "workdir") and v:
            out.append(v.replace("\\,", ","))
    return out


def _linux_zfs(source: str, where: Where, depth: int) -> None:
    pool = source.split("/")[0]
    out = _run(["zpool", "status", "-P", "-L", pool]) if pool else ""
    devs = re.findall(r"(/dev/\S+)", out)
    if not devs:
        where.unknown = where.unknown or "I couldn't list the disks of ZFS pool %s" % pool
        return
    for d in devs:
        _linux_add_dev(d, where, depth + 1)


def _linux_where(path: str, where: Where | None = None, depth: int = 0) -> Where:
    """Which disks (and image files) writes into `path` end up on."""
    where = where if where is not None else Where()
    if depth > 8:
        where.unknown = where.unknown or "too many layers of storage to follow"
        return where
    p = os.path.realpath(_existing_ancestor(path))
    try:
        st = os.stat(p)
    except OSError as e:
        where.unknown = where.unknown or "%s could not be checked (%s)" % (p, e.strerror or e)
        return where
    m = _linux_mount_of(p)
    mp, fstype, source, sopts = m if m else ("", "", "", "")
    if os.major(st.st_dev):
        _linux_add_devnum(os.major(st.st_dev), os.minor(st.st_dev), where, depth)
        if source.startswith("/dev/"):
            _linux_add_dev(source, where, depth)
        if fstype == "btrfs":
            _linux_btrfs_members(where, depth)
        return where
    return _linux_where_mount(mp or p, fstype, source, sopts, where, depth)


def _linux_where_mount(mp: str, fstype: str, source: str, sopts: str, where: Where, depth: int) -> Where:
    """A filesystem with an anonymous device number (btrfs, overlay, FUSE,
    network and RAM filesystems, ZFS...): work out what it stores files on
    from its type and mount source."""
    if fstype in RAM_FS:
        where.remote = True
    elif fstype in NET_FS:
        if _is_this_machine(_net_host(fstype, source)):
            where.unknown = where.unknown or "%s is a network share served by this computer" % mp
        else:
            where.remote = True
    elif fstype == "overlay":
        dirs = _overlay_dirs(sopts)
        if not dirs:
            where.unknown = where.unknown or "I couldn't tell where overlay %s stores its files" % mp
        for d in dirs:
            _linux_where(d, where, depth + 1)
    elif fstype == "zfs":
        _linux_zfs(source, where, depth)
    elif source.startswith("/dev/"):
        _linux_add_dev(source, where, depth)  # btrfs, fuseblk (ntfs-3g, exfat-fuse) and others
        if fstype == "btrfs":
            _linux_btrfs_members(where, depth)
    elif fstype.startswith("fuse") or fstype == "ecryptfs":
        # bindfs, mergerfs, gocryptfs, encfs, ecryptfs, fuse2fs: the source names
        # the folder(s) or image file the files really go to
        parts = [source] if os.path.isabs(source) else [x for x in source.split(":") if x]
        if parts and all(os.path.isabs(x) and os.path.exists(x) for x in parts):
            for x in parts:
                if os.path.isfile(x):
                    where.files.add(_file_key(x))
                _linux_where(x, where, depth + 1)
        else:
            where.unknown = where.unknown or "%s (%s) doesn't say which disk it keeps its files on" % (mp, fstype)
    else:
        where.unknown = where.unknown or "I couldn't tell which disk %s (%s) is on" % (mp, fstype or "?")
    return where


def _linux_source(source: str) -> Where:
    w = Where()
    try:
        rs = os.stat(source).st_rdev
    except OSError:
        return w
    # For a source, the file behind a loop device matters (another loop device
    # on the same file would write into it), but the disk that file sits on
    # does not: other writes to that disk can't change the file.
    _linux_add_devnum(os.major(rs), os.minor(rs), w, 0, follow_files=False)
    return w


# ------------------------------------------------------------------ macOS
_MAC_DISK = re.compile(r"^/dev/r?(disk\d+)(s\d+)*$")


def _mac_rdev_map() -> dict[int, str]:
    out = {}
    for dev in glob.glob("/dev/disk*"):
        try:
            out[os.stat(dev).st_rdev] = os.path.basename(dev)
        except OSError:
            continue
    return out


_APFS_CACHE: dict = {}


def _mac_apfs_map() -> dict:
    """APFS container disk (e.g. disk3) -> physical store partitions (disk0s2)."""
    if "map" in _APFS_CACHE:
        return _APFS_CACHE["map"]
    out: dict = {}
    try:
        pl = plistlib.loads(_run(["diskutil", "apfs", "list", "-plist"]).encode())
        for c in pl.get("Containers", []):
            ref = c.get("ContainerReference")
            stores = [p.get("DeviceIdentifier") for p in c.get("PhysicalStores", []) if p.get("DeviceIdentifier")]
            if c.get("DesignatedPhysicalStore"):
                stores.append(c["DesignatedPhysicalStore"])
            if ref and stores:
                out[ref] = set(stores)
    except Exception:
        pass
    _APFS_CACHE["map"] = out
    return out


def _mac_whole(ident: str, depth: int = 0) -> set[str]:
    """Physical whole disks for a disk identifier, looking through APFS
    containers (synthesized disks) to their physical stores."""
    m = re.match(r"^(disk\d+)", ident)
    if not m or depth > 4:
        return set()
    whole = m.group(1)
    stores = _mac_apfs_map().get(whole)
    if not stores:
        try:
            info = plistlib.loads(_run(["diskutil", "info", "-plist", whole]).encode())
        except Exception:
            info = {}
        stores = {s.get("APFSPhysicalStore", "") for s in info.get("APFSPhysicalStores") or []} - {""}
    if stores:
        out = set()
        for st in stores:
            out |= _mac_whole(st, depth + 1)
        return out or {whole}
    return {whole}


def _mac_path_disks(path: str):
    p = _existing_ancestor(path)
    try:
        dev = os.stat(p).st_dev
    except OSError:
        return None, True
    ident = _mac_rdev_map().get(dev)
    if not ident:
        return set(), False  # not on a local disk (network share, etc.)
    return _mac_whole(ident), True


def _mac_source_disks(source: str) -> set[str]:
    m = _MAC_DISK.match(source)
    if not m:
        return set()
    return _mac_whole(m.group(1))


# ---------------------------------------------------------------- Windows
def _win_disks_of_handle_path(dev_path: str) -> set[int] | None:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(dev_path, 0, 3, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        buf = ctypes.create_string_buffer(8 + 24 * 64)
        ret = wintypes.DWORD(0)
        if k32.DeviceIoControl(wintypes.HANDLE(h), 0x00560000, None, 0, buf, len(buf), ctypes.byref(ret), None):
            import struct
            n = struct.unpack_from("<I", buf.raw, 0)[0]
            return {struct.unpack_from("<I", buf.raw, 8 + 24 * i)[0] for i in range(min(n, 64))}
        # Not a volume: maybe a physical drive itself
        out = ctypes.create_string_buffer(12)
        if k32.DeviceIoControl(wintypes.HANDLE(h), 0x002D1080, None, 0, out, 12, ctypes.byref(ret), None):
            import struct
            return {struct.unpack_from("<I", out.raw, 4)[0]}
        return None
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def _win_path_disks(path: str):
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\") and not p.startswith("\\\\?\\UNC\\"):
        p = p[4:]
    if p.startswith("\\\\") or p.startswith("\\\\?\\UNC\\"):
        return set(), False  # network share
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        vol = ctypes.create_unicode_buffer(1024)
        if not k32.GetVolumePathNameW(_existing_ancestor(p), vol, 1024):
            return None, True
        guid = ctypes.create_unicode_buffer(1024)
        if not k32.GetVolumeNameForVolumeMountPointW(vol.value, guid, 1024):
            return None, True
        disks = _win_disks_of_handle_path(guid.value.rstrip("\\"))
        return disks, True
    except Exception:
        return None, True


def _win_source_disks(source: str) -> set[int]:
    s = normalize_source(source)
    m = _WIN_DRIVE.match(s)
    if m:
        return {int(m.group(1))}
    m = _WIN_VOLUME.match(s)
    if m:
        try:
            return _win_disks_of_handle_path("\\\\.\\%s:" % m.group(1).upper()) or set()
        except Exception:
            return set()
    return set()


# -------------------------------------------------------------- the guard
def _pair_to_where(disks, backed) -> Where:
    w = Where()
    if disks is None:
        w.unknown = "I couldn't work out which disk it is on"
    elif not disks and not backed:
        w.remote = True
    else:
        w.disks |= set(disks)
    return w


def where(path: str) -> Where:
    """Which disks (and image files) writes into folder `path` end up on."""
    if sys.platform == "win32":
        return _pair_to_where(*_win_path_disks(path))
    if sys.platform == "darwin":
        return _pair_to_where(*_mac_path_disks(path))
    return _linux_where(path)


def source_where(source: str) -> Where:
    if sys.platform == "win32":
        w = Where()
        w.disks |= _win_source_disks(source)
        return w
    if sys.platform == "darwin":
        w = Where()
        w.disks |= _mac_source_disks(source)
        return w
    return _linux_source(source)


def source_disks(source: str) -> set:
    return source_where(source).disks


def path_disks(path: str):
    """(disks, backed): which physical disks hold this folder, and whether it
    is on a local disk at all (a network share or RAM disk is not). disks is
    None when that can't be worked out."""
    w = where(path)
    if w.unknown:
        return None, True
    return w.disks, bool(w.disks) or not w.remote


# Words in check_output's reasons that no override flag can wave through.
DEFINITE = ("same disk", "folder", "itself")


def check_output(source: str, out_dir: str) -> tuple[bool, str]:
    """(ok, reason). ok is True only when saving into out_dir provably cannot
    write to the source."""
    src = normalize_source(source)
    if os.path.isdir(src):
        return False, ("%s is a folder. Point me at the device itself (see `recover-anything devices`) or a disk "
                       "image file." % source)
    if not is_device_path(src):
        # An image file: refuse writing over it, or into a copy of it that is
        # mounted (a loop device or FUSE mount backed by this very file).
        out = os.path.realpath(os.path.abspath(out_dir))
        try:
            if os.path.exists(out) and os.path.samefile(out, src):
                return False, "the output is the image file itself"
        except OSError:
            pass
        key = _file_key(src)
        if key and key in where(out_dir).files:
            return False, "%s is inside %s itself (it is mounted from that image)" % (out_dir, source)
        return True, ""
    s = source_where(src)
    if not s.disks:
        return False, ("I couldn't work out which physical disk %s is, so I can't prove that %s is on a different "
                       "one." % (source, out_dir))
    w = where(out_dir)
    if w.disks & s.disks or w.files & s.files:
        return False, "%s is on the same disk as %s." % (out_dir, source)
    if w.unknown:
        return False, "%s: %s." % (out_dir, w.unknown)
    if not w.disks and not w.remote:
        return False, "I couldn't work out which disk %s is on." % out_dir
    return True, ""


def output_is_on_source(source: str, out_dir: str) -> bool:
    """Backwards-compatible yes/no form of check_output (True = refuse)."""
    return not check_output(source, out_dir)[0]


def system_disks() -> set:
    if sys.platform == "win32":
        d, _ = _win_path_disks(os.environ.get("SystemDrive", "C:") + "\\")
        return d or set()
    d, _ = path_disks("/")
    out = set(d or set())
    for extra in ("/boot", "/usr", "/home", "/var", os.path.expanduser("~")):
        if os.path.exists(extra):
            e, _ = path_disks(extra)
            out |= e or set()
    return out


def is_system_disk(source: str) -> bool:
    sd = source_disks(normalize_source(source))
    return bool(sd and sd & system_disks())


# ------------------------------------------------------------------- listing
def list_devices() -> list[dict]:
    if sys.platform.startswith("linux"):
        devs = _linux_devices()
    elif sys.platform == "darwin":
        devs = _mac_devices()
    elif sys.platform == "win32":
        devs = _win_devices()
    else:
        devs = []
    try:
        sysd = system_disks()
        for d in devs:
            d["system_disk"] = bool(source_disks(d["path"]) & sysd) or d.get("system_disk", False)
    except Exception:
        pass
    return devs


def _lsblk() -> list:
    base = ["lsblk", "-J", "-b", "-o"]
    for cols in ("NAME,PATH,SIZE,TYPE,TRAN,RM,ROTA,MODEL,FSTYPE,MOUNTPOINTS,DISC-MAX",
                 "NAME,PATH,SIZE,TYPE,TRAN,RM,ROTA,MODEL,FSTYPE,MOUNTPOINT,DISC-MAX",
                 "NAME,SIZE,TYPE,TRAN,RM,ROTA,MODEL,FSTYPE,MOUNTPOINT"):
        out = _run(base + [cols])
        try:
            tree = json.loads(out).get("blockdevices")
        except ValueError:
            continue
        if tree is not None:
            return tree
    return []


def _mounts_of(node) -> list[str]:
    out = []
    mp = node.get("mountpoints") or ([node.get("mountpoint")] if node.get("mountpoint") else [])
    out += [m for m in mp if m]
    for c in node.get("children") or []:
        out += _mounts_of(c)
    return out


def _parts_of(node) -> list[str]:
    out = []
    for c in node.get("children") or []:
        out.append(c.get("path") or "/dev/" + c.get("name", ""))
        out += _parts_of(c)
    return out


def _linux_devices() -> list[dict]:
    devs = []
    for d in _lsblk():
        name = d.get("name", "")
        if d.get("type") not in ("disk", "loop") or name.startswith(("zram", "ram")) or not int(d.get("size") or 0):
            continue
        devs.append({
            "path": d.get("path") or "/dev/" + name,
            "size": int(d.get("size") or 0),
            "model": (d.get("model") or "").strip(),
            "bus": d.get("tran") or ("mmc" if name.startswith("mmcblk") else ""),
            "removable": bool(int(d.get("rm") or 0)) or name.startswith("mmcblk"),
            "ssd": not bool(int(d.get("rota") or 0)),
            "trim": str(d.get("disc-max") or "0") not in ("0", "0B"),
            "mounted_at": _mounts_of(d),
            "partitions": _parts_of(d),
        })
    return devs


def _mac_devices() -> list[dict]:
    raw = _run(["diskutil", "list", "-plist", "physical"])
    try:
        pl = plistlib.loads(raw.encode())
    except Exception:
        return []
    devs = []
    for d in pl.get("AllDisksAndPartitions", []):
        ident = d.get("DeviceIdentifier")
        info = {}
        try:
            info = plistlib.loads(_run(["diskutil", "info", "-plist", ident]).encode())
        except Exception:
            pass
        mounts = [p.get("MountPoint") for p in d.get("Partitions", []) if p.get("MountPoint")]
        if d.get("MountPoint"):
            mounts.append(d["MountPoint"])
        devs.append({
            "path": "/dev/r" + ident,
            "size": int(d.get("Size") or info.get("TotalSize") or 0),
            "model": info.get("MediaName", ""),
            "bus": info.get("BusProtocol", ""),
            "removable": bool(info.get("RemovableMedia") or info.get("Removable") or info.get("Ejectable")),
            "ssd": bool(info.get("SolidState")),
            "trim": bool(info.get("TRIMSupport")),
            "mounted_at": mounts,
            "partitions": ["/dev/" + p.get("DeviceIdentifier") for p in d.get("Partitions", [])],
        })
    return devs


def _win_devices() -> list[dict]:
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-Disk | ForEach-Object { $d=$_; $l=@(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue "
          "| Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter + ':' }); "
          "[pscustomobject]@{Number=$d.Number;Name=$d.FriendlyName;Size=$d.Size;Bus=[string]$d.BusType;"
          "Boot=$d.IsBoot;System=$d.IsSystem;Letters=$l} } | ConvertTo-Json -Compress")
    raw = _run(["powershell", "-NoProfile", "-Command", ps], timeout=40)
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if isinstance(items, dict):
        items = [items]
    media = {}
    raw2 = _run(["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-PhysicalDisk | Select-Object DeviceId,MediaType | ConvertTo-Json -Compress"], timeout=40)
    try:
        m = json.loads(raw2)
        for x in m if isinstance(m, list) else [m]:
            media[str(x.get("DeviceId"))] = str(x.get("MediaType"))
    except ValueError:
        pass
    devs = []
    for d in items:
        bus = d.get("Bus", "")
        devs.append({
            "path": "\\\\.\\PhysicalDrive%d" % d["Number"],
            "size": int(d.get("Size") or 0),
            "model": d.get("Name", ""),
            "bus": bus,
            "removable": bus in ("SD", "USB", "MMC"),
            "ssd": media.get(str(d["Number"])) in ("SSD", "4"),
            "trim": media.get(str(d["Number"])) in ("SSD", "4"),
            "mounted_at": d.get("Letters") or [],
            "partitions": [],
            "system_disk": bool(d.get("Boot") or d.get("System")),
        })
    return devs


def describe(path: str) -> dict | None:
    """Listing entry for a device path, matched by physical disk identity."""
    want = source_disks(normalize_source(path))
    if not want:
        return None
    for d in list_devices():
        try:
            if source_disks(d["path"]) & want:
                return d
        except Exception:
            continue
    return None


def mounts_of(path: str) -> list[str]:
    d = describe(path)
    return list(d.get("mounted_at") or []) if d else []
