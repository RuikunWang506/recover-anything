"""The promises that matter most: never write to the source, never save onto it,
never let one bad file stop a recovery, never reuse the wrong card's image."""
import os
import shutil
import subprocess
import sys

import pytest

from conftest import SMALL, recovered_hashes, sha
from recover_anything import devices
from recover_anything.cli import main
from recover_anything.imaging import ImageError, make_image
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.simcard.fat32 import Fat32Card


def _device_for(disks):
    """A device path for one of `disks` as this OS spells it, if it exists."""
    for d in disks:
        if sys.platform == "win32":
            return "\\\\.\\PhysicalDrive%d" % d
        p = "/dev/" + d
        if os.path.exists(p):
            return p
    return None


def test_guard_refuses_output_on_the_same_disk(tmp_path):
    disks, backed = devices.path_disks(str(tmp_path))
    if not backed or not disks:
        pytest.skip("temp folder is not on a local disk here")
    dev = _device_for(disks)
    if not dev:
        pytest.skip("no device node for %s" % disks)
    ok, why = devices.check_output(dev, str(tmp_path / "Recovered"))
    assert not ok and "same disk" in why
    assert devices.output_is_on_source(dev, str(tmp_path))


def test_guard_spelling_variants_are_the_same_device():
    assert devices.normalize_source("//./physicaldrive2") == "\\\\.\\PhysicalDrive2"
    assert devices.normalize_source("\\\\.\\PHYSICALDRIVE2") == "\\\\.\\PhysicalDrive2"
    assert devices.normalize_source("\\\\.\\e:") == "\\\\.\\E:"


def test_folder_as_source_is_refused(tmp_path):
    ok, why = devices.check_output(str(tmp_path), str(tmp_path / "out"))
    assert not ok and "folder" in why
    assert main(["recover", str(tmp_path), "-o", str(tmp_path / "out")]) == 2
    assert not os.path.exists(tmp_path / "out" / "report.html")


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0 or not shutil.which("losetup")
                    or not shutil.which("mkfs.ext4"), reason="needs root, losetup and mkfs.ext4")
def test_guard_sees_through_bind_mounts(tmp_path):
    img = tmp_path / "disk.img"
    with open(img, "wb") as f:
        f.truncate(64 << 20)
    loop = subprocess.run(["losetup", "--find", "--show", str(img)], capture_output=True, text=True,
                          check=True).stdout.strip()
    m, b = tmp_path / "m", tmp_path / "b"
    m.mkdir()
    b.mkdir()
    try:
        subprocess.run(["mkfs.ext4", "-q", loop], check=True)
        subprocess.run(["mount", loop, str(m)], check=True)
        subprocess.run(["mount", "--bind", str(m), str(b)], check=True)
        for out in (m / "Recovered", b / "Recovered", b / "deep" / "Recovered"):
            assert devices.output_is_on_source(loop, str(out))
        assert main(["recover", loop, "-o", str(b / "Recovered"), "--direct"]) == 2
        assert not os.path.exists(b / "Recovered")
    finally:
        subprocess.run(["umount", str(b)])
        subprocess.run(["umount", str(m)])
        subprocess.run(["losetup", "-d", loop])


def test_image_refuses_to_overwrite_its_source(tmp_path):
    src = tmp_path / "Card.img"
    src.write_bytes(os.urandom(1 << 20))
    before = sha(src.read_bytes())
    link = tmp_path / "copy.img"
    os.link(src, link)
    with pytest.raises(ImageError):
        make_image(str(src), str(link))
    assert sha(src.read_bytes()) == before


def test_image_of_another_card_is_never_reused(tmp_path):
    a, b = tmp_path / "a.img", tmp_path / "b.img"
    a.write_bytes(os.urandom(2 << 20))
    b.write_bytes(a.read_bytes()[: 1 << 20] + os.urandom(1 << 20))  # same size, same start
    dest = tmp_path / "card.img"
    make_image(str(a), str(dest))
    with pytest.raises(ImageError):
        make_image(str(b), str(dest))
    make_image(str(a), str(dest))  # same card again: fine
    assert dest.read_bytes() == a.read_bytes()


def test_one_broken_file_does_not_stop_the_scan(workdir):
    pytest.importorskip("PIL")
    from recover_anything.simcard.media import make_photo
    img = os.path.join(workdir, "mixed.img")
    good = [make_photo(i, width=640, height=480) for i in range(4)]
    bad = bytearray(make_photo(99, width=640, height=480))
    sos = bad.find(b"\xff\xda")
    bad[sos + 2:sos + 5] = b"\x00\x03\x03"  # SOS claims 3 components but has room for none
    with Fat32Card(img, **SMALL) as card:
        for i, d in enumerate(good):
            card.write_file("DCIM/IMG_%04d.JPG" % i, d)
        card.write_file("DCIM/IMG_0099.JPG", bytes(bad))
        card.format()
    res = scan(img, workers=1)
    got = recovered_hashes(res)
    assert all(sha(d) in got for d in good)


