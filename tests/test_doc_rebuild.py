"""Word/Excel (ZIP) files and PDFs stored in pieces are put back together,
and proven by their own checksums and tables, not guessed."""
import io
import os
import random
import zipfile

from conftest import SMALL, recovered_hashes, sha
from recover_anything.formats import others
from recover_anything.pipeline import scan
from recover_anything.simcard import files as F
from recover_anything.simcard.fat32 import Fat32Card


def _noise(seed, n):
    return random.Random(seed).randbytes(n)


def _split_card(workdir, name, data, hole_clusters=3):
    """A card where `data` is written into a hole left by a deleted file, so it
    is split around the file after the hole, then the card is formatted."""
    img = os.path.join(workdir, "split.img")
    with Fat32Card(img, **SMALL) as card:
        card.write_file("a.bin", _noise(1, 20000))
        card.write_file("hole.bin", _noise(2, 4096 * hole_clusters))
        card.write_file("b.bin", _noise(3, 30000))
        card.delete("hole.bin")
        cl = card.write_file(name, data)
        card.format()
    assert any(b != a + 1 for a, b in zip(cl, cl[1:])), "the file should be in pieces"
    return img


def _docx(picture: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + "a letter to the bank " * 2000 + "</w:document>")
        z.writestr(zipfile.ZipInfo("word/media/image1.jpeg"), _noise(4, picture))  # stored, as Word does
    return buf.getvalue()


def test_a_split_word_file_is_rebuilt(workdir):
    """Regression: a split ZIP was dropped without a trace (the search for its
    end ran to the end of the card), and nothing could rebuild one. Its stored
    picture spans more clusters than the rebuild used to look ahead."""
    data = _docx(60000)
    got = recovered_hashes(scan(_split_card(workdir, "Documents/letter.docx", data), workers=1))
    assert sha(data) in got and got[sha(data)].status == "ok"


def test_a_split_pdf_is_rebuilt(workdir):
    data = F.make_pdf(12, pages=8)
    got = recovered_hashes(scan(_split_card(workdir, "Documents/report.pdf", data), workers=1))
    assert sha(data) in got and got[sha(data)].status == "ok"


def test_a_split_zip_is_kept_as_its_first_piece():
    data = _docx(30000)
    cut = data[:8192] + _noise(5, 40000)  # another file's data where its second piece should be
    m = others.measure_zip(lambda o, n: cut[o:o + n], 0, len(cut))
    assert not m.complete and 0 < m.end <= len(cut)


def test_a_pdf_without_its_end_is_kept_as_its_first_piece():
    data = F.make_pdf(13)
    cut = data[:20000] + bytes(1 << 20)
    m = others.measure_pdf(lambda o, n: cut[o:o + n], 0, len(cut))
    assert not m.complete and 20000 <= m.end < len(cut)


def test_an_android_app_with_its_signing_block_is_whole():
    """An APK keeps a signing block between its last member and its directory:
    the chain of members breaks there, but the archive is whole."""
    import struct
    data = _docx(1000)
    eocd = data.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", data[eocd + 16:eocd + 20])[0]
    block = struct.pack("<Q", 4016) + _noise(6, 4000) + b"APK Sig Block 42"
    apk = data[:cd_off] + block + data[cd_off:eocd + 16] + struct.pack("<I", cd_off + len(block)) + data[eocd + 20:]
    m = others.measure_zip(lambda o, n: apk[o:o + n], 0, len(apk) + 4096)
    assert m.complete and m.end == len(apk)
