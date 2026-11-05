"""Files inside other files: a container may only claim what it really holds."""
import io
import os
import struct
import zipfile

import pytest

from conftest import sha
from recover_anything.pipeline import scan

pytest.importorskip("PIL")
from recover_anything.simcard.media import make_photo  # noqa: E402

MB = 1 << 20


def _image(path, size, plants):
    buf = bytearray(size)
    for off, data in plants:
        buf[off:off + len(data)] = data
    with open(path, "wb") as f:
        f.write(buf)
    return path


def _found(path):
    res = scan(path, workers=1)
    src = res._src
    return {sha(r.read(src)): r for r in res.found if r.status == "ok"}, res


def _segment(marker, payload):
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def _tiff(strips, size_claim=None):
    """A minimal little-endian TIFF/RAW header whose strips start at `strips`."""
    n = len(strips)
    entries = [(0x0111, 4, n, 64 if n > 1 else strips[0]),
               (0x0117, 4, n, 64 + 4 * n if n > 1 else (size_claim or 4096))]
    ifd = struct.pack("<H", len(entries)) + b"".join(struct.pack("<HHII", *e) for e in entries) + bytes(4)
    head = (b"II*\x00" + struct.pack("<I", 8) + ifd).ljust(64, b"\x00")
    if n > 1:
        head += struct.pack("<%dI" % n, *strips) + struct.pack("<%dI" % n, *([size_claim or 4096] * n))
    return head


def test_fragmented_raw_does_not_swallow_the_photos_after_it(tmp_path):
    raw = _tiff([4096], size_claim=2 * MB) + os.urandom(60_000)
    photos = [make_photo(500 + i, 640, 480) for i in range(5)]
    plants, off = [(0, raw)], 64 << 10
    for p in photos:
        plants.append((off, p))
        off = (off + len(p) + 4095) // 4096 * 4096
    got, _ = _found(_image(str(tmp_path / "raw.img"), 4 * MB, plants))
    assert all(sha(p) in got for p in photos)


def test_raw_preview_it_points_to_is_not_listed_twice(tmp_path):
    preview = make_photo(7, 320, 240)
    raw = _tiff([4096], size_claim=len(preview)).ljust(4096, b"\x00") + preview
    raw = raw.ljust(200_000, b"\x01")
    got, res = _found(_image(str(tmp_path / "raw2.img"), 2 * MB, [(0, raw)]))
    assert sha(preview) not in got
    assert any(r.kind == "tiff" for r in res.found)


def test_thumbnail_inside_a_photo_is_not_listed_twice(tmp_path):
    thumb = make_photo(8, 160, 120)
    main = make_photo(9, 800, 600)
    # SOI, padding comment, then an APP15 segment holding a whole JPEG at byte 512
    pad = _segment(0xFE, bytes(512 - 2 - 4 - 4))
    photo = main[:2] + pad + _segment(0xEF, thumb) + main[2:]
    assert photo.index(thumb) == 512
    got, _ = _found(_image(str(tmp_path / "thumb.img"), 2 * MB, [(0, photo)]))
    assert sha(photo) in got and sha(thumb) not in got


def test_photo_stored_in_a_zip_is_not_listed_twice(tmp_path):
    inner = make_photo(10, 640, 480)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("p" * (512 - 30), inner)  # member data starts at byte 512
    data = buf.getvalue()
    assert data[512:515] == b"\xff\xd8\xff"
    got, _ = _found(_image(str(tmp_path / "zip.img"), 2 * MB, [(0, data)]))
    assert sha(data) in got and sha(inner) not in got


def test_motion_jpeg_frames_are_not_listed_as_photos(tmp_path):
    frames = [make_photo(20 + i, 320, 240) for i in range(4)]

    def chunk(fourcc, data):
        return fourcc + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")

    movi = b""
    base = 12 + len(chunk(b"LIST", b"hdrl" + chunk(b"avih", bytes(56)))) + 12  # where movi's chunks begin
    for fr in frames:
        pos = base + len(movi)
        gap = (-(pos + 8 + 8)) % 512  # JUNK chunk so the frame's data lands on a sector
        movi += chunk(b"JUNK", bytes(gap)) + chunk(b"00dc", fr)
    body = b"AVI " + chunk(b"LIST", b"hdrl" + chunk(b"avih", bytes(56))) + chunk(b"LIST", b"movi" + movi)
    avi = b"RIFF" + struct.pack("<I", len(body)) + body
    assert all(avi.index(fr) % 512 == 0 for fr in frames)
    got, res = _found(_image(str(tmp_path / "avi.img"), 2 * MB, [(0, avi)]))
    assert sha(avi) in got
    assert not any(sha(fr) in got for fr in frames)
