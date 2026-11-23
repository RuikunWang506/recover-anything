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
