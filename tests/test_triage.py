"""Triage must not call a card erased when its files are still there.

Cameras and phones fill a card from the start of its data area. A formatted
4 GB card holding 64 photos is blank in 98% of places, so random samples all
missed them and triage said "Chances: LOW ... looks fully erased", which sends
people (and the skill) away from a scan that recovers every photo."""
import datetime as dt

import pytest

from recover_anything.simcard.exfat import ExfatCard
from recover_anything.simcard.fat32 import Fat32Card
from recover_anything.triage import format_triage, triage

pytest.importorskip("PIL")

CARDS = {"FAT32": (Fat32Card, 1024), "exFAT": (ExfatCard, 4096)}


def _card(path, fs):
    cls, cluster = CARDS[fs]
    return cls(path, size=96 * 1024**2, cluster_size=cluster)


@pytest.mark.parametrize("fs", sorted(CARDS))
def test_a_formatted_card_with_a_few_photos_is_not_called_erased(tmp_path, fs):
    from recover_anything.simcard.media import make_photo
    from recover_anything.simcard.scenarios import PHOTO_STYLE, _when
    img = str(tmp_path / "card.img")
    with _card(img, fs) as card:
        for i in range(2):
            card.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, make_photo(i, taken=_when(i), style=PHOTO_STYLE),
                            dt.datetime(2026, 7, 4, 9, 30))
        card.format()
    t = triage(img)
    assert t["data_present"] < 0.02                       # the premise: random samples see a blank card
    assert t["verdict"] == "sparse" and t["odds"] == "good", t["summary"]
    assert t["volumes"][0]["starts"] == 2
    assert "erased" not in t["summary"] and "'recover'" in t["summary"]
    assert "2 file start(s)" in format_triage(t)


@pytest.mark.parametrize("fs", sorted(CARDS))
def test_a_blank_card_with_empty_folders_is_still_called_empty(tmp_path, fs):
    """A new card's own tables and a few folders are not files."""
    img = str(tmp_path / "card.img")
    with _card(img, fs) as card:
        for d in ("DCIM/100CANON", "DCIM/101CANON", "MISC"):
            card.mkdir(d)
    t = triage(img)
    assert t["verdict"] == "empty" and t["odds"] == "low"
    assert "Run 'recover' anyway" in t["summary"]


# ---- 1.5: odds that sent people the wrong way ------------------------------
import gzip  # noqa: E402
import os  # noqa: E402
import struct  # noqa: E402
import zlib  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")


def _apfs_image(tmp_path, name):
    """A real APFS container made by macOS (diskutil), stored as its non-blank
    4 KB blocks: 'plain' (one unencrypted volume), 'encrypted' (one volume
    with a password) and 'mixed' (one of each)."""
    raw = gzip.decompress(open(os.path.join(DATA, "apfs-%s.blocks.gz" % name), "rb").read())
    blocks = {struct.unpack_from("<Q", raw, i)[0]: raw[i + 8:i + 8 + 4096] for i in range(0, len(raw), 4104)}
    img = tmp_path / ("apfs-%s.img" % name)
    with open(img, "wb") as f:
        f.truncate((max(blocks) + 64) * 4096)
        for n, b in blocks.items():
            f.seek(n * 4096)
            f.write(b)
    return str(img)


def test_apfs_volumes_report_their_own_encryption(tmp_path):
    from recover_anything.fs import apfs
    from recover_anything.source import Source
    want = {"plain": [("Plain", False)], "encrypted": [("Secret", True)],
            "mixed": [("Plain", False), ("Secret", True)]}
    for name, vols in want.items():
        with Source(_apfs_image(tmp_path, name)) as s:
            assert apfs.volumes(s, 0) == vols


