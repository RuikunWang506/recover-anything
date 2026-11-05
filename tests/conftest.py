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


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)
