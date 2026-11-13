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


def stress(workdir: str, seed: int, cluster_size: int = 32768, mixed: bool = True) -> Scenario:
    """A randomized card with three rounds of shooting and deleting (so photos
    and videos end up split across the holes), formatted in-camera (folders
    recreated, so no names survive). Ground truth: every file present at the
    format whose clusters weren't reused by the new folders."""
    import random as _random
    rng = _random.Random(seed)
    img = os.path.join(workdir, "stress-%d-%d.img" % (seed, cluster_size))
    present: dict = {}
    counter = [0]

    def shoot(card, k):
        for _ in range(k):
            counter[0] += 1
            n = counter[0]
            if rng.random() < 0.08:
                data = make_video(seed * 1000 + n, frames=rng.randint(60, 200))
                name = "MVI_%04d.MP4" % n
            else:
                w = rng.choice([1200, 1600, 2000, 2400])
                kw = rng.choice([{}, {"restart_blocks": 8}, {"optimize": True}, {"quality": 95}]) if mixed else {}
                data = make_photo(seed * 1000 + n, width=w, height=w * 3 // 4, style=PHOTO_STYLE, **kw)
                name = "IMG_%04d.JPG" % n
            cl = card.write_file("DCIM/100CANON/" + name, data, _when(n))
            present[name] = (data, set(cl))

    def delete(card, k):
        for name in rng.sample(sorted(present), k):
            card.delete("DCIM/100CANON/" + name)
            del present[name]

    with Fat32Card(img, size=4 * 1024**3, cluster_size=cluster_size) as card:
        shoot(card, 40)
        delete(card, 15)
        shoot(card, 20)
        delete(card, 12)
        shoot(card, 15)
        card.format(recreate_dirs=["DCIM", "DCIM/100CANON"])
        used = card.allocated_clusters()
    truth = {_sha(d): nm for nm, (d, cl) in present.items() if not (cl & used)}
    frag = {_sha(d) for nm, (d, cl) in present.items()
            if len(cl) > 1 and max(cl) - min(cl) + 1 != len(cl) and _sha(d) in truth}
    sc = Scenario("stress", "Randomized, 3 rounds of shoot/delete, camera-formatted", img, truth)
    sc.groups["fragmented photos"] = {h for h in frag if truth[h].endswith(".JPG")}
    sc.groups["videos"] = {h for h in truth if truth[h].endswith(".MP4")}
    sc.groups["fragmented videos"] = {h for h in frag if truth[h].endswith(".MP4")}
    return sc


def _device_file(rng, n: int):
    """One random file of the kinds a phone, recorder, dashcam or laptop leaves on a card or stick."""
    from . import files as F
    k = rng.choice(["photo", "photo", "mp3", "mp3", "flac", "opus", "ts", "mts", "webm", "mkv", "sqlite", "gzip",
                    "bmp", "heic", "video"])
    s = 10_000 + n
    if k == "photo":
        w = rng.choice([1200, 1600])
        return "DCIM/IMG_%04d.JPG" % n, make_photo(s, width=w, height=w * 3 // 4, style=PHOTO_STYLE)
    if k == "video":
        return "DCIM/MVI_%04d.MP4" % n, make_video(s, frames=rng.randint(60, 160))
    if k == "mp3":
        return "Music/track%03d.mp3" % n, F.make_mp3(s, seconds=rng.uniform(8, 40), xing=rng.random() < 0.6,
                                                     id3v1=rng.random() < 0.3, id3=rng.random() < 0.8)
    if k == "flac":
        return "Music/track%03d.flac" % n, F.make_flac(s, seconds=rng.uniform(2, 8))
    if k == "opus":
        return "Recordings/memo%03d.opus" % n, F.make_ogg(s, seconds=rng.uniform(5, 60))
    if k == "ts":
        return "DashCam/REC%04d.TS" % n, F.make_ts(s, packets=rng.randint(1500, 6000))
    if k == "mts":
        return "AVCHD/BDMV/STREAM/%05d.MTS" % n, F.make_ts(s, packets=rng.randint(1500, 6000), m2ts=True)
    if k in ("webm", "mkv"):
        return "Videos/clip%03d.%s" % (n, k), F.make_mkv(s, clusters=rng.randint(3, 10), webm=k == "webm")
    if k == "sqlite":
        return "Backup/db%03d.sqlite" % n, F.make_sqlite(s, rows=rng.randint(100, 1500))
    if k == "gzip":
        return "Backup/log%03d.gz" % n, F.make_gzip(s, lines=rng.randint(3000, 30000))
    if k == "bmp":
        return "Pictures/scan%03d.bmp" % n, F.make_bmp(s, width=rng.choice([200, 320, 480]), height=240)
    return "DCIM/IMG_%04d.HEIC" % n, F.make_heic(s, size=rng.randint(300_000, 1_500_000))


def mixed_device(workdir: str, seed: int, cluster_size: int = 4096, size: int = 512 * 1024**2,
                 fs: str = "FAT32") -> Scenario:
    """A card or stick used by several devices: photos, music, voice memos,
    dashcam and camcorder clips, screen recordings, phone databases, logs and
    scans. Three rounds of saving and deleting (so files fragment), then a
    format that recreates the folders, so no names survive. Ground truth:
    every file present at the format whose clusters weren't reused."""
    import random as _random
    rng = _random.Random("mixed-%d" % seed)
    img = os.path.join(workdir, "mixed-%d-%s.img" % (seed, fs.lower()))
    present: dict = {}
    counter = [0]
    ever: set = set()

    def save(card, k):
        for _ in range(k):
            counter[0] += 1
            path, data = _device_file(rng, counter[0])
            cl = card.write_file(path, data, _when(counter[0]))
            present[path] = (data, set(cl))
            ever.add(_sha(data))

    def delete(card, k):
        for path in rng.sample(sorted(present), min(k, len(present))):
            card.delete(path)
            del present[path]

    with _card(img, fs, size=size, cluster_size=cluster_size) as card:
        save(card, 30)
        delete(card, 12)
        save(card, 16)
        delete(card, 9)
        save(card, 10)
        card.format(recreate_dirs=["DCIM", "Music"])
        used = card.allocated_clusters()
    truth = {_sha(d): p for p, (d, cl) in present.items() if not (cl & used)}
    frag = {_sha(d) for p, (d, cl) in present.items()
            if len(cl) > 1 and max(cl) - min(cl) + 1 != len(cl) and _sha(d) in truth}
    sc = Scenario("mixed", "Shared by many devices, 3 rounds of save/delete, formatted", img, truth, fs=fs)
    sc.groups["fragmented"] = frag
    sc.groups["in one piece"] = set(truth) - frag
    sc.groups["ever written"] = ever  # a file deleted earlier that comes back exact is a correct find too
    return sc


SINGLE_KINDS = ("mp3", "ts", "mts", "flac", "opus", "mp4", "jpg", "amr", "mpg", "doc",
                "pdf", "docx", "wav", "avi", "raw", "gif", "heic", "webm", "mkv")


def _one_kind_file(kind: str, rng, n: int):
    from . import files as F
    s = 20_000 + n
    if kind == "mp3":
        return "MUSIC/%03d.mp3" % n, F.make_mp3(s, seconds=rng.uniform(10, 40), xing=rng.random() < 0.6,
                                                id3=rng.random() < 0.8, id3v1=rng.random() < 0.3)
    if kind == "ts":
        return "DCIM/REC%04d.TS" % n, F.make_ts(s, packets=rng.randint(2000, 7000))
    if kind == "mts":
        return "AVCHD/BDMV/STREAM/%05d.MTS" % n, F.make_ts(s, packets=rng.randint(2000, 7000), m2ts=True)
    if kind == "flac":
        return "MUSIC/%03d.flac" % n, F.make_flac(s, seconds=rng.uniform(2, 8))
    if kind == "opus":
        return "VOICE/%03d.opus" % n, F.make_ogg(s, seconds=rng.uniform(10, 60))
    if kind == "mp4":
        return "DCIM/MVI_%04d.MP4" % n, make_video(s, frames=rng.randint(60, 160))
    if kind == "jpg":
        w = rng.choice([1200, 1600])
        return "DCIM/IMG_%04d.JPG" % n, make_photo(s, width=w, height=w * 3 // 4, style=PHOTO_STYLE)
    if kind == "amr":
        return "VOICE/%03d.amr" % n, F.make_amr(s, seconds=rng.uniform(10, 60))
    if kind == "mpg":
        return "MP_ROOT/MOV%03d.MOD" % n, F.make_ps(s, packs=rng.randint(200, 700))
    if kind == "doc":
        return "Documents/report%03d.doc" % n, F.make_ole(s, size=rng.randint(8000, 120000))
    if kind == "pdf":
        return "Documents/report%03d.pdf" % n, F.make_pdf(s, pages=rng.randint(2, 30))
    if kind == "docx":
        return "Documents/letter%03d.docx" % n, F.make_docx(s, paragraphs=rng.randint(20, 400))
    if kind == "wav":
        return "RECORD/REC%04d.WAV" % n, F.make_wav(s, seconds=rng.uniform(10, 120))
    if kind == "avi":
        return "DCIM/100MEDIA/IMAG%04d.AVI" % n, F.make_avi(s, frames=rng.randint(20, 80))
    if kind == "raw":
        return "DCIM/100CANON/IMG_%04d.CR2" % n, F.make_raw(s, raw_size=rng.randint(1_000_000, 3_000_000))
    if kind == "gif":
        return "Pictures/anim%03d.gif" % n, F.make_gif(s, frames=rng.randint(1, 8))
    if kind == "heic":
        return "DCIM/100APPLE/IMG_%04d.HEIC" % n, F.make_heic(s, size=rng.randint(800_000, 2_500_000))
    if kind == "webm":
        return "Movies/screen%03d.webm" % n, F.make_mkv(s, clusters=rng.randint(3, 10), webm=True)
    if kind == "mkv":
        return "Videos/clip%03d.mkv" % n, F.make_mkv(s, clusters=rng.randint(3, 10))
    raise ValueError(kind)


def single_kind(workdir: str, kind: str, seed: int, cluster_size: int = 4096, size: int = 384 * 1024**2,
                fs: str = "FAT32") -> Scenario:
    """One device, one kind of file: a music stick, a dashcam, a camcorder, a
    voice recorder. Files of the same kind sit side by side and fragment into
    each other's holes, which is the hardest case for telling them apart.
    Three rounds of saving and deleting, then a format that recreates the
    folders. Ground truth as in mixed_device."""
    import random as _random
    rng = _random.Random("one-%s-%d" % (kind, seed))
    img = os.path.join(workdir, "one-%s-%d-%s.img" % (kind, seed, fs.lower()))
    present: dict = {}
    ever: set = set()
    counter = [0]

    def save(card, k):
        for _ in range(k):
            counter[0] += 1
            path, data = _one_kind_file(kind, rng, counter[0])
            cl = card.write_file(path, data, _when(counter[0]))
            present[path] = (data, set(cl))
            ever.add(_sha(data))

    def delete(card, k):
        for path in rng.sample(sorted(present), min(k, len(present))):
            card.delete(path)
            del present[path]

    with _card(img, fs, size=size, cluster_size=cluster_size) as card:
        save(card, 24)
        delete(card, 10)
        save(card, 14)
        delete(card, 8)
        save(card, 8)
        top = sorted({p.split("/")[0] for p in present})
        card.format(recreate_dirs=top)
        used = card.allocated_clusters()
    truth = {_sha(d): p for p, (d, cl) in present.items() if not (cl & used)}
    frag = {_sha(d) for p, (d, cl) in present.items()
            if len(cl) > 1 and max(cl) - min(cl) + 1 != len(cl) and _sha(d) in truth}
    sc = Scenario("one-" + kind, "Only %s files, 3 rounds of save/delete, formatted" % kind, img, truth, fs=fs)
    sc.groups["fragmented"] = frag
    sc.groups["ever written"] = ever
    return sc


# The scenarios below test logic, not scale: 96 MB cards. Windows writes a card
# image out in full, and CI's Windows machine has little disk to spare. FAT32
# needs at least 65,525 clusters, so 1 KB ones; the simulated exFAT card keeps
# each folder in one cluster, so 4 KB ones (about 128 entries).
def _small_clusters(fs: str) -> int:
    return 4096 if fs == "exFAT" else 1024


def _win_trash_record(original: str, size: int, version: int = 2) -> bytes:
    """A Windows Recycle Bin $I record: version, size, deletion time, path."""
    import struct
    when = 133650000000000000                    # a FILETIME in 2024
    path = original.encode("utf-16-le") + b"\x00\x00"
    if version == 1:
        return struct.pack("<QQQ", 1, size, when) + path.ljust(520, b"\x00")
    return struct.pack("<QQQI", 2, size, when, len(path) // 2) + path


def trashed(workdir: str, fs: str = "FAT32", n: int = 24, size: int = 96 * 1024**2,
            cluster_size: int = 0) -> Scenario:
    """Photos deleted from the card on a computer: moved (not copied) into the
    trash folder on the card itself, the way macOS, Linux and Windows do it,
    then some of each trash emptied. Every one of them should come back, under
    its original name; the photos still in DCIM are not the user's loss."""
    img = os.path.join(workdir, "trashed-%s.img" % fs.lower())
    folder = "DCIM/100CANON"
    sid = "$RECYCLE.BIN/S-1-5-21-3623811015-3361044348-30300820-1001"
    with _card(img, fs, size=size, cluster_size=cluster_size or _small_clusters(fs)) as card:
        shots = _shoot(card, 1, n, folder=folder)
        names = [nm for nm, _ in shots]

        def move(name, to):
            card.move("%s/%s" % (folder, name), to)
        # macOS (Finder): /.Trashes/501/, same name
        for name in names[0:5]:
            move(name, ".Trashes/501/" + name)
        card.write_file(".Trashes/501/.DS_Store", b"\x00\x00\x00\x01Bud1" + bytes(500), _when(1))
        # Linux file manager: /.Trash-1000/files/ + info/NAME.trashinfo; one renamed on a clash
        for k, name in enumerate(names[5:10]):
            tname = name.replace(".JPG", ".2.JPG") if k == 4 else name
            move(name, ".Trash-1000/files/" + tname)
            card.write_file(".Trash-1000/info/%s.trashinfo" % tname,
                            ("[Trash Info]\nPath=/media/me/CARD/%s/%s\nDeletionDate=2026-07-06T10:00:00\n"
                             % (folder, name)).encode(), _when(1))
        # Windows: $RECYCLE.BIN/<SID>/$R<id>.JPG + $I<id>.JPG with the original path
        for k, name in enumerate(names[10:15]):
            ident = "%06X" % (0xA1B2C0 + k)
            data = dict(shots)[name]
            move(name, "%s/$R%s.JPG" % (sid, ident))
            card.write_file("%s/$I%s.JPG" % (sid, ident),
                            _win_trash_record("E:\\DCIM\\100CANON\\" + name, len(data), version=1 if k == 0 else 2),
                            _when(1))
        # then each trash is partly emptied: those are ordinary deleted files now
        card.delete(".Trashes/501/" + names[0])
        card.delete(".Trash-1000/files/" + names[5])
        card.delete(".Trash-1000/info/%s.trashinfo" % names[5])
        card.delete("%s/$R%06X.JPG" % (sid, 0xA1B2C0 + 1))
        card.delete("%s/$I%06X.JPG" % (sid, 0xA1B2C0 + 1))
    truth = {_sha(d): nm for nm, d in shots[:15]}
    return Scenario("trashed", "Deleted on a computer (moved to the trash on the card)", img, truth, fs=fs)


def make_appledouble(name: str) -> bytes:
    """The hidden ._NAME a Mac writes next to each file it copies onto FAT or
    exFAT: an AppleDouble header and Finder metadata, 4096 bytes."""
    import struct
    head = struct.pack(">II16sH", 0x00051607, 0x00020000, b"Mac OS X        ", 2)
    head += struct.pack(">III", 9, 50, 3698) + struct.pack(">III", 2, 3748, 286)
    body = bytearray(4096)
    body[:len(head)] = head
    body[50:54] = b"ATTR"
    q = b"0083;6728d1a0;Image Capture;" + name.encode()
    body[120:120 + len(q)] = q
    return bytes(body)


def mac_copied(workdir: str, fs: str = "FAT32", n: int = 12, size: int = 96 * 1024**2,
               cluster_size: int = 0) -> Scenario:
    """Photos a Mac copied onto the card (each with its hidden ._ companion),
    with the system's own files a Mac and Windows leave on a card, then all
    the photos deleted together with their companions. The companions and the
    system files are not the owner's: they must not be saved, counted, or
    reported as lost."""
    img = os.path.join(workdir, "mac-copied-%s.img" % fs.lower())
    folder = "DCIM/100CANON"
    with _card(img, fs, size=size, cluster_size=cluster_size or _small_clusters(fs)) as card:
        card.write_file(".fseventsd/fseventsd-uuid", b"5F2D8C3A-0B6E-4F4C-9A61-3B1E2D7C9A10", _when(0))
        card.write_file(".Spotlight-V100/VolumeConfiguration.plist", b"<?xml version=\"1.0\"?><plist/>", _when(0))
        card.write_file("System Volume Information/IndexerVolumeGuid",
                        "{2B8A6E44-1C1F-4D21-9D6E-7A1D4C3E5B10}".encode("utf-16-le"), _when(0))
        shots = []
        for i in range(1, n + 1):
            name = "IMG_%04d.JPG" % i
            data = make_photo(i, taken=_when(i), style=PHOTO_STYLE)
            card.write_file("%s/%s" % (folder, name), data, _when(i))
            card.write_file("%s/._%s" % (folder, name), make_appledouble(name), _when(i))
            shots.append((name, data))
        card.write_file(folder + "/.DS_Store", b"\x00\x00\x00\x01Bud1" + bytes(6138), _when(n))
        for name, _ in shots:
            card.delete("%s/%s" % (folder, name))
            card.delete("%s/._%s" % (folder, name))
        card.delete(folder + "/.DS_Store")
    truth = {_sha(d): nm for nm, d in shots}
    return Scenario("mac-copied", "Copied on a Mac, then deleted", img, truth, fs=fs)


def android_trashed(workdir: str, fs: str = "exFAT", n: int = 16, size: int = 96 * 1024**2,
                    cluster_size: int = 0) -> Scenario:
    """A phone's SD card: photos deleted in the Gallery or Google Photos are
    renamed in place to .trashed-<expiry>-NAME and kept for 30 days; after
    that the phone deletes them for good. Both kinds should come back under
    their original names."""
    img = os.path.join(workdir, "android-trashed-%s.img" % fs.lower())
    folder = "DCIM/Camera"
    with _card(img, fs, size=size, cluster_size=cluster_size or _small_clusters(fs)) as card:
        shots = []
        for i in range(1, n + 1):
            t = _when(i)
            name = "IMG_%s.jpg" % t.strftime("%Y%m%d_%H%M%S")
            data = make_photo(i, taken=t, style=PHOTO_STYLE)
            card.write_file("%s/%s" % (folder, name), data, t)
            shots.append((name, data))
        expiry = 1786000000                       # 30 days after deletion, in seconds since 1970
        for k, (name, _) in enumerate(shots[:8]):
            card.move("%s/%s" % (folder, name), "%s/.trashed-%d-%s" % (folder, expiry + k, name))
        for k, (name, _) in enumerate(shots[:3]):  # 30 days later: gone for good
            card.delete("%s/.trashed-%d-%s" % (folder, expiry + k, name))
    truth = {_sha(d): nm for nm, d in shots[:8]}
    return Scenario("android-trashed", "Deleted in a phone's gallery (Android trash)", img, truth, fs=fs)


def unopenable(workdir: str, fs: str = "FAT32", damage: str = "boot", n: int = 10, size: int = 96 * 1024**2,
               cluster_size: int = 0) -> Scenario:
    """'This card needs to be formatted': nothing was deleted, but the start of
    the card is damaged, so no computer can open it. damage='boot' wipes the
    main boot sector (the backup copy survives), damage='mbr' wipes the
    partition table. Every photo is still listed and whole."""
    import struct
    img = os.path.join(workdir, "unopenable-%s-%s.img" % (damage, fs.lower()))
    with _card(img, fs, size=size, cluster_size=cluster_size or _small_clusters(fs)) as card:
        shots = _shoot(card, 1, n)
    if damage in ("boot", "mbr"):
        with open(img, "r+b") as f:
            mbr = f.read(512)
            lba = struct.unpack_from("<I", mbr, 446 + 8)[0] if mbr[510:512] == b"\x55\xaa" else 0
            f.seek(lba * 512 if damage == "boot" else 0)
            f.write(bytes(512))
    truth = {_sha(d): name for name, d in shots}
    return Scenario("unopenable-" + damage, "Card that needs formatting (%s damaged)" % damage, img, truth, fs=fs)


def ntfs_drive(workdir: str, cluster_size: int = 4096, size: int = 64 * 1024**2) -> Scenario:
    """An external NTFS drive used on Windows: photos and documents in
    folders, some deleted for good (Shift+Delete), some sent to the Recycle
    Bin and the bin then partly emptied, one deleted photo stored in two
    pieces, one whose middle a new file later overwrote, and one whose record
    and space a new file took over. The MFT is in three pieces.
    truth: what must come back byte for byte; groups["folders"]: sha -> folder;
    groups["partial"]: the overwritten photo, which must never count as recovered."""
    from .ntfs import NtfsCard
    img = os.path.join(workdir, "ntfs-%d.img" % cluster_size)
    bin_ = "$RECYCLE.BIN/S-1-5-21-3623811015-3361044348-30300820-1001"
    truth, folders = {}, {}

    def keep(path, data):
        folder, _, name = path.rpartition("/")
        truth[_sha(data)] = name
        folders[_sha(data)] = folder

    with NtfsCard(img, size=size, cluster_size=cluster_size, mft_pieces=3) as v:
        photos = {}
        for i in range(1, 11):
            folder = "Photos/2026 Lisbon" if i <= 5 else "Photos/Family"
            path = "%s/IMG_%04d.JPG" % (folder, i)
            data = make_photo(i, taken=_when(i), style=PHOTO_STYLE)
            v.write_file(path, data, _when(i))
            photos[i] = (path, data)
        notes = ("Chapter 3: results\n" * 3000).encode()
        v.write_file("Documents/Thesis/chapter3 notes.txt", notes, _when(12))
        v.write_file("Documents/Thesis/outline.txt", b"1. intro\n2. methods\n", _when(12))   # small: kept in its record
        v.write_file("Documents/scratch.tmp", b"y" * 40000, _when(13))
        # a hole that the next photo fills before continuing elsewhere: two pieces
        v.delete("Documents/scratch.tmp")
        frag = make_photo(11, taken=_when(11), style=PHOTO_STYLE)
        v.write_file("Photos/Family/IMG_0011.JPG", frag, _when(11))
        # photo 10: deleted, then a new file takes its record and its space. NTFS hands a
        # new file the lowest free record, so everything that creates records comes
        # before the deletions below, or their names would be taken over too.
        v.delete(photos[10][0])
        v.write_file("Photos/Family/new video.part", bytes(len(photos[10][1])), _when(20))
        # the Recycle Bin: the same record, renamed, plus a $I record
        for k, i in enumerate((3, 7, 8)):
            ident = "%06X" % (0xC0FFE0 + k)
            path, data = photos[i]
            v.move(path, "%s/$R%s.JPG" % (bin_, ident))
            v.write_file("%s/$I%s.JPG" % (bin_, ident),
                         _win_trash_record("E:\\" + path.replace("/", "\\"), len(data)), _when(14))
            keep(path, data)
        # the bin partly emptied: those records are deleted, still readable
        v.delete("%s/$R%06X.JPG" % (bin_, 0xC0FFE0))
        v.delete("%s/$I%06X.JPG" % (bin_, 0xC0FFE0))
        # Shift+Delete
        for i in (1, 2, 6):
            v.delete(photos[i][0])
            keep(*photos[i])
        v.delete("Photos/Family/IMG_0011.JPG")
        keep("Photos/Family/IMG_0011.JPG", frag)
        v.delete("Documents/Thesis/chapter3 notes.txt")
        keep("Documents/Thesis/chapter3 notes.txt", notes)
        # photo 9: deleted, then a new file's data lands in its middle (no new record)
        v.delete(photos[9][0])
        runs = next(r["runs"] for r in v.records.values() if r.get("name") == "IMG_0009.JPG")
        lcn, n = runs[0]
        v._take(lcn + n // 2, 6)
        v.f.seek(v.cluster_offset(lcn + n // 2))
        v.f.write(bytes(range(256)) * (6 * cluster_size // 256))
    sc = Scenario("ntfs", "External NTFS drive", img, truth, fs="NTFS")
    sc.groups = {"folders": folders, "partial": {_sha(photos[9][1])}, "gone": {_sha(photos[10][1])}}
    return sc
