"""Every file goes to the folder for what it is, not for its container: an
iPhone HEIC photo and a Canon CR3 RAW are ISO BMFF like an MP4, a WAV
recording is RIFF like an AVI."""
import os
import struct

from conftest import SMALL
from recover_anything.carve import group_of
from recover_anything.organize import write_all
from recover_anything.pipeline import scan
from recover_anything.simcard.fat32 import Fat32Card
from recover_anything.simcard.media import make_video


def _heic(size: int = 400_000) -> bytes:
    """An iPhone-style HEIC container: ftyp, meta, mdat."""
    ftyp = struct.pack(">I", 24) + b"ftypheic" + struct.pack(">I", 0) + b"mif1heic"
    body = b"\0\0\0\0" + struct.pack(">I", 33) + b"hdlr" + b"\0" * 8 + b"pict" + b"\0" * 13
    meta = struct.pack(">I", 8 + len(body)) + b"meta" + body
    noise = bytes((i * 131 + (i >> 7)) & 0xFF for i in range(size))
    return ftyp + meta + struct.pack(">I", 8 + size) + b"mdat" + noise


def test_groups_follow_the_file_not_its_container():
    assert group_of("isobmff", ".heic") == "photo"
    assert group_of("isobmff", ".cr3") == "photo"
    assert group_of("isobmff", ".mp4") == "video"
    assert group_of("isobmff", ".m4a") == "audio"
    assert group_of("riff", ".wav") == "audio"
    assert group_of("riff", ".webp") == "photo"
    assert group_of("riff", ".avi") == "video"


def test_heic_goes_to_photos_and_only_photos_means_photos(workdir):
    """Regression: HEIC photos landed in Videos, --only photos skipped them,
    and --only didn't filter the files the folders still name."""
    img = os.path.join(workdir, "heic.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("DCIM/IMG_0001.HEIC", _heic())
        card.write_file("Movies/MVI_0002.MP4", make_video(2, frames=40))
        card.format(recreate_dirs=["DCIM"])  # the photo loses its name; the video's folder survives
    res = scan(img, workers=1, kinds={"photo"})
    assert any(r.ext == ".heic" for r in res.found)
    assert not any(r.ext == ".mp4" for r in res.found)
    out = os.path.join(workdir, "out")
    write_all(scan(img, workers=1), out)
    assert any(f.endswith(".heic") for f in os.listdir(os.path.join(out, "Photos")))
    assert not any(f.endswith(".heic") for d, _, fs in os.walk(os.path.join(out, "Videos")) for f in fs)
