"""Find disks and cards, describe them, and make sure we never save onto them.

The save guard compares *device identity*, not path strings: which physical
disk holds the output folder, and which physical disk is the source. It fails
closed. If the source is a device and we cannot prove the output folder lives
on a different disk, we refuse.
"""
from __future__ import annotations

import glob
import json
import ntpath
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
MAC_NET_FS = {"smbfs", "nfs", "afpfs", "webdav", "ftp", "cifs"}
_MAC_CACHE: dict = {}


def _mac_plist(cmd: list[str]):
    key = tuple(cmd)
    if key not in _MAC_CACHE:
        try:
            _MAC_CACHE[key] = plistlib.loads(_run(cmd).encode())
        except Exception:
            _MAC_CACHE[key] = None
    return _MAC_CACHE[key]


def _mac_statfs(path: str):
    """(filesystem type, mounted from, mounted on) for the volume holding path,
    straight from the kernel (statfs), or None."""
    import ctypes
    import ctypes.util

    class StatFs(ctypes.Structure):  # struct statfs with 64-bit inodes
        _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
                    ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                    ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
                    ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                    ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                    ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                    ("f_reserved", ctypes.c_uint32 * 7)]
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        try:
            fn = libc["statfs$INODE64"]  # Intel Macs: the 64-bit-inode variant has this name
        except AttributeError:
            fn = libc["statfs"]
        fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFs)]
        buf = StatFs()
        if fn(os.fsencode(path), ctypes.byref(buf)) != 0:
            return None
        dec = lambda b: b.decode("utf-8", "replace")  # noqa: E731
        return dec(buf.f_fstypename), dec(buf.f_mntfromname), dec(buf.f_mntonname)
    except Exception:
        return None


def _mac_apfs_map() -> dict:
    """APFS container disk (e.g. disk3) -> physical store partitions (disk0s2)."""
    out: dict = {}
    pl = _mac_plist(["diskutil", "apfs", "list", "-plist"]) or {}
    for c in pl.get("Containers", []):
        ref = c.get("ContainerReference")
        stores = [x.get("DeviceIdentifier") for x in c.get("PhysicalStores", []) if x.get("DeviceIdentifier")]
        if c.get("DesignatedPhysicalStore"):
            stores.append(c["DesignatedPhysicalStore"])
        if ref and stores:
            out[ref] = set(stores)
    return out


def _mac_image_of(whole: str):
    """The disk image file attached as /dev/<whole>, if it is one."""
    pl = _mac_plist(["hdiutil", "info", "-plist"]) or {}
    for img in pl.get("images", []):
        for ent in img.get("system-entities", []):
            m = _MAC_DISK.match(ent.get("dev-entry", ""))
            if m and m.group(1) == whole:
                return img.get("image-path")
    return None


def _mac_resolve(ident: str, where: Where, depth: int, follow_files: bool = True) -> None:
    """Physical disks behind a disk identifier: through APFS containers to their
    physical stores, and through attached disk images to the file (and, if
    follow_files, the disk that file is on)."""
    m = re.match(r"^(disk\d+)", ident)
    if not m or depth > 6:
        where.unknown = where.unknown or "I couldn't follow %s to a disk" % ident
        return
    whole = m.group(1)
    stores = _mac_apfs_map().get(whole)
    info = _mac_plist(["diskutil", "info", "-plist", whole])
    if not stores and info:
        stores = {x.get("APFSPhysicalStore", "") for x in info.get("APFSPhysicalStores") or []} - {""}
    if stores:
        for st in stores:
            _mac_resolve(st, where, depth + 1, follow_files)
        return
    if not info:
        where.unknown = where.unknown or "diskutil didn't answer for %s" % whole
        return
    where.disks.add(whole)
    image = _mac_image_of(whole)
    if image or info.get("BusProtocol") == "Disk Image":
        key = _file_key(image) if image else None
        if key is None:
            where.unknown = where.unknown or "%s is a disk image whose file I couldn't find" % whole
            return
        where.files.add(key)
        if follow_files:
            _mac_where(image, where, depth + 1)


def _mac_where(path: str, where: Where | None = None, depth: int = 0) -> Where:
    where = where if where is not None else Where()
    p = os.path.realpath(_existing_ancestor(path))
    info = _mac_statfs(p)
    if info is None:
        where.unknown = where.unknown or "I couldn't check which disk %s is on" % p
        return where
    fstype, mfrom, mon = info
    if fstype in MAC_NET_FS:
        host = re.sub(r"^[a-z]+://", "", mfrom).lstrip("/").split("@")[-1]
        host = re.split(r"[/:]", host)[0]
        if _is_this_machine(host):
            where.unknown = where.unknown or "%s is a network share served by this computer" % mon
        else:
            where.remote = True
        return where
    m = _MAC_DISK.match(mfrom)
    if not m:
        where.unknown = where.unknown or "%s (%s, from %s) doesn't say which disk it is on" % (mon, fstype, mfrom)
        return where
    _mac_resolve(m.group(1) + (m.group(2) or ""), where, depth)
    return where


