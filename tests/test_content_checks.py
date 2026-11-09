"""PDF, AVI, GIF, WAV and RAW: a deleted file is only called recovered when
its contents were checked, not just its structure."""
import random

import pytest

from recover_anything.formats import checks, others
from recover_anything.simcard import files as F

pytest.importorskip("PIL")


def _rd(d):
    return lambda o, n: d[o:o + n]


def _splice(d, other, cs=4096):
    """d with one cluster in the middle replaced by the same cluster of another
    file of the same kind: what a reused cluster looks like."""
    k = (len(d) // cs) // 2 * cs
    return d[:k] + other[k:k + cs] + d[k + cs:]


def test_a_pdf_stops_at_the_next_file():
    """Regression: the end-marker search read 1 MB at a time past its limit and
    took the last end marker it saw, which was the next PDF's."""
    a, b = F.make_pdf(1), F.make_pdf(2)
    card = a + bytes(-len(a) % 4096) + b
    assert others.measure_pdf(_rd(card), 0, len(a) + (-len(a) % 4096)).end == len(a)


def test_a_pdf_ends_at_its_own_end_marker():
    """Regression: a deleted PDF's leftover tail (with its own end marker) in the
    free space after a PDF was counted as part of it, and its table, pointing
    past the end, listed nothing to check, so the file passed."""
    new, old = F.make_pdf(3), F.make_pdf(4, pages=12)
    card = new + bytes(3000) + old[-30000:]
    assert others.measure_pdf(_rd(card), 0, len(card)).end == len(new)
    assert checks.validate_pdf(_rd(card), len(card))[0] != "ok"
    # an update appended to a PDF is part of it
    obj = b"5 0 obj\n<< /Producer (x) >>\nendobj\n"
    upd = (new + obj + b"xref\n5 1\n%010d 00000 n \ntrailer\n<< /Size 52 >>\nstartxref\n%d\n%%%%EOF\n"
           % (len(new), len(new) + len(obj)))
    assert others.measure_pdf(_rd(upd + bytes(100)), 0, len(upd) + 100).end == len(upd)


@pytest.mark.parametrize("kw", [{}, dict(xref_stream=True)], ids=["xref-table", "xref-stream"])
def test_a_pdf_is_checked_object_by_object(kw):
    d = F.make_pdf(5, **kw)
    assert checks.validate_pdf(_rd(d), len(d)) == ("ok", "every object and stream checked", None)
    bad = _splice(d, F.make_pdf(6, **kw))
    st, why, at = checks.validate_pdf(_rd(bad), len(bad))
    assert st == "corrupt" and at is not None, why


def test_an_uncompressed_pdf_proves_nothing_once_deleted():
    d = F.make_pdf(7, raw_streams=True)
    assert checks.validate_pdf(_rd(d), len(d))[0] == "unverified"
    assert checks.validate_pdf(_rd(d), len(d), layout_known=True)[0] == "ok"


def test_avi_frames_are_decoded():
    d = F.make_avi(1, frames=20)
    assert checks.validate_riff(_rd(d), len(d))[0] == "ok"
    bad = _splice(d, F.make_avi(2, frames=20))
    assert checks.validate_riff(_rd(bad), len(bad))[0] == "corrupt"


def test_an_avi_index_must_match():
    d = bytearray(F.make_avi(3, frames=10))
    i = d.rfind(b"idx1") + 8 + 16 + 12  # the second entry's size
    d[i] ^= 1
    assert checks.validate_riff(_rd(bytes(d)), len(d))[0] == "corrupt"


def test_wav_and_raw_prove_nothing_once_deleted():
    for d in (F.make_wav(1), F.make_raw(1)):
        fn = checks.validate_riff if d[:4] == b"RIFF" else checks.validate_tiff
        assert fn(_rd(d), len(d))[0] == "unverified"
        assert fn(_rd(d), len(d), layout_known=True)[0] == "ok"


def test_a_raw_with_a_damaged_preview_is_caught():
    import struct
    d = bytearray(F.make_raw(2))
    ifd0 = struct.unpack("<I", d[4:8])[0]
    entries = [d[ifd0 + 2 + 12 * i:ifd0 + 14 + 12 * i] for i in range(struct.unpack("<H", d[ifd0:ifd0 + 2])[0])]
    p = next(struct.unpack("<I", e[8:12])[0] for e in entries if e[:2] == b"\x11\x01")  # the preview's offset
    d[p + 8192:p + 12288] = random.Random(2).randbytes(4096)  # a cluster of something else
    assert checks.validate_tiff(_rd(bytes(d)), len(d))[0] == "corrupt"


def test_a_gif_is_decoded():
    d = F.make_gif(1, frames=3)
    assert checks.validate_gif(_rd(d), len(d))[0] == "ok"
    bad = _splice(d, F.make_gif(2, frames=3), cs=512)
    assert checks.validate_gif(_rd(bad), len(bad))[0] == "corrupt"


class _Card:
    def __init__(self, data):
        self.data, self.size = data, len(data)

    def read(self, o, n):
        return self.data[o:o + n]

    read_uncached = read


def _carve_pdfs(card):
    from recover_anything import carve
    got, _ = carve.carve(_Card(card), 0, len(card), 512, 0, set(), carve.Claimed())
    return [(r.start, r.size) for r in got if r.kind == "pdf"]


def test_a_pdf_holding_a_picture_is_measured_to_its_end():
    """Regression: a PDF was only measured up to the next header on the card, so
    one holding a picture that starts a sector (or compressed data that looks
    like a header by chance) had no end marker in reach and was lost."""
    from recover_anything.simcard.media import make_photo
    pdf = bytearray(F.make_pdf(9))
    jpg = make_photo(3, 320, 240)
    k = (len(pdf) // 2) // 512 * 512
    pdf[k:k + len(jpg)] = jpg
    card = bytearray(1 << 20)
    card[65536:65536 + len(pdf)] = pdf
    assert _carve_pdfs(bytes(card)) == [(65536, len(pdf))]


def test_a_cut_off_pdf_does_not_swallow_the_next_one():
    a, b = F.make_pdf(10)[:-6000], F.make_pdf(11)  # the first lost its end
    at = 65536 + (len(a) + 4095) // 4096 * 4096
    card = bytearray(1 << 20)
    card[65536:65536 + len(a)] = a
    card[at:at + len(b)] = b
    assert (at, len(b)) in _carve_pdfs(bytes(card))


def test_a_raw_cut_short_is_not_called_recovered():
    """Regression: a RAW whose sensor data ran past the bytes at hand (an old
    folder entry's smaller size over a newer photo, or the end of the card)
    lost that block from the check, and its previews alone called it recovered."""
    d = F.make_raw(3)
    cut = len(d) - 200_000  # inside the sensor data, the last thing in the file
    m = others.measure_tiff(_rd(d), 0, cut)
    assert not m.complete and m.end <= cut
    assert checks.validate_tiff(_rd(d[:cut]), cut)[0] == "partial"
    assert checks.validate_tiff(_rd(d[:cut]), cut, layout_known=True)[0] == "partial"


def _tiff(entries, data_at, data):
    """A big-endian TIFF: one IFD at 8 with (tag, type, count, value field) entries."""
    import struct
    ifd = struct.pack(">H", len(entries)) + b"".join(struct.pack(">HHI", t, ty, c) + v for t, ty, c, v in entries)
    head = b"MM\0*" + struct.pack(">I", 8) + ifd + bytes(4)
    return head + bytes(data_at - len(head)) + data


def test_tiff_values_kept_in_the_entry_itself():
    """Regression: up to two 16-bit values sit in the entry itself, not where
    its value field would point; read as an offset, the picture data vanished."""
    import struct
    d = _tiff([(0x100, 3, 1, struct.pack(">HH", 64, 0)), (0x101, 3, 1, struct.pack(">HH", 64, 0)),
               (0x103, 3, 1, struct.pack(">HH", 1, 0)), (0x111, 3, 2, struct.pack(">HH", 200, 2200)),
               (0x117, 3, 2, struct.pack(">HH", 2000, 2000))], 200, random.Random(4).randbytes(4000))
    m = others.measure_tiff(_rd(d), 0, len(d))
    assert m.complete and m.end == 4200 == len(d)
    assert not others.measure_tiff(_rd(d), 0, 3000).complete


def test_a_tiff_cut_before_its_next_directory_is_not_complete():
    import struct
    d = bytearray(_tiff([(0x100, 3, 1, struct.pack(">HH", 8, 0)), (0x111, 4, 1, struct.pack(">I", 100)),
                         (0x117, 4, 1, struct.pack(">I", 3000))], 100, random.Random(5).randbytes(3000)))
    nxt = len(d)
    d[8 + 2 + 36:8 + 2 + 36 + 4] = struct.pack(">I", nxt)  # a second IFD after the picture data
    d += struct.pack(">H", 1) + struct.pack(">HHI", 0x100, 3, 1) + struct.pack(">HH", 8, 0) + bytes(4)
    assert others.measure_tiff(_rd(bytes(d)), 0, len(d)).complete
    assert not others.measure_tiff(_rd(bytes(d)), 0, nxt).complete
