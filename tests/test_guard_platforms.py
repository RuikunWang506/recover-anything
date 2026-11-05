"""The Windows and macOS save guards, with the operating system's answers
stubbed, so every spelling and layer is checked on every platform. The real
thing is exercised on Windows and macOS runners by test_guard_real.py."""
import types

import pytest

from recover_anything import devices

CARD = "\\\\.\\PhysicalDrive2"


# ------------------------------------------------------------------ Windows
# C: = disk 0, D: = disk 1, E: = the SD card = disk 2 (also mounted at
# C:\Mount\Card, and C:\Users\me\CardLink is a junction to it), V: = a .vhdx
# (disk 3), \\nas\photos = a network share.
VOLUMES = {"c": "cccc", "d": "dddd", "e": "eeee", "v": "vvvv"}
DISKS = {"cccc": 0, "dddd": 1, "eeee": 2, "vvvv": 3}
LINKS = {"c:\\mount\\card": "e:", "c:\\users\\me\\cardlink": "e:"}


def _final(path):
    q = path.lower().rstrip("\\")
    for link, target in LINKS.items():
        if q == link or q.startswith(link + "\\"):
            q = target + q[len(link):]
    if q.startswith("\\\\?\\volume{"):
        g = q[11:q.index("}")]
        return "\\\\?\\Volume{%s}%s" % (g, q[q.index("}") + 1:] or "\\"), None
    if q.startswith("\\\\"):
        return None, "\\\\?\\UNC\\" + q[2:]
    letter = q[:1]
    if q[1:2] != ":" or letter not in VOLUMES:
        return None
    return "\\\\?\\Volume{%s}%s" % (VOLUMES[letter], q[2:] or "\\"), "\\\\?\\" + q.upper()


