"""A long run must not look frozen to someone (or an agent) reading its output.
Without a terminal a 5-minute run printed 7 lines; now each stage prints a
line every 30 seconds with how far it is and how long is left, and --json
prints the same as JSON lines. Rebuilding ticks on every attempt: it used to
move only when a photo was rebuilt, and sat at 75% for most of a run."""
import io
import json

from recover_anything.cli import Bar


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _run(bar, clock):
    bar("Checking every file it found")             # a stage's message, then its ticks
    for i in range(100):
        clock.t += 1.0
        bar("check", i, 100)
    clock.t += 1.0
    bar("check", 100, 100)


def test_without_a_terminal_a_line_every_30_seconds():
    out, clock = io.StringIO(), _Clock()
    _run(Bar(out=out, clock=clock), clock)
    lines = out.getvalue().splitlines()
    assert lines[0] == "- Checking every file it found"
    assert "- Checking files" not in lines                 # the same stage, not a new one
    beats = [ln for ln in lines if ln.startswith("  [")]
    assert len(beats) == 3                                  # at 30, 60 and 90 seconds
    assert beats[0] == "  [0:30] Checking files: 29 of 100 (29%), about 1:13 left"


def test_json_lines_with_json():
    out, clock = io.StringIO(), _Clock()
    _run(Bar(as_json=True, out=out, clock=clock), clock)
    events = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert events[0] == {"event": "stage", "stage": "Checking every file it found", "elapsed": 0}
    prog = [e for e in events if e["event"] == "progress"]
    assert len(prog) == 3 and prog[0]["done"] == 29 and prog[0]["total"] == 100 and prog[0]["seconds_left"] == 73


def test_the_check_stage_says_how_long_it_may_take():
    from recover_anything.pipeline import _check_label
    from recover_anything.records import Found
    big = [Found(kind="jpeg", ext=".jpg", extents=[(0, 900 * 10**6)], origin="carved") for _ in range(4)]
    assert _check_label(big, workers=4, fast=False) == \
        "Checking every file it found (up to about 10 minutes for 3.6 GB of photos)"
    assert _check_label(big[:1], workers=8, fast=False) == "Checking every file it found"
    assert _check_label(big, workers=4, fast=True) == "Checking every file it found"


def test_rebuilding_reports_every_attempt(tmp_path):
    """On a card where no split photo can be rebuilt, progress still moves."""
    import datetime as dt
    import os

    from recover_anything.pipeline import scan
    from recover_anything.simcard.fat32 import Fat32Card
    from recover_anything.simcard.media import make_photo
    from recover_anything.simcard.scenarios import PHOTO_STYLE, _when
    path = str(tmp_path / "card.img")
    with Fat32Card(path, size=96 * 1024**2, cluster_size=1024) as card:
        t = dt.datetime(2026, 7, 4, 9, 30)
        for i in range(3):
            card.write_file("DCIM/x%d.tmp" % i, os.urandom(9000), t)
            card.write_file("DCIM/IMG_%04d.JPG" % i, make_photo(i, taken=_when(i), style=PHOTO_STYLE), t)
        for i in range(3):
            card.delete("DCIM/x%d.tmp" % i)
        for i in range(3):
            card.write_file("DCIM/IMG_%04d.JPG" % (10 + i), make_photo(10 + i, taken=_when(i), style=PHOTO_STYLE), t)
        card.format()
    ticks = []
    scan(path, workers=1, progress=lambda st, d=None, n=None: ticks.append((st, d, n)) if st == "reassemble" else None)
    assert ticks, "rebuilding should report progress"
    assert ticks[0][1] == 0                    # before the first attempt ends, not only after a success
    assert all(d is not None and n for _, d, n in ticks)
