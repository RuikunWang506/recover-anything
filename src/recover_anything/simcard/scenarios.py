"""The standard disaster scenarios, with ground truth, used by `selftest` and the tests."""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
from dataclasses import dataclass, field

from .fat32 import Fat32Card
from .media import make_photo, make_video


@dataclass
class Scenario:
    name: str
    title: str
    image: str
    truth: dict                     # sha256 -> original name, for files that *should* be recoverable
    overwritten: int = 0            # files whose data was physically destroyed
    groups: dict = field(default_factory=dict)  # label -> set of sha256 (e.g. fragmented files)
    fs: str = "FAT32"


PHOTO_STYLE = "landscape"  # photo-like test images (sky, mountains, water, trees + sensor noise)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _when(i: int) -> _dt.datetime:
    return _dt.datetime(2026, 7, 4, 9, 30, 0) + _dt.timedelta(minutes=11 * i, seconds=7 * i)


def _shoot(card, start: int, count: int, folder="DCIM/100CANON", seed_base=0, **kw):
    shots = []
    for i in range(start, start + count):
        data = make_photo(seed_base + i, taken=_when(i), style=PHOTO_STYLE, **kw)
        name = "IMG_%04d.JPG" % i
        card.write_file("%s/%s" % (folder, name), data, _when(i))
        shots.append((name, data))
    return shots


def _card(path, fs, **kw):
    if fs == "exFAT":
        from .exfat import ExfatCard
        return ExfatCard(path, **kw)
    return Fat32Card(path, **kw)


def deleted(workdir: str, n: int = 63, fs: str = "FAT32", size: int = 4 * 1024**3, cluster_size: int = 32768) -> Scenario:
    img = os.path.join(workdir, "deleted-%s.img" % fs.lower())
    with _card(img, fs, size=size, cluster_size=cluster_size) as card:
        shots = _shoot(card, 1, n)
        clip = make_video(900, taken=_when(n + 1))
        card.write_file("DCIM/100CANON/MVI_%04d.MP4" % (n + 1), clip, _when(n + 1))
        for name, _ in shots:
            card.delete("DCIM/100CANON/" + name)
        card.delete("DCIM/100CANON/MVI_%04d.MP4" % (n + 1))
    truth = {_sha(d): name for name, d in shots}
    truth[_sha(clip)] = "MVI_%04d.MP4" % (n + 1)
    return Scenario("deleted", "Accidentally deleted", img, truth, fs=fs)


def formatted(workdir: str, n: int = 63, fs: str = "FAT32", size: int = 4 * 1024**3, cluster_size: int = 32768) -> Scenario:
    img = os.path.join(workdir, "formatted-%s.img" % fs.lower())
    with _card(img, fs, size=size, cluster_size=cluster_size) as card:
        shots = _shoot(card, 1, n)
        clip = make_video(901, taken=_when(n + 1))
        card.write_file("DCIM/100CANON/MVI_%04d.MP4" % (n + 1), clip, _when(n + 1))
        card.format()
    truth = {_sha(d): name for name, d in shots}
    truth[_sha(clip)] = "MVI_%04d.MP4" % (n + 1)
    return Scenario("formatted", "Formatted", img, truth, fs=fs)


def formatted_then_used(workdir: str, n: int = 63, new: int = 20, fs: str = "FAT32", size: int = 4 * 1024**3,
                        cluster_size: int = 32768) -> Scenario:
    """Format in camera (it recreates DCIM/100CANON), then keep shooting."""
    img = os.path.join(workdir, "reused-%s.img" % fs.lower())
    with _card(img, fs, size=size, cluster_size=cluster_size) as card:
        old = []
        for i in range(1, n + 1):
            data = make_photo(i, taken=_when(i), style=PHOTO_STYLE)
            name = "IMG_%04d.JPG" % i
            clusters = card.write_file("DCIM/100CANON/" + name, data, _when(i))
            old.append((name, data, set(clusters)))
        card.format(recreate_dirs=["DCIM", "DCIM/100CANON"])
        fresh = _shoot(card, 1, new, seed_base=5000)
        used = card.allocated_clusters()
    truth, gone = {}, 0
    for name, d, clusters in old:
        if clusters & used:
            gone += 1  # physically overwritten by the new photos: gone for good
        else:
            truth[_sha(d)] = name
    for name, d in fresh:
        truth[_sha(d)] = "(new) " + name
    sc = Scenario("reused", "Formatted, then %d new photos taken" % new, img, truth, overwritten=gone, fs=fs)
    sc.groups["old photos"] = {_sha(d) for _, d, _ in old}
    sc.groups["new photos"] = {_sha(d) for _, d in fresh}
    return sc


def fragmented(workdir: str, n: int = 60, fs: str = "FAT32", size: int = 4 * 1024**3, cluster_size: int = 32768) -> Scenario:
    """Shoot, delete every other photo in camera, shoot bigger photos that fill
    the holes (so each one is split across several holes), then format."""
    img = os.path.join(workdir, "fragmented-%s.img" % fs.lower())
    with _card(img, fs, size=size, cluster_size=cluster_size) as card:
        first = _shoot(card, 1, n)
        for i, (name, _) in enumerate(first):
            if i % 2 == 0:
                card.delete("DCIM/100CANON/" + name)
        survivors = [s for i, s in enumerate(first) if i % 2 == 1]
        frag, whole = [], []
        for i in range(n + 1, n + 1 + n // 2):
            data = make_photo(7000 + i, taken=_when(i), width=2000, height=1500, style=PHOTO_STYLE)
            name = "IMG_%04d.JPG" % i
            cl = card.write_file("DCIM/100CANON/" + name, data, _when(i))
            pieces = 1 + sum(1 for a, b in zip(cl, cl[1:]) if b != a + 1)
            (frag if pieces > 1 else whole).append((name, data))
        card.format()
    truth = {_sha(d): name for name, d in survivors + frag + whole}
    sc = Scenario("fragmented", "Heavily fragmented, then formatted", img, truth, fs=fs)
    sc.groups["fragmented"] = {_sha(d) for _, d in frag}
    sc.groups["in one piece"] = {_sha(d) for _, d in survivors + whole}
    return sc


ALL = [deleted, formatted, formatted_then_used, fragmented]
