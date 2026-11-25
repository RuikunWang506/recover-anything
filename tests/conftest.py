import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

SMALL = dict(size=320 * 1024 * 1024, cluster_size=4096)  # 81,920 clusters: a valid FAT32 volume


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def recovered_hashes(res):
    src = res._src
    return {sha(r.read(src)): r for r in res.found if r.status == "ok"}


def saved_hashes(res):
    """Every file saved, whatever its label ("check that it plays" too)."""
    src = res._src
    return {sha(r.read(src)): r for r in res.found if r.extents}


# A split video is rebuilt byte for byte but only labeled "check that it plays"
# when a jump has no frame start checked on one side: there, another deleted
# file's cluster could stand in for one a newer file took (test_stand_in_cluster.py).
UNPROVEN_JOIN = "unverified"


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


@pytest.fixture(scope="session", autouse=True)
def _opened_sources():
    """Every card image opened during the run and not yet closed by
    _close_card_images; whatever is left (a fixture shared by a module that
    didn't close its scan) is closed at the end, before pytest's last cleanup."""
    from recover_anything import source
    opened = []
    init = source.Source.__init__

    def tracked(self, *a, **kw):
        init(self, *a, **kw)
        opened.append(self)
    source.Source.__init__ = tracked
    yield opened
    source.Source.__init__ = init
    for s in opened:
        s.close()


@pytest.fixture(autouse=True)
def _close_card_images(tmp_path, _opened_sources):
    """Close every card image a test opened before pytest deletes the test's
    folder. A Source holds a plain file number, so one never closed stays open
    until the run ends; Windows can't delete an open file, pytest's cleanup
    skips it without a word, and the cards (full size on Windows, 320 MB for
    SMALL) filled the CI machine's disk. (Asking for tmp_path makes this close
    them before that folder goes.)"""
    start = len(_opened_sources)
    yield
    for s in _opened_sources[start:]:
        s.close()
    del _opened_sources[start:]  # (and let go of their read caches)
