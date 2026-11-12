"""The save guard against real disk images on macOS (hdiutil) and Windows
(diskpart .vhd). Skipped elsewhere; CI runs both."""
import os
import plistlib
import shutil
import subprocess
import sys
import time

import pytest

from recover_anything import devices


def _run(*cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _diag(*parts):
    """Details CI prints after the tests (set RA_DIAG to a file name)."""
    print(*parts)
    if os.environ.get("RA_DIAG"):
        with open(os.environ["RA_DIAG"], "a", encoding="utf-8") as f:
            print(*parts, file=f)


# -------------------------------------------------------------------- macOS
# The card is FAT32, as real cards are. Since macOS 15.4 FAT and exFAT volumes
# are served by FSKit, outside the kernel. A second FSKit volume whose image
# file sits on the first never lets go: its driver freezes at the unmount, both
# disks stay attached, and every hdiutil and diskutil call on the Mac hangs
# until those processes are stopped. So the image stored on the card must hold
# a filesystem the kernel serves itself.
CARD_FS = "MS-DOS FAT32"
INNER_FS = "HFS+"
FSKIT_FILESYSTEMS = ("MS-DOS", "FAT", "ExFAT")


def _attach(dmg):
    r = _run("hdiutil", "attach", "-nobrowse", "-plist", str(dmg))
    assert r.returncode == 0, r.stderr
    ents = plistlib.loads(r.stdout.encode())["system-entities"]
    whole = min((e["dev-entry"] for e in ents), key=len)
    mnt = next(e["mount-point"] for e in ents if e.get("mount-point"))
    return whole, mnt


def _detach(devs, run=_run, tries=4, wait=5.0, timeout=180):
    """Detach disk images, innermost first, waiting longer before each new try
    (DiskArbitration can take a while to let go). Returns {device: why} for
    every one still attached. Once one is stuck, the ones after it are not
    tried: they hold its image file, so they can't detach either."""
    stuck = {}
    for d in devs:
        if stuck:
            stuck[d] = "not tried: %s is still attached from it" % next(iter(stuck))
            continue
        why = None
        for i in range(tries):
            if i:
                time.sleep(wait * i)
            try:
                r = run("hdiutil", "detach", "-force", d, timeout=timeout)
            except subprocess.TimeoutExpired:
                why = "hdiutil detach didn't answer in %d s" % timeout
                continue
            if r.returncode == 0:
                why = None
                break
            why = (r.stderr or r.stdout or "exit code %d" % r.returncode).strip()
        if why:
            stuck[d] = why
    return stuck


def _stuck_message(stuck):
    return ("These test disk images are still attached: %s. Eject them in Disk Utility, "
            "or restart the Mac." % "; ".join("%s (%s)" % kv for kv in stuck.items()))


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("hdiutil"), reason="macOS only")
def test_mac_card_image_and_an_image_stored_on_it(tmp_path):
    dmg = tmp_path / "card.dmg"
    r = _run("hdiutil", "create", "-size", "64m", "-fs", CARD_FS, "-volname", "RACARD", "-layout", "MBRSPUD",
             str(dmg))
    assert r.returncode == 0, r.stderr
    whole, mnt = _attach(dmg)
    attached = [whole]
    try:
        part = next(d for d in ("%ss1" % whole,) if os.path.exists(d))
        _diag("macOS statfs of the card volume:", devices._mac_statfs(mnt), "| st_dev of volume:",
              os.stat(mnt).st_dev, "| st_rdev of %s:" % part, os.stat(part).st_rdev)
        src = whole.replace("/dev/", "/dev/r")
        ok, why = devices.check_output(src, os.path.join(mnt, "Recovered"))
        assert not ok and "same disk" in why, why
        # the Mac's own disk is a different disk
        ok, why = devices.check_output(src, str(tmp_path / "Recovered"))
        assert ok, why
        # a disk image stored on the card, attached, used as the output
        inner = os.path.join(mnt, "inner.dmg")
        r = _run("hdiutil", "create", "-size", "16m", "-fs", INNER_FS, "-volname", "RAINNER", inner)
        assert r.returncode == 0, r.stderr
        iwhole, imnt = _attach(inner)
        attached.insert(0, iwhole)
        ok, why = devices.check_output(src, os.path.join(imnt, "Recovered"))
        assert not ok and "same disk" in why, why
        _diag("macOS: card volume, disk image stored on the card: both refused; own disk allowed")
    finally:
        # if the checks above failed, that error is the one pytest reports
        stuck = _detach(attached)
        if stuck:
            _diag("macOS:", _stuck_message(stuck))
    # On a Mac you use, images left attached jam hdiutil and diskutil until a
    # restart: say so loudly. A CI machine is thrown away after the run, so
    # there it is only reported (the save guard's checks above still count).
    if stuck and not os.environ.get("CI"):
        pytest.fail(_stuck_message(stuck))