def _disks(dev):
    q = dev.lower()
    if q.startswith("\\\\?\\volume{"):
        return {DISKS[q[11:q.index("}")]]}
    if q.startswith("\\\\.\\") and q[4:5] in VOLUMES:
        return {DISKS[VOLUMES[q[4:5]]]}
    return None


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(devices, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(devices, "_win_final_paths", _final)
    monkeypatch.setattr(devices, "_win_disks_of_handle_path", _disks)
    monkeypatch.setattr(devices, "_win_bus_type", lambda d: 15 if d == 3 else 7)
    monkeypatch.setattr(devices, "_win_exists", lambda p: not p.lower().rstrip("\\").endswith(("recovered", "new")))
    monkeypatch.setattr(devices, "_win_share_path", lambda host, share: {"cardshare": "E:\\DCIM"}.get(
        share.lower(), share[0] + ":\\" if share.endswith("$") else None))


@pytest.mark.parametrize("out", [
    "E:\\Recovered", "e:\\recovered", "E:/Recovered", "\\\\.\\E:\\Recovered", "\\\\?\\E:\\Recovered",
    "\\\\?\\Volume{eeee}\\Recovered", "\\\\localhost\\E$\\Recovered", "\\\\127.0.0.1\\cardshare\\Recovered",
    "\\\\?\\UNC\\localhost\\E$\\Recovered", "C:\\Users\\me\\CardLink\\Recovered", "C:\\Mount\\Card\\new\\Recovered",
])
def test_windows_every_spelling_of_the_card_is_refused(windows, out):
    for src in (CARD, "\\\\.\\physicaldrive2", "//./PhysicalDrive2", "\\\\.\\E:"):
        ok, why = devices.check_output(src, out)
        assert not ok and "same disk" in why, (src, out, why)


def test_windows_other_drives_and_shares(windows):
    assert devices.check_output(CARD, "D:\\Recovered") == (True, "")
    assert devices.check_output(CARD, "C:\\Users\\me\\Recovered") == (True, "")
    assert devices.check_output(CARD, "\\\\nas\\photos\\Recovered") == (True, "")
    ok, why = devices.check_output(CARD, "V:\\Recovered")  # a .vhdx: its file could be anywhere
    assert not ok and "virtual disk" in why and not any(k in why for k in devices.DEFINITE)
    ok, why = devices.check_output(CARD, "\\\\.\\PhysicalDrive1\\Recovered")
    assert not ok


# -------------------------------------------------------------------- macOS
# disk4 = the SD card (FAT, /Volumes/CARD); disk3 = APFS container on disk0s2
# (the Mac's own disk); disk6 = INNER.dmg, a disk image stored on the card;
# disk5 = an APFS container diskutil doesn't answer for.
def _mac(tmp_path):
    inner = tmp_path / "INNER.dmg"
    inner.write_bytes(b"")
    mounts = {
        "/Volumes/CARD": ("msdos", "/dev/disk4s1"),
        "/Volumes/NTFSCARD": ("macfuse", "/dev/disk4s1"),
        "/Volumes/FUSE": ("macfuse", "ntfs-3g@macfuse0"),
        "/Volumes/INNER": ("hfs", "/dev/disk6s1"),
        "/Volumes/SLOW": ("apfs", "/dev/disk5s1"),
        "/Volumes/NAS": ("smbfs", "//me@nas.local/photos"),
        "/Volumes/LOOP": ("smbfs", "//me@localhost/card"),
        str(tmp_path): ("msdos", "/dev/disk4s1"),  # where INNER.dmg is stored: on the card
        "/": ("apfs", "/dev/disk3s5"),
    }

    def statfs(p):
        q = p.replace("\\", "/")
        best = max((m for m in mounts if q == m.replace("\\", "/")
                    or q.startswith(m.replace("\\", "/").rstrip("/") + "/")), key=len)
        return mounts[best][0], mounts[best][1], best

    plists = {
        ("diskutil", "apfs", "list", "-plist"): {"Containers": [
            {"ContainerReference": "disk3", "PhysicalStores": [{"DeviceIdentifier": "disk0s2"}]}]},
        ("diskutil", "info", "-plist", "disk0"): {"BusProtocol": "Apple Fabric"},
        ("diskutil", "info", "-plist", "disk4"): {"BusProtocol": "USB"},
        ("diskutil", "info", "-plist", "disk6"): {"BusProtocol": "Disk Image"},
        ("diskutil", "info", "-plist", "disk5"): None,
        ("hdiutil", "info", "-plist"): {"images": [{"image-path": str(inner), "system-entities": [
            {"dev-entry": "/dev/disk6"}, {"dev-entry": "/dev/disk6s1"}]}]},
    }
    return statfs, lambda cmd: plists.get(tuple(cmd))


@pytest.fixture
def mac(monkeypatch, tmp_path):
    statfs, plist = _mac(tmp_path)
    monkeypatch.setattr(devices, "sys", types.SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(devices, "_mac_statfs", statfs)
    monkeypatch.setattr(devices, "_mac_plist", plist)
    monkeypatch.setattr(devices.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(devices, "_existing_ancestor", lambda p: p)
    monkeypatch.setattr(devices, "is_device_path", lambda p: p.startswith("/dev/"))
    return tmp_path


def test_mac_card_volume_and_its_other_mounts_are_refused(mac):
    for src in ("/dev/rdisk4", "/dev/disk4", "/dev/disk4s1"):
        for out in ("/Volumes/CARD/Recovered", "/Volumes/NTFSCARD/Recovered", "/Volumes/INNER/Recovered"):
            ok, why = devices.check_output(src, out)
            assert not ok and "same disk" in why, (src, out, why)


def test_mac_unknown_layers_are_refused_and_others_allowed(mac):
    for out in ("/Volumes/FUSE/Recovered", "/Volumes/SLOW/Recovered", "/Volumes/LOOP/Recovered"):
        ok, why = devices.check_output("/dev/rdisk4", out)
        assert not ok and not any(k in why for k in devices.DEFINITE), (out, why)
    assert devices.check_output("/dev/rdisk4", "/Users/me/Recovered") == (True, "")
    assert devices.check_output("/dev/rdisk4", "/Volumes/NAS/Recovered") == (True, "")
