"""An MP4/MOV/M4A is only called recovered when its frames were checked
against its own bytes. Boxes that merely line up prove nothing: a deleted
file's clusters may hold another file's data."""
import struct

import pytest

from recover_anything.formats import others
from recover_anything.simcard.media import make_video

pytest.importorskip("PIL")


def _rd(d):
    return lambda o, n: d[o:o + n]


def _codec(d: bytes, codec: bytes) -> bytes:
    """The same file with its video track's sample description changed (the
    last 'avc1' in the file is in the index; the first is a brand name)."""
    i = d.rfind(b"avc1")
    return d[:i] + codec + d[i + 4:]


def _samples(d):
    moov = d.rfind(b"moov") - 4
    stco = d.index(b"stco", moov)
    n = struct.unpack(">I", d[stco + 8:stco + 12])[0]
    return list(struct.unpack(">%dI" % n, d[stco + 12:stco + 12 + 4 * n]))


def test_audio_only_tracks_are_not_called_checked():
    """Regression: an AAC-only file (a voice memo .m4a) counted every sample as
    'checked' though only their positions were, so a deleted one built from
    another file's clusters passed."""
    d = _codec(make_video(5, frames=40), b"mp4a")
    st, why, _ = others.validate_isobmff(_rd(d), len(d))
    assert st == "unverified", why


def test_motion_jpeg_frames_are_checked_by_their_start():
    d = bytearray(_codec(make_video(6, frames=40), b"jpeg"))
    for o in _samples(bytes(d)):
        d[o:o + 3] = b"\xff\xd8\xff"
    assert others.validate_isobmff(_rd(bytes(d)), len(d))[0] == "ok"
    d[_samples(bytes(d))[17]] = 0
    assert others.validate_isobmff(_rd(bytes(d)), len(d))[0] == "corrupt"