def _mac_source(source: str) -> Where:
    w = Where()
    m = _MAC_DISK.match(source)
    if m:
        _mac_resolve(m.group(1), w, 0, follow_files=False)
    return w


# ---------------------------------------------------------------- Windows
_WIN_DEVNS = re.compile(r"^[\\/]{2}[.?][\\/]([a-z]):([\\/].*)?$", re.I)   # \\.\E:\x and \\?\E:\x
_WIN_UNC_LONG = re.compile(r"^[\\/]{2}\?[\\/]UNC[\\/](.*)$", re.I)        # \\?\UNC\server\share\x
_WIN_DEVICE = re.compile(r"^[\\/]{2}[.?][\\/](physicaldrive\d+|harddisk\d+|cdrom\d+|tape\d+|pipe[\\/])", re.I)
BUS_VIRTUAL = {14, 15, 16}  # BusTypeVirtual, BusTypeFileBackedVirtual (.vhd/.vhdx/.iso), BusTypeSpaces


def _win_plain(path: str) -> str:
    """Spell a folder without device-namespace prefixes: \\\\.\\E:\\x and
    \\\\?\\E:\\x are E:\\x, \\\\?\\UNC\\s\\x is \\\\s\\x. Volume GUID and
    GLOBALROOT paths are left as they are (Windows opens them directly)."""
    m = _WIN_DEVNS.match(path)
    if m:
        return m.group(1).upper() + ":" + (m.group(2) or "\\")
    m = _WIN_UNC_LONG.match(path)
    if m:
        return "\\\\" + m.group(1)
    return path


def _k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    return k32


