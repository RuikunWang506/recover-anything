import hashlib
import os

import pytest

from conftest import SMALL, recovered_hashes, sha
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios

pytest.importorskip("PIL")


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_deleted_keeps_names(workdir, fs):
    sc = scenarios.deleted(workdir, 12, fs=fs, **SMALL)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    assert set(sc.truth) <= set(got)
    for h, name in sc.truth.items():
        assert got[h].name == name
        assert got[h].path == "DCIM/100CANON/"


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_formatted_recovers_names_from_lost_folders(workdir, fs):
    sc = scenarios.formatted(workdir, 12, fs=fs, **SMALL)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    assert set(sc.truth) <= set(got)
    assert all(got[h].name == n for h, n in sc.truth.items())
    assert all(got[h].origin == "lost-folder" for h in sc.truth)


def test_formatted_then_used(workdir):
    sc = scenarios.formatted_then_used(workdir, 16, 5, **SMALL)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    old = sc.groups["old photos"] & set(sc.truth)
    assert sc.overwritten >= 4
    assert old <= set(got)


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_fragmented_photos_are_rebuilt(workdir, fs):
    sc = scenarios.fragmented(workdir, 16, fs=fs, **SMALL)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    frag = sc.groups["fragmented"]
    assert frag <= set(got), "%d/%d fragmented rebuilt" % (len(frag & set(got)), len(frag))
    # nothing marked OK may differ from an original
    stray = [r for h, r in got.items() if h not in sc.truth]
    assert not stray


def test_fragmented_without_folder_info(workdir):
    """Camera format recreates the folders, so only carving + reassembly can help."""
    import datetime as dt
    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.simcard.media import make_photo
    img = os.path.join(workdir, "camfmt.img")
    keep = {}
    with Fat32Card(img, **SMALL) as card:
        for i in range(12):
            card.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, make_photo(i, width=1024, height=768))
        for i in range(0, 12, 2):
            card.delete("DCIM/100CANON/IMG_%04d.JPG" % i)
        for i in range(12, 18):
            d = make_photo(100 + i, width=1400, height=1050)
            card.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, d)
            keep[sha(d)] = i
        card.format(recreate_dirs=["DCIM", "DCIM/100CANON"])
    res = scan(img, workers=1)
    got = recovered_hashes(res)
    assert set(keep) <= set(got)
    assert any(r.origin == "reassembled" for r in got.values())


def test_source_is_never_modified(workdir):
    sc = scenarios.formatted(workdir, 6, **SMALL)
    before = hashlib.sha256(open(sc.image, "rb").read()).hexdigest()
    mtime = os.stat(sc.image).st_mtime_ns
    from recover_anything.cli import main
    out = os.path.join(workdir, "out")
    assert main(["recover", sc.image, "-o", out, "--json", "--workers", "1"]) == 0
    assert hashlib.sha256(open(sc.image, "rb").read()).hexdigest() == before
    assert os.stat(sc.image).st_mtime_ns == mtime