def test_a_damaged_apfs_block_gives_up_instead_of_guessing(tmp_path):
    from recover_anything.fs import apfs
    from recover_anything.source import Source
    img = _apfs_image(tmp_path, "encrypted")
    with Source(img) as s:
        blocks = [n for n in range(s.size // 4096) if s.read(n * 4096 + 32, 4) == b"APSB"]
    with open(img, "r+b") as f:                  # flip a byte in every volume superblock
        for n in blocks:
            f.seek(n * 4096 + 300)
            f.write(b"\x5a")
    with Source(img) as s:
        assert apfs.volumes(s, 0) is None


def test_triage_calls_only_encrypted_apfs_encrypted(tmp_path):
    plain = triage(_apfs_image(tmp_path, "plain"))
    assert plain["verdict"] != "encrypted" and plain["volumes"][0]["filesystem"] == "APFS"
    assert triage(_apfs_image(tmp_path, "encrypted"))["verdict"] == "encrypted"
    mixed = triage(_apfs_image(tmp_path, "mixed"))
    assert mixed["verdict"] != "encrypted"
    assert any("encrypted (Secret)" in w for w in mixed["warnings"])


def test_scan_skips_only_encrypted_apfs(tmp_path):
    from recover_anything.pipeline import scan
    enc = scan(_apfs_image(tmp_path, "encrypted"), workers=1)
    assert any("encrypted" in w for w in enc.warnings)
    plain = scan(_apfs_image(tmp_path, "plain"), workers=1)
    assert not any("encrypted" in w for w in plain.warnings)


def test_deleted_files_the_drive_already_erased(tmp_path):
    """A USB SSD (or a stick that trims) keeps the names of deleted files and
    erases their contents. Lots of other data was still there, so triage said
    "Good news"."""
    from recover_anything.simcard.media import make_photo
    from recover_anything.simcard.scenarios import PHOTO_STYLE, _when
    img = str(tmp_path / "card.img")
    t = dt.datetime(2026, 7, 4, 9, 30)
    with Fat32Card(img, size=96 * 1024**2, cluster_size=1024) as card:
        for i in range(24):
            card.write_file("DCIM/100CANON/KEEP_%04d.JPG" % i, make_photo(i, taken=_when(i), style=PHOTO_STYLE), t)
        gone = {}
        for i in range(6):
            name = "DCIM/100CANON/GONE_%04d.JPG" % i
            gone[name] = card.write_file(name, make_photo(100 + i, taken=_when(i), style=PHOTO_STYLE), t)
        for name in gone:
            card.delete(name)
        offs = [card.cluster_offset(c) for cl in gone.values() for c in cl]
        size = card.cluster_size
    with open(img, "r+b") as f:                       # what TRIM leaves: zeros
        for o in offs:
            f.seek(o)
            f.write(bytes(size))
    t = triage(img)
    assert t["data_present"] > 0.15                   # the premise: plenty of other data
    assert t["verdict"] == "erased" and t["odds"] == "low", t["summary"]
    assert t["erased_check"] == {"checked": 6, "blank": 6}


def test_deleted_files_still_there_are_not_called_erased(tmp_path):
    from recover_anything.simcard.scenarios import deleted
    sc = deleted(str(tmp_path), n=8, size=96 * 1024**2, cluster_size=1024)
    t = triage(sc.image)
    assert t["verdict"] == "recoverable" and t["erased_check"]["blank"] == 0


def _gpt(path, size, parts):
    """A GPT disk: parts = [(type GUID bytes, first LBA, last LBA)]."""
    entries = b"".join(typ + os.urandom(16) + struct.pack("<QQQ", first, last, 0) + bytes(72)
                       for typ, first, last in parts).ljust(128 * 128, b"\0")
    last_lba = size // 512 - 1
    hdr = bytearray(struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000, 92, 0, 0, 1, last_lba, 34,
                                last_lba - 33, os.urandom(16), 2, 128, 128, zlib.crc32(entries)))
    struct.pack_into("<I", hdr, 16, zlib.crc32(bytes(hdr)))
    mbr = bytearray(512)
    mbr[446:462] = bytes([0, 0, 2, 0, 0xEE, 0xFF, 0xFF, 0xFF]) + struct.pack("<II", 1, min(last_lba, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    with open(path, "r+b") as f:
        f.write(bytes(mbr) + bytes(hdr).ljust(512, b"\0") + entries)


def test_an_android_phone_storage_card_is_called_encrypted(tmp_path):
    """A card 'formatted as internal storage' by an Android phone is encrypted
    with a key that stays on the phone; it looked full of data ("Good news")."""
    meta = bytes.fromhex("a210a719cab3e411b02610604b889dcf")
    expand = bytes.fromhex("a41e3d19cab3e411b07510604b889dcf")
    img = str(tmp_path / "card.img")
    size = 32 * 1024**2
    with open(img, "wb") as f:
        f.write(bytes(1 << 20) + os.urandom(size - (1 << 20)))
    _gpt(img, size, [(meta, 2048, 2048 + 32767), (expand, 34816, size // 512 - 34)])
    t = triage(img)
    assert t["verdict"] == "encrypted" and t["odds"] == "low"
    assert "Android" in t["summary"]


def test_a_disk_of_noise_is_called_encrypted_but_a_card_of_photos_is_not(tmp_path):
    """No filesystem and random-looking data everywhere: VeraCrypt or similar.
    A card whose start was wiped, full of photos, looks random too, except that
    photos start at cluster boundaries and their data has JPEG's stuffing."""
    from recover_anything.simcard.media import make_photo
    from recover_anything.simcard.scenarios import PHOTO_STYLE, _when
    noise = str(tmp_path / "noise.img")
    with open(noise, "wb") as f:
        f.write(os.urandom(32 * 1024**2))
    t = triage(noise)
    assert t["verdict"] == "encrypted" and "VeraCrypt" in t["summary"]
    photos = str(tmp_path / "photos.img")
    with open(photos, "wb") as f:
        i = 0
        while f.tell() < 32 * 1024**2:
            b = make_photo(i, taken=_when(i), style=PHOTO_STYLE)
            f.write(b + bytes(-len(b) % 4096))
            i += 1
    t = triage(photos)
    assert t["verdict"] != "encrypted", t["summary"]