def _win_open(path: str, flags: int = 0):
    from ctypes import wintypes
    k32 = _k32()
    h = k32.CreateFileW(path, 0, 7, None, 3, flags, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    return h


def _win_disks_of_handle_path(dev_path: str) -> set[int] | None:
    import ctypes
    import struct
    from ctypes import wintypes
    k32 = _k32()
    h = _win_open(dev_path)
    if h is None:
        return None
    try:
        buf = ctypes.create_string_buffer(8 + 24 * 64)
        ret = wintypes.DWORD(0)
        if k32.DeviceIoControl(wintypes.HANDLE(h), 0x00560000, None, 0, buf, len(buf), ctypes.byref(ret), None):
            n = struct.unpack_from("<I", buf.raw, 0)[0]
            return {struct.unpack_from("<I", buf.raw, 8 + 24 * i)[0] for i in range(min(n, 64))}
        # Not a volume: maybe a physical drive itself
        out = ctypes.create_string_buffer(12)
        if k32.DeviceIoControl(wintypes.HANDLE(h), 0x002D1080, None, 0, out, 12, ctypes.byref(ret), None):
            return {struct.unpack_from("<I", out.raw, 4)[0]}
        return None
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def _win_bus_type(disk: int):
    """STORAGE_BUS_TYPE of a physical drive (15 = a mounted .vhd/.vhdx/.iso), or None."""
    import ctypes
    import struct
    from ctypes import wintypes
    k32 = _k32()
    h = _win_open("\\\\.\\PhysicalDrive%d" % disk)
    if h is None:
        return None
    try:
        query = ctypes.create_string_buffer(struct.pack("<II4x", 0, 0))  # StorageDeviceProperty, standard query
        out = ctypes.create_string_buffer(1024)
        ret = wintypes.DWORD(0)
        if not k32.DeviceIoControl(wintypes.HANDLE(h), 0x002D1400, query, 12, out, 1024, ctypes.byref(ret), None):
            return None
        return struct.unpack_from("<I", out.raw, 28)[0] if ret.value >= 32 else None
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def _win_final_paths(path: str):
    """(volume-GUID spelling, DOS spelling) of a file or folder after every
    symlink, junction, mounted folder and subst drive is resolved."""
    import ctypes
    from ctypes import wintypes
    h = _win_open(path, 0x02000000)  # FILE_FLAG_BACKUP_SEMANTICS: lets folders be opened
    if h is None:
        return None
    k32 = _k32()
    try:
        out = []
        for flag in (1, 0):  # VOLUME_NAME_GUID, VOLUME_NAME_DOS
            buf = ctypes.create_unicode_buffer(32768)
            n = k32.GetFinalPathNameByHandleW(wintypes.HANDLE(h), buf, 32768, flag)
            out.append(buf.value if 0 < n < 32768 else None)
        return out[0], out[1]
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def _win_share_path(host: str, share: str):
    """Local folder behind a share this computer serves (E$ -> E:\\)."""
    import ctypes
    from ctypes import wintypes
    m = re.match(r"^([a-z])\$$", share, re.I)
    if m:
        return m.group(1).upper() + ":\\"
    try:
        net = ctypes.windll.netapi32
        ptr = ctypes.c_void_p()
        if net.NetShareGetInfo(None, ctypes.c_wchar_p(share), 2, ctypes.byref(ptr)) != 0 or not ptr.value:
            return None
        try:
            # SHARE_INFO_2: netname, type, remark, permissions, max_uses, current_uses, path, passwd
            class ShareInfo2(ctypes.Structure):
                _fields_ = [("netname", wintypes.LPWSTR), ("type", wintypes.DWORD), ("remark", wintypes.LPWSTR),
                            ("permissions", wintypes.DWORD), ("max_uses", wintypes.DWORD),
                            ("current_uses", wintypes.DWORD), ("path", wintypes.LPWSTR), ("passwd", wintypes.LPWSTR)]
            return ctypes.cast(ptr, ctypes.POINTER(ShareInfo2)).contents.path
        finally:
            net.NetApiBufferFree(ptr)
    except Exception:
        return None


def _win_exists(p: str) -> bool:
    return os.path.exists(p)


def _win_existing_ancestor(p: str) -> str:
    while not _win_exists(p):
        parent = ntpath.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def _win_where(path: str, where: Where | None = None, depth: int = 0) -> Where:
    where = where if where is not None else Where()
    if depth > 4:
        where.unknown = where.unknown or "too many layers of storage to follow"
        return where
    if _WIN_DEVICE.match(path):
        where.unknown = where.unknown or "%s is a device, not a folder" % path
        return where
    p = _win_plain(path)
    if not p.startswith(("\\\\?\\", "\\\\.\\")):
        p = ntpath.abspath(p)
    anc = _win_existing_ancestor(p)
    try:
        final = _win_final_paths(anc)
    except Exception:
        final = None
    if not final:
        where.unknown = where.unknown or "I couldn't open %s to check which disk it is on" % anc
        return where
    guid, dos = final
    if guid and guid.lower().startswith("\\\\?\\volume{"):
        vol = guid[:guid.index("}") + 1]
        disks = _win_disks_of_handle_path(vol)
        if not disks:
            where.unknown = where.unknown or "I couldn't work out which disk %s is on" % path
            return where
        where.disks |= disks
        if any(_win_bus_type(d) in BUS_VIRTUAL for d in disks):
            where.unknown = where.unknown or ("%s is on a virtual disk (a .vhd or .vhdx file, or Storage Spaces), and "
                                              "I can't tell which drive that is stored on" % path)
        return where
    m = re.match(r"^\\\\\?\\UNC\\([^\\]+)\\([^\\]+)(.*)$", dos or "", re.I)
    if m:
        host = m.group(1).split("@")[0]  # WebDAV: server@SSL@port
        if not _is_this_machine(host):
            where.remote = True
            return where
        local = _win_share_path(host, m.group(2))
        if not local:
            where.unknown = where.unknown or "%s is a share served by this computer, and I couldn't tell which folder" % path
            return where
        return _win_where(local.rstrip("\\") + "\\" + m.group(3).lstrip("\\"), where, depth + 1)
    m = re.match(r"^\\\\\?\\([a-z]):", dos or "", re.I)
    if m:  # a local volume without a GUID name
        disks = _win_disks_of_handle_path("\\\\.\\%s:" % m.group(1).upper())
        if disks:
            where.disks |= disks
            return where
    where.unknown = where.unknown or "I couldn't work out which disk %s is on" % path
    return where


def _win_path_disks(path: str):
    """(disks, backed) for older callers."""
    w = _win_where(path)
    if w.unknown:
        return None, True
    return w.disks, not w.remote


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
def where(path: str) -> Where:
    """Which disks (and image files) writes into folder `path` end up on."""
    if sys.platform == "win32":
        return _win_where(path)
    if sys.platform == "darwin":
        return _mac_where(path)
    return _linux_where(path)


def source_where(source: str) -> Where:
    if sys.platform == "win32":
        w = Where()
        w.disks |= _win_source_disks(source)
        return w
    if sys.platform == "darwin":
        return _mac_source(source)
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
    _MAC_CACHE.clear()
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