def test_the_image_stored_on_the_card_is_not_a_second_fskit_volume():
    """A FAT image on the FAT card jammed every disk-image operation on the Mac."""
    assert any(name.lower() in CARD_FS.lower() for name in FSKIT_FILESYSTEMS)      # the card stays FAT
    assert not any(name.lower() in INNER_FS.lower() for name in FSKIT_FILESYSTEMS)


class _Done:
    def __init__(self, code, err=""):
        self.returncode, self.stderr, self.stdout = code, err, ""


def test_detach_reports_images_that_stay_attached():
    calls = []

    def run(*cmd, timeout=None):
        calls.append(cmd[-1])
        if cmd[-1] == "/dev/disk7":
            return _Done(1, "hdiutil: detach: timeout for DiskArbitration expired\n")
        return _Done(0)

    stuck = _detach(["/dev/disk7", "/dev/disk6"], run=run, wait=0)
    assert calls == ["/dev/disk7"] * 4                     # retried, and the outer disk not tried
    assert "DiskArbitration" in stuck["/dev/disk7"]
    assert "not tried" in stuck["/dev/disk6"]
    assert "/dev/disk7" in _stuck_message(stuck)


def test_detach_retries_and_handles_a_hung_hdiutil():
    answers = iter([subprocess.TimeoutExpired("hdiutil", 1), _Done(0), _Done(0)])

    def run(*cmd, timeout=None):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    assert _detach(["/dev/disk7", "/dev/disk6"], run=run, wait=0) == {}


# ------------------------------------------------------------------ Windows
def _diskpart(tmp_path, lines):
    script = tmp_path / "diskpart.txt"
    script.write_text("\n".join(lines) + "\n")
    return _run("diskpart", "/s", str(script))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_card_on_a_vhd_every_spelling(tmp_path):
    import ctypes
    letter = next((c for c in "RSTUVWXYZ" if not os.path.exists(c + ":\\")), None)
    if not letter:
        pytest.skip("no free drive letter")
    vhd = tmp_path / "card.vhd"
    r = _diskpart(tmp_path, ['create vdisk file="%s" maximum=64 type=expandable' % vhd,
                             'select vdisk file="%s"' % vhd, "attach vdisk", "create partition primary",
                             "format fs=fat32 quick label=RACARD", "assign letter=%s" % letter])
    if not os.path.exists(letter + ":\\"):
        pytest.skip("diskpart couldn't attach a .vhd here: %s" % r.stdout[-400:])
    try:
        disks = devices._win_source_disks("\\\\.\\%s:" % letter)
        assert disks, "no disk number for %s:" % letter
        card = "\\\\.\\PhysicalDrive%d" % min(disks)
        guid = ctypes.create_unicode_buffer(1024)
        assert ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(letter + ":\\", guid, 1024)
        link = tmp_path / "CardLink"
        _run("cmd", "/c", "mklink", "/J", str(link), letter + ":\\")
        sysdisk = devices._win_where(os.environ.get("SystemDrive", "C:") + "\\")
        _diag("Windows card disk:", min(disks), "bus", devices._win_bus_type(min(disks)), "| system disk:",
              sorted(sysdisk.disks), [devices._win_bus_type(d) for d in sysdisk.disks], sysdisk.unknown)
        outs = ["%s:\\Recovered" % letter, "%s:/Recovered" % letter.lower(), "\\\\.\\%s:\\Recovered" % letter,
                "\\\\?\\%s:\\Recovered" % letter, guid.value + "Recovered", str(link / "Recovered")]
        if os.path.exists("\\\\localhost\\%s$\\" % letter):
            outs.append("\\\\localhost\\%s$\\Recovered" % letter)
        for src in (card, "\\\\.\\%s:" % letter):
            for out in outs:
                ok, why = devices.check_output(src, out)
                assert not ok and "same disk" in why, (src, out, why)
        ok, why = devices.check_output(card, str(tmp_path / "Recovered"))
        assert ok or not any(k in why for k in devices.DEFINITE), why
        _diag("Windows: system drive as output:", ok, why, "| spellings refused:", len(outs))
        # the .vhd itself as an output: its file could be anywhere, so it is refused
        ok, why = devices.check_output("\\\\.\\PhysicalDrive%d" % min(sysdisk.disks or {0}), "%s:\\Recovered" % letter)
        assert not ok and "virtual disk" in why, why
    finally:
        _run("cmd", "/c", "rmdir", str(tmp_path / "CardLink"))
        _diskpart(tmp_path, ['select vdisk file="%s"' % vhd, "detach vdisk"])
