"""Find disks and cards, describe them, and make sure we never save onto them."""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------------- listing
def list_devices() -> list[dict]:
    if sys.platform.startswith("linux"):
        return _linux_devices()
    if sys.platform == "darwin":
        return _mac_devices()
    if sys.platform == "win32":
        return _win_devices()
    return []


def _linux_devices() -> list[dict]:
    out = _run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,TRAN,RM,ROTA,MODEL,FSTYPE,MOUNTPOINT,DISC-MAX"])
    try:
        tree = json.loads(out).get("blockdevices", [])
    except ValueError:
        return []
    devs = []
    for d in tree:
        if d.get("type") not in ("disk", "loop") or d["name"].startswith(("zram", "ram")) or not int(d.get("size") or 0):
            continue
        mounts = [c.get("mountpoint") for c in d.get("children", []) or [] if c.get("mountpoint")]
        if d.get("mountpoint"):
            mounts.append(d["mountpoint"])
        devs.append({
            "path": d.get("path") or "/dev/" + d["name"],
            "size": int(d.get("size") or 0),
            "model": (d.get("model") or "").strip(),
            "bus": d.get("tran") or ("mmc" if d["name"].startswith("mmcblk") else ""),
            "removable": bool(int(d.get("rm") or 0)) or d["name"].startswith("mmcblk"),
            "ssd": not bool(int(d.get("rota") or 0)),
            "trim": str(d.get("disc-max") or "0") not in ("0", "0B"),
            "mounted_at": mounts,
            "partitions": [c.get("path") for c in d.get("children", []) or []],
            "system_disk": any(m in ("/", "/boot", "/boot/efi") for m in mounts),
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
            "system_disk": bool(info.get("Internal")) and "/" in mounts or ident == "disk0",
        })
    return devs


def _win_devices() -> list[dict]:
    ps = ("Get-Disk | ForEach-Object { $d=$_; $l=@(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue "
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
    norm = os.path.realpath(path) if not path.startswith("\\\\") else path
    for d in list_devices():
        cand = [d["path"]] + d.get("partitions", [])
        if sys.platform == "darwin":
            cand += [p.replace("/dev/r", "/dev/") for p in cand] + [p.replace("/dev/", "/dev/r") for p in cand]
        if norm in cand or path in cand:
            return d
    return None


# --------------------------------------------------------------------- guard
def mounts_of(path: str) -> list[str]:
    """Mount points (folders or drive letters) that live on the given device."""
    if not path.startswith("\\\\") and os.path.isfile(path):
        return []
    d = describe(path)
    if d:
        return list(d.get("mounted_at") or [])
    if sys.platform == "win32":
        m = re.match(r"^\\\\\.\\([A-Za-z]):$", path)
        if m:
            return [m.group(1).upper() + ":"]
    return []


def output_is_on_source(source: str, out_dir: str) -> bool:
    out = os.path.realpath(os.path.abspath(out_dir))
    if os.path.isfile(source):
        src = os.path.realpath(source)
        return out == src
    for m in mounts_of(source):
        if not m:
            continue
        if sys.platform == "win32":
            if out[:2].upper() == m[:2].upper():
                return True
        else:
            mp = os.path.realpath(m)
            if out == mp or out.startswith(mp.rstrip("/") + "/"):
                if mp != "/":
                    return True
    return False