def test_reformatted_with_different_cluster_size(workdir):
    """Reformatted with other settings: old files no longer sit on cluster
    boundaries of the new layout, so carving must look at every sector."""
    pytest.importorskip("PIL")
    from recover_anything.simcard.media import make_photo
    img = os.path.join(workdir, "re.img")
    photos = [make_photo(i, width=800, height=600) for i in range(8)]
    offsets = []
    with Fat32Card(img, size=SMALL["size"], cluster_size=32768, strict=False) as card:
        for i, d in enumerate(photos):
            offsets.append(card.cluster_offset(card.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, d)[0]))
    with Fat32Card(img, size=SMALL["size"], cluster_size=16384, strict=False, existing=True) as card:
        new_start, new_cs = card.data_offset, card.cluster_size
    survivors = [d for d, o in zip(photos, offsets) if o >= new_start + 2 * new_cs]  # beyond the new FAT and root
    assert len(survivors) >= 4
    assert any((o - new_start) % new_cs for o in offsets), "setup should put old files off the new grid"
    got = recovered_hashes(scan(img, workers=1))
    assert all(sha(d) in got for d in survivors)


def test_zeroed_total_sectors_uses_backup_boot_sector(workdir):
    sc = scenarios.deleted(workdir, 5, **SMALL)
    with open(sc.image, "r+b") as f:
        f.seek(4 * 1024 * 1024 + 32)
        f.write(b"\x00\x00\x00\x00")
    got = recovered_hashes(scan(sc.image, workers=1))
    assert set(sc.truth) <= set(got)
    assert all(got[h].name == n for h, n in sc.truth.items())


def test_truncated_image_keeps_names(workdir):
    sc = scenarios.formatted(workdir, 6, **SMALL)
    res = scan(sc.image, workers=1)
    last = max(o + n for r in res.found for o, n in r.extents)
    res._src.close()
    with open(sc.image, "r+b") as f:
        f.truncate(last + 3 * 4096)  # like a dd that stopped at a read error
    got = recovered_hashes(scan(sc.image, workers=1))
    assert set(sc.truth) <= set(got)
    assert all(got[h].name == n for h, n in sc.truth.items())


def test_long_unicode_names_are_saved(workdir):
    pytest.importorskip("PIL")
    from recover_anything.organize import write_all
    from recover_anything.simcard.exfat import ExfatCard
    from recover_anything.simcard.media import make_photo
    img = os.path.join(workdir, "cjk.img")
    name = "東京旅行の写真" * 3 + ".JPG"
    with ExfatCard(img, **SMALL) as card:
        card.write_file("DCIM/" + name, make_photo(1, width=640, height=480))
        card.write_file("DCIM/IMG_0002.JPG", make_photo(2, width=640, height=480))
        card.delete("DCIM/" + name)
        card.delete("DCIM/IMG_0002.JPG")
    res = scan(img, workers=1)
    w = write_all(res, os.path.join(workdir, "out"))
    assert len(w["written"]) == 2 and not w["failed"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to change owners")
def test_ownership_is_handed_back_only_for_what_the_tool_created(tmp_path, monkeypatch):
    """Under sudo, only files and folders this run created are re-owned: never
    files that were already in the output folder, never symlink targets."""
    pytest.importorskip("PIL")
    from recover_anything.cli import _give_back_to_user
    from recover_anything.organize import write_all
    out, elsewhere = tmp_path / "Backup", tmp_path / "elsewhere"
    (out / "existing").mkdir(parents=True)
    elsewhere.mkdir()
    (elsewhere / "system.conf").write_text("x")
    theirs = out / "existing" / "someone-elses.txt"
    theirs.write_text("keep")
    os.chown(theirs, 4242, 4242)
    os.symlink(elsewhere / "system.conf", out / "link")
    sc = scenarios.deleted(str(tmp_path), 3, **SMALL)
    res = scan(sc.image, workers=1)
    made = []
    w = write_all(res, str(out / "Recovered"), created=made)
    assert w["written"] and all(p.startswith(str(out / "Recovered")) for p in made)
    monkeypatch.setenv("SUDO_UID", "12345")
    monkeypatch.setenv("SUDO_GID", "12345")
    _give_back_to_user(made + [str(out / "link")])
    assert all(os.lstat(p).st_uid == 12345 for p in made)
    assert os.lstat(out / "link").st_uid == 12345          # the link itself...
    assert os.stat(elsewhere / "system.conf").st_uid == 0  # ...not what it points to
    assert os.stat(theirs).st_uid == 4242
    assert os.stat(out).st_uid == 0                        # the folder that was already there
