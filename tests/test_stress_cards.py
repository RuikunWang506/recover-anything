"""Two of the randomized stress cards, as regression tests: on these, photos
are intertwined (one photo's second piece starts right after another photo's
first piece) and a video's index ends in a cluster full of an old photo."""
import os

import pytest

from conftest import sha

pytest.importorskip("PIL")
from recover_anything.organize import write_all  # noqa: E402
from recover_anything.pipeline import scan  # noqa: E402
from recover_anything.simcard import scenarios  # noqa: E402


@pytest.mark.parametrize("seed,cluster_size", [(10, 16384), (11, 8192)])
def test_randomized_card_recovers_everything(tmp_path, seed, cluster_size):
    sc = scenarios.stress(str(tmp_path), seed, cluster_size=cluster_size, mixed=seed % 2 == 0)
    res = scan(sc.image)
    out = tmp_path / "out"
    write_all(res, str(out))
    res._src.close()
    got = {}
    for root, _, files in os.walk(out):
        for f in files:
            if f != "recovery.json":
                with open(os.path.join(root, f), "rb") as fh:
                    got[sha(fh.read())] = os.path.relpath(os.path.join(root, f), out)
    missing = sorted(sc.truth[h] for h in sc.truth if h not in got)
    wrong = [p for h, p in got.items() if h not in sc.truth and not p.startswith(("Partly recovered", "Other files"))]
    assert not missing and not wrong, (missing, wrong)
