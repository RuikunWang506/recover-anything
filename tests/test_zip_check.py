import io
import struct
import tracemalloc
import zipfile

from recover_anything.validate import validate_zip


def _zip(members, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return bytearray(buf.getvalue())


def _check(b):
    b = bytes(b)
    return validate_zip(lambda o, n: b[o:o + n], len(b))


def test_members_are_checksum_verified():
    b = _zip([("a.txt", b"hello " * 5000), ("b.bin", bytes(range(256)) * 40)])
    assert _check(b) == ("ok", "2 members checksum-verified")
    stored = _zip([("c.bin", bytes(range(256)) * 40)], zipfile.ZIP_STORED)
    assert _check(stored) == ("ok", "1 members checksum-verified")


def test_damaged_member_is_caught():
    b = _zip([("a.bin", bytes(range(256)) * 400)], zipfile.ZIP_STORED)
    b[30 + len("a.bin") + 500] ^= 0xFF
    st, why = _check(b)
    assert st == "corrupt" and "checksum" in why


def test_small_archive_that_inflates_hugely_uses_little_memory():
    b = _zip([("zeros", bytes(64 << 20))])
    assert len(b) < 1 << 20
    tracemalloc.start()
    try:
        st, why = _check(b)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert st == "ok", why
    assert peak < 16 << 20, peak


def test_member_bigger_than_declared_is_corrupt():
    b = _zip([("zeros", bytes(8 << 20))])
    cd = bytes(b).index(b"PK\x01\x02")
    struct.pack_into("<I", b, cd + 24, 1000)  # directory now claims 1000 bytes
    st, why = _check(b)
    assert st == "corrupt" and "bigger than the archive says" in why
