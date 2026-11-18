"""'This card needs to be formatted': the start of the card is damaged and no
computer can open it, though nothing was deleted. A default recovery used to
say "Nothing recoverable was found" and call the photos "still visible"."""
import hashlib
import os

import pytest

from recover_anything import organize
from recover_anything.pipeline import scan
from recover_anything.simcard import scenarios
from recover_anything.triage import triage


def _written(res, out):
    w = organize.write_all(res, str(out))
    return {hashlib.sha256(open(r.meta["written_to"], "rb").read()).hexdigest(): os.path.basename(r.meta["written_to"])
            for r in w["written"]}


@pytest.mark.parametrize("fs", ["FAT32", "exFAT"])
@pytest.mark.parametrize("damage", ["boot", "mbr"])
def test_every_photo_on_a_card_the_computer_cant_open_is_copied(tmp_path, fs, damage):
    sc = scenarios.unopenable(str(tmp_path), fs=fs, damage=damage)
    res = scan(sc.image, workers=1)
    got = _written(res, tmp_path / "out")
    assert got == sc.truth                       # every photo, byte for byte, under its own name
    assert res.live_count == 0                   # none of them is "still visible"
    t = triage(sc.image)
    assert t["listed_but_unopenable"] == 10 and t["visible_files"] == 0
    assert t["odds"] == "good" and "Don't format it" in t["summary"]


def test_a_card_the_computer_can_open_still_leaves_visible_files_alone(tmp_path):
    sc = scenarios.unopenable(str(tmp_path), damage="none")     # nothing wiped: a healthy card
    res = scan(sc.image, workers=1)
    assert _written(res, tmp_path / "out") == {}
    assert res.live_count == 10
    assert triage(sc.image)["visible_files"] == 10


def test_a_gpt_disk_with_a_damaged_primary_header_opens_from_its_backup(tmp_path):
    """Every OS falls back to the backup GPT header in the last sector: such a
    disk opens normally, so it is not "a card the computer can't open"."""
    import datetime as dt
    import struct
    import zlib

    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.source import Source
    from recover_anything.volume import find_volumes
    img = str(tmp_path / "gpt.img")
    size = 96 * 1024**2
    with Fat32Card(img, size=size, cluster_size=1024) as c:
        for i in range(3):
            c.write_file("DCIM/100CANON/IMG_%04d.JPG" % i, b"\xff\xd8\xff\xe1" + bytes([i]) * 30000,
                         dt.datetime(2026, 1, 1))
    nsec = size // 512
    first, last = (4 << 20) // 512, (size - (1 << 20)) // 512
    entry = bytearray(128)
    entry[0:16] = bytes.fromhex("a2a0d0ebe5b9334487c068b6b72699c7")      # basic data
    entry[16:32] = bytes(range(16))
    struct.pack_into("<QQ", entry, 32, first, last)
    entries = bytes(entry) + bytes(128 * 127)

    def header(my, alt, entries_lba):
        h = bytearray(92)
        struct.pack_into("<8sIII", h, 0, b"EFI PART", 0x10000, 92, 0)
        struct.pack_into("<QQQQ", h, 24, my, alt, 34, nsec - 34)
        h[56:72] = bytes(range(16, 32))
        struct.pack_into("<QIII", h, 72, entries_lba, 128, 128, zlib.crc32(entries) & 0xFFFFFFFF)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h) + bytes(420)

    with open(img, "r+b") as f:
        mbr = bytearray(512)
        mbr[446 + 4] = 0xEE
        struct.pack_into("<II", mbr, 446 + 8, 1, nsec - 1)
        mbr[510:512] = b"\x55\xaa"
        f.write(mbr)
        primary = bytearray(header(1, nsec - 1, 2))
        primary[20] ^= 0xFF                                              # damaged primary header
        f.seek(512)
        f.write(primary)
        f.write(entries)
        f.seek((nsec - 33) * 512)
        f.write(entries)
        f.seek((nsec - 1) * 512)
        f.write(header(nsec - 1, 1, nsec - 33))                         # intact backup
    vols = find_volumes(Source(img))
    assert [(v.offset, v.fs, v.os_cannot_open) for v in vols] == [(first * 512, "FAT32", False)]
    res = scan(img, workers=1, reassemble=False)
    assert not any(r.meta.get("card_unreadable") for r in res.found)