def test_fat32_high_word_wipe(workdir):
    """Some systems zero the top 16 bits of a deleted file's start cluster."""
    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.simcard.media import make_photo
    img = os.path.join(workdir, "hw.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_placeholder("FILLER.BIN", 66000 * 4096)
        photo = make_photo(77, width=800, height=600)
        clusters = card.write_file("DCIM/100CANON/IMG_7777.JPG", photo)
        assert clusters[0] > 0xFFFF
        card.delete("DCIM/100CANON/IMG_7777.JPG", clear_high_word=True)
    res = scan(img, workers=1)
    got = recovered_hashes(res)
    assert sha(photo) in got
    assert got[sha(photo)].name == "IMG_7777.JPG"


def test_other_file_types_are_carved(workdir):
    import io
    import zipfile
    from PIL import Image
    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.simcard.media import make_video
    b = io.BytesIO()
    Image.effect_noise((400, 300), 40).convert("RGB").save(b, "PNG")
    png = b.getvalue()
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document>" + "hello " * 5000 + "</w:document>")
    docx = z.getvalue()
    pdf = b"%PDF-1.4\n" + b"1 0 obj << /Type /Catalog >> endobj\n" * 2000 + b"trailer\n%%EOF\n"
    vid = make_video(3, frames=60)
    img = os.path.join(workdir, "mixed.img")
    with Fat32Card(img, **SMALL) as card:
        for name, data in (("a.png", png), ("report.docx", docx), ("paper.pdf", pdf), ("clip.mp4", vid)):
            card.write_file("FILES/" + name, data)
        card.format()
    res = scan(img, workers=1)
    got = recovered_hashes(res)
    for data in (png, docx, pdf, vid):
        assert sha(data) in got
    exts = {r.ext for r in got.values()}
    assert {".png", ".docx", ".pdf", ".mp4"} <= exts


@pytest.mark.parametrize("moov_first", [False, True])
def test_fragmented_video_is_rebuilt(workdir, moov_first):
    """Camera fills holes left by deleted photos with a video, then the card is
    camera-formatted. The video's own frame index guides reassembly."""
    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.simcard.media import make_photo, make_video
    img = os.path.join(workdir, "vid.img")
    with Fat32Card(img, **SMALL) as card:
        for i in range(10):
            card.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, make_photo(i, width=800, height=600))
        for i in range(0, 10, 2):
            card.delete("DCIM/100CANON/IMG_%04d.JPG" % i)
        v = make_video(40, frames=150, moov_first=moov_first)
        cl = card.write_file("DCIM/100CANON/MVI_0001.MP4", v)
        card.format(recreate_dirs=["DCIM", "DCIM/100CANON"])
    assert any(b != a + 1 for a, b in zip(cl, cl[1:])), "test setup should fragment the video"
    res = scan(img, workers=1)
    got = recovered_hashes(res)
    assert sha(v) in got
    assert got[sha(v)].meta.get("reassembled_from", 1) > 1


def test_fragmented_video_with_long_pcm_audio_is_rebuilt(workdir):
    """QuickTime uncompressed audio lists every audio sample (48,000 a second).
    A long audio table must not stop the video from being checked and rebuilt."""
    import struct

    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.simcard.media import _box, _full, make_photo, make_video
    n = 48000 * 52
    stsd = _full(b"stsd", 0, 0, struct.pack(">I", 1) + _box(b"sowt", bytes(6) + struct.pack(">H", 1) + bytes(20)))
    stbl = _box(b"stbl", stsd + _full(b"stts", 0, 0, struct.pack(">III", 1, n, 1))
                + _full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, n, 1))
                + _full(b"stsz", 0, 0, struct.pack(">II", 1, n)) + _full(b"stco", 0, 0, struct.pack(">II", 1, 64)))
    trak = _box(b"trak", _box(b"mdia", _full(b"hdlr", 0, 0, bytes(4) + b"soun" + bytes(13)) + _box(b"minf", stbl)))
    v = make_video(41, frames=150, avg_frame=8000)
    m = v.rfind(b"moov") - 4
    v = v[:m] + struct.pack(">I", struct.unpack(">I", v[m:m + 4])[0] + len(trak)) + v[m + 4:] + trak
    img = os.path.join(workdir, "pcm.img")
    with Fat32Card(img, **SMALL) as card:
        for i in range(3):
            card.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, make_photo(600 + i, 640, 480))
        card.delete("DCIM/100CANON/IMG_0001.JPG")
        cl = card.write_file("DCIM/100CANON/MVI_0041.MOV", v)
        card.format()
    assert any(b != a + 1 for a, b in zip(cl, cl[1:])), "test setup should fragment the video"
    got = recovered_hashes(scan(img, workers=1))
    assert sha(v) in got


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
def test_damaged_boot_sector_uses_backup(workdir, fs):
    """'This card needs to be formatted': the boot sector is trashed, the rest is fine."""
    sc = scenarios.deleted(workdir, 6, fs=fs, **SMALL)
    part = 4 * 1024 * 1024  # simulated cards are partitioned at 4 MiB like the SD formatter does
    with open(sc.image, "r+b") as f:
        f.seek(part)
        f.write(b"\x00" * 512)
    res = scan(sc.image, workers=1)
    got = recovered_hashes(res)
    assert set(sc.truth) <= set(got)
    assert all(got[h].name == n for h, n in sc.truth.items())
    assert any("backup" in w for w in res.warnings)
