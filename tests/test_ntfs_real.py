"""The NTFS reader against a real NTFS volume: made by mkntfs, written by the
ntfs-3g driver, files deleted the ordinary way. The simulated drive in
simcard/ntfs.py follows the format docs, so it could share the reader's
misunderstandings; this can't.

Needs Linux, root, mkntfs and ntfs-3g (Debian/Ubuntu package ntfs-3g):

    sudo python -m pytest tests/test_ntfs_real.py

CI runs it on Ubuntu. Skipped elsewhere."""
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

from recover_anything import organize
from recover_anything.pipeline import scan

# Builds $IMG from the files in $SRC. A file grown in steps, with other files
# written in between, is the dependable way to get ntfs-3g to store it in
# pieces (filling the volume doesn't: it falls back to the MFT's reserve).
BUILD = r"""
set -e
truncate -s 64M "$IMG"
mkntfs -F -Q -q -c 4096 -L REALNTFS "$IMG"
ntfs-3g "$IMG" "$MNT"
trap 'umount "$MNT" 2>/dev/null || true' EXIT
mkdir -p "$MNT/Photos/Lisbon" "$MNT/Photos/Family" "$MNT/Photos/Old" "$MNT/Documents/Thesis" "$MNT/Camera Roll" "$MNT/other"
cp "$SRC/IMG_0001.JPG" "$SRC/IMG_0002.JPG" "$SRC/IMG_0003.JPG" "$MNT/Photos/Lisbon/"
cp "$SRC/IMG_0004.JPG" "$SRC/IMG_0005.JPG" "$MNT/Photos/Family/"
cp "$SRC/IMG_0006.JPG" "$SRC/IMG_0007.JPG" "$MNT/Photos/Old/"
cp "$SRC/notes.txt" "$SRC/tiny.txt" "$MNT/Documents/Thesis/"
n=$(( ($(stat -c %s "$SRC/IMG_0008.JPG") + 65535) / 65536 ))
k=0
while [ $k -lt $n ]; do
  dd if="$SRC/IMG_0008.JPG" of="$MNT/Camera Roll/IMG_0008.JPG" bs=65536 skip=$k seek=$k count=1 conv=notrunc 2>/dev/null
  sync
  head -c 12288 /dev/urandom > "$MNT/other/o$k.bin"
  sync
  k=$((k+1))
done
cmp "$SRC/IMG_0008.JPG" "$MNT/Camera Roll/IMG_0008.JPG"
sync
rm "$MNT/Photos/Lisbon/IMG_0001.JPG" "$MNT/Photos/Family/IMG_0004.JPG"
rm "$MNT/Documents/Thesis/notes.txt" "$MNT/Documents/Thesis/tiny.txt"
rm "$MNT/Camera Roll/IMG_0008.JPG"
rm -r "$MNT/Photos/Old"
sync
umount "$MNT"
trap - EXIT
"""

DELETED = {"IMG_0001.JPG": "Photos/Lisbon", "IMG_0004.JPG": "Photos/Family", "IMG_0006.JPG": "Photos/Old",
           "IMG_0007.JPG": "Photos/Old", "IMG_0008.JPG": "Camera Roll", "notes.txt": "Documents/Thesis",
           "tiny.txt": "Documents/Thesis"}


def make_sources(folder) -> dict:
    """The files the volume is built from; returns name -> sha256."""
    from recover_anything.simcard.media import make_photo
    from recover_anything.simcard.scenarios import PHOTO_STYLE, _when
    os.makedirs(folder, exist_ok=True)
    files = {"IMG_%04d.JPG" % i: make_photo(i, taken=_when(i), style=PHOTO_STYLE) for i in range(1, 9)}
    files["notes.txt"] = ("Chapter 3: results\n" * 3000).encode()
    files["tiny.txt"] = b"a small file kept inside its MFT record\n"
    for name, data in files.items():
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def check(img, shas, out) -> None:
    """Everything deleted comes back byte for byte, named and in its folder;
    nothing is marked recovered wrongly; the visible photos are left alone."""
    from recover_anything.fs.ntfs import NtfsVolume
    from recover_anything.source import Source
    from recover_anything.volume import find_volumes, open_filesystem
    with Source(img) as src:
        fsr = open_filesystem(src, find_volumes(src)[0])
        assert isinstance(fsr, NtfsVolume)
        recs = {r.name: r for r in fsr.walk()}
    grown = recs["IMG_0008.JPG"]
    assert grown.extents, "the grown photo's list of pieces couldn't be read: %s" % grown.note
    assert len(grown.extents) >= 2, "ntfs-3g stored the grown photo in one piece; the test needs it split"
    res = scan(img, workers=1)
    w = organize.write_all(res, str(out))
    got = {}
    for r in w["written"]:
        got[r.name] = (hashlib.sha256(open(r.meta["written_to"], "rb").read()).hexdigest(), r)
    for name, folder in DELETED.items():
        assert name in got, "%s not recovered" % name
        sha, r = got[name]
        assert sha == shas[name], "%s not byte for byte" % name
        assert r.path == folder, (name, r.path)
    for name, (sha, r) in got.items():
        if r.status == "ok":
            assert shas.get(name) == sha, "%s marked recovered but wrong" % name
    assert res.live_count >= 4                     # IMG_0002, 0003, 0005 and the other/ files stay put
    assert "IMG_0002.JPG" not in got


def _missing():
    if not sys.platform.startswith("linux"):
        return "Linux only"
    if os.geteuid() != 0:
        return "needs root to mount with ntfs-3g (sudo python -m pytest tests/test_ntfs_real.py)"
    for tool in ("mkntfs", "ntfs-3g"):
        if not shutil.which(tool):
            return "needs %s (package ntfs-3g)" % tool
    if not os.path.exists("/dev/fuse"):
        return "needs /dev/fuse"
    return None


@pytest.mark.skipif(_missing() is not None, reason=_missing() or "")
def test_deleted_files_on_a_real_ntfs_volume(tmp_path):
    shas = make_sources(tmp_path / "src")
    os.makedirs(tmp_path / "mnt")
    env = dict(os.environ, IMG=str(tmp_path / "real.img"), MNT=str(tmp_path / "mnt"), SRC=str(tmp_path / "src"))
    r = subprocess.run(["sh", "-c", BUILD], env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    check(str(tmp_path / "real.img"), shas, tmp_path / "out")
