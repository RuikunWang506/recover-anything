"""Generate realistic test media: camera-style JPEGs (with EXIF and an embedded
thumbnail) and structurally valid MP4 videos. Photos need Pillow; videos don't."""
from __future__ import annotations

import datetime as _dt
import io
import random
import struct


# ----------------------------------------------------------------------- EXIF
def _ifd(entries: list[tuple[int, int, bytes, int]], base: int, next_ifd: int) -> tuple[bytes, bytes]:
    """Serialize one little-endian IFD. entries: (tag, type, value_bytes, count).
    Returns (ifd_bytes, data_bytes); data lives right after the IFD at `base`."""
    entries = sorted(entries)
    ifd_len = 2 + 12 * len(entries) + 4
    data = bytearray()
    out = bytearray(struct.pack("<H", len(entries)))
    for tag, typ, val, count in entries:
        if len(val) <= 4:
            out += struct.pack("<HHI", tag, typ, count) + val.ljust(4, b"\x00")
        else:
            off = base + ifd_len + len(data)
            out += struct.pack("<HHII", tag, typ, count, off)
            data += val
            if len(data) % 2:
                data += b"\x00"
    out += struct.pack("<I", next_ifd)
    return bytes(out), bytes(data)


def build_exif(taken: _dt.datetime, make: str, model: str, thumb: bytes | None) -> bytes:
    """Return the APP1 payload ('Exif\\0\\0' + TIFF) a camera would write."""
    asc = lambda s: s.encode("ascii") + b"\x00"  # noqa: E731
    stamp = asc(taken.strftime("%Y:%m:%d %H:%M:%S"))
    # Two-pass layout: compute sizes with placeholder offsets, then for real.
    def layout(exif_off: int, ifd1_off: int, thumb_off: int):
        ifd0, d0 = _ifd([
            (0x010F, 2, asc(make), len(asc(make))),
            (0x0110, 2, asc(model), len(asc(model))),
            (0x0132, 2, stamp, len(stamp)),
            (0x8769, 4, struct.pack("<I", exif_off), 1),
        ], 8, ifd1_off if thumb else 0)
        exif, de = _ifd([
            (0x9003, 2, stamp, len(stamp)),
            (0x9004, 2, stamp, len(stamp)),
        ], exif_off, 0)
        ifd1, d1 = (b"", b"")
        if thumb:
            ifd1, d1 = _ifd([
                (0x0103, 3, struct.pack("<H", 6), 1),
                (0x0201, 4, struct.pack("<I", thumb_off), 1),
                (0x0202, 4, struct.pack("<I", len(thumb)), 1),
            ], ifd1_off, 0)
        return ifd0 + d0, exif + de, ifd1 + d1
    a, b, c = layout(0, 0, 0)
    exif_off = 8 + len(a)
    ifd1_off = exif_off + len(b)
    thumb_off = ifd1_off + len(c)
    a, b, c = layout(exif_off, ifd1_off, thumb_off)
    tiff = b"II*\x00" + struct.pack("<I", 8) + a + b + c + (thumb or b"")
    return b"Exif\x00\x00" + tiff


def _strip_app0(jpeg: bytes) -> bytes:
    if jpeg[2:4] == b"\xff\xe0":
        seglen = struct.unpack(">H", jpeg[4:6])[0]
        return jpeg[:2] + jpeg[4 + seglen:]
    return jpeg


# --------------------------------------------------------------------- photos
def make_photo(
    seed: int,
    width: int = 1600,
    height: int = 1200,
    taken: _dt.datetime | None = None,
    quality: int = 90,
    make: str = "Canon",
    model: str = "Canon EOS R6",
    restart_blocks: int = 0,
    optimize: bool = False,
    thumbnail: bool = True,
    noise: float = 9.0,
    style: str = "abstract",
) -> bytes:
    """A camera-like JPEG: smooth scene + sensor noise + edges, EXIF date, thumbnail."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    rng = random.Random(seed)
    taken = taken or _dt.datetime(2026, 7, 4, 10, 0, 0) + _dt.timedelta(minutes=seed * 7)
    if style == "landscape":
        im = _landscape(rng, width, height)
        return _finish(im, rng, taken, quality, make, model, restart_blocks, optimize, thumbnail, noise)
    small = Image.new("RGB", (8, 6))
    small.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(48)])
    im = small.resize((width, height), Image.BICUBIC)
    draw = ImageDraw.Draw(im)
    for _ in range(rng.randint(6, 14)):
        x0, y0 = rng.randrange(width), rng.randrange(height)
        x1, y1 = x0 + rng.randrange(40, width // 2), y0 + rng.randrange(40, height // 2)
        col = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        if rng.random() < 0.5:
            draw.ellipse((x0, y0, x1, y1), fill=col)
        else:
            draw.rectangle((x0, y0, x1, y1), fill=col)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    return _finish(im, rng, taken, quality, make, model, restart_blocks, optimize, thumbnail, noise)


def _finish(im, rng, taken, quality, make, model, restart_blocks, optimize, thumbnail, noise):
    from PIL import Image, ImageChops
    width, height = im.size
    if noise:
        # Seeded sensor noise (Pillow's effect_noise ignores Python's seed, which
        # would make every simulated card, and so every result, different).
        raw = Image.frombytes("L", (width, height), rng.randbytes(width * height))
        scale = noise / 73.9  # std of uniform 0..255 is ~73.9
        n = raw.point([max(0, min(255, int(round(128 + (v - 127.5) * scale)))) for v in range(256)]).convert("RGB")
        im = ImageChops.add(im, n, 1.0, -128)
    thumb = None
    if thumbnail:
        t = im.copy()
        t.thumbnail((160, 120))
        tb = io.BytesIO()
        t.save(tb, "JPEG", quality=75)
        thumb = _strip_app0(tb.getvalue())
    exif = build_exif(taken, make, model, thumb)
    out = io.BytesIO()
    kw = dict(quality=quality, exif=exif, optimize=optimize, subsampling=2)
    if restart_blocks:
        kw["restart_marker_blocks"] = restart_blocks
    im.save(out, "JPEG", **kw)
    return _strip_app0(out.getvalue())


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _landscape(rng, w, h):
    """A plausible outdoor photo: sky, sun, clouds, mountain ridges, lake or field, trees."""
    from PIL import Image, ImageDraw, ImageFilter
    sunset = rng.random() < 0.35
    top = (rng.randint(30, 70), rng.randint(40, 80), rng.randint(110, 150)) if sunset else \
        (rng.randint(30, 80), rng.randint(90, 150), rng.randint(180, 240))
    hor = (rng.randint(235, 255), rng.randint(140, 190), rng.randint(90, 130)) if sunset else \
        (rng.randint(190, 230), rng.randint(215, 240), rng.randint(235, 255))
    horizon = int(h * rng.uniform(0.48, 0.64))
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(horizon + 1):
        d.line([(0, y), (w, y)], fill=_lerp(top, hor, y / max(1, horizon)))
    sx, sy, sr = rng.randint(w // 6, 5 * w // 6), int(horizon * rng.uniform(0.35, 0.8)), rng.randint(w // 40, w // 18)
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse((sx - sr * 4, sy - sr * 4, sx + sr * 4, sy + sr * 4), fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(sr * 2))
    im.paste((255, 236, 200), (0, 0), glow)
    d = ImageDraw.Draw(im)
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=(255, 246, 225))
    clouds = Image.new("L", (w, h), 0)
    cd = ImageDraw.Draw(clouds)
    for _ in range(rng.randint(2, 6)):
        cx, cy = rng.randint(0, w), rng.randint(0, int(horizon * 0.6))
        for _k in range(rng.randint(3, 6)):
            rx, ry = rng.randint(w // 30, w // 10), rng.randint(h // 60, h // 22)
            ox, oy = rng.randint(-w // 12, w // 12), rng.randint(-h // 50, h // 50)
            cd.ellipse((cx + ox - rx, cy + oy - ry, cx + ox + rx, cy + oy + ry), fill=170)
    clouds = clouds.filter(ImageFilter.GaussianBlur(w // 120 + 2))
    im.paste((255, 250, 245) if not sunset else (250, 190, 170), (0, 0), clouds)
    d = ImageDraw.Draw(im)
    base = (rng.randint(40, 90), rng.randint(60, 100), rng.randint(90, 130))
    layers = rng.randint(2, 4)
    for li in range(layers):
        t = (li + 1) / (layers + 1)
        col = _lerp(_lerp(base, hor, 0.65), (25, 35, 45), t)
        y = horizon - int(h * rng.uniform(0.12, 0.3) * (1 - t * 0.6))
        pts = [(0, h)]
        x = 0
        while x <= w:
            pts.append((x, y))
            x += rng.randint(w // 40, w // 12)
            y = min(horizon + 4, max(int(h * 0.12), y + rng.randint(-h // 14, h // 14)))
        pts += [(w, y), (w, h)]
        d.polygon(pts, fill=col)
    if rng.random() < 0.5:  # lake reflecting the sky
        sky = im.crop((0, max(0, 2 * horizon - h), w, horizon)).transpose(Image.FLIP_TOP_BOTTOM)
        sky = sky.resize((w, h - horizon)).filter(ImageFilter.GaussianBlur(3))
        dark = Image.new("RGB", sky.size, (20, 40, 60))
        im.paste(Image.blend(sky, dark, 0.35), (0, horizon))
        d = ImageDraw.Draw(im)
        for _ in range(60):
            y = rng.randint(horizon + 2, h - 1)
            x = rng.randint(0, w)
            d.line([(x, y), (x + rng.randint(10, w // 8), y)], fill=_lerp(hor, (255, 255, 255), 0.3), width=1)
    else:
        g1 = (rng.randint(60, 110), rng.randint(110, 150), rng.randint(40, 70))
        g2 = (rng.randint(30, 60), rng.randint(60, 90), rng.randint(20, 40))
        for y in range(horizon, h):
            d.line([(0, y), (w, y)], fill=_lerp(g1, g2, (y - horizon) / max(1, h - horizon)))
    for _ in range(rng.randint(4, 14)):
        tx = rng.randint(0, w)
        th = rng.randint(h // 12, h // 4)
        ty = horizon + rng.randint(0, (h - horizon) // 2)
        tw = th // 3
        d.polygon([(tx, ty - th), (tx - tw, ty), (tx + tw, ty)], fill=(rng.randint(15, 35), rng.randint(40, 70),
                                                                    rng.randint(25, 45)))
    return im.filter(ImageFilter.GaussianBlur(1.0))


# --------------------------------------------------------------------- videos
def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _full(typ: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(typ, struct.pack(">I", (version << 24) | flags) + payload)


def make_video(seed: int, frames: int = 120, avg_frame: int = 12000, taken: _dt.datetime | None = None,
               moov_first: bool = False) -> bytes:
    """A structurally valid H.264 MP4 (length-prefixed NAL samples, full sample
    tables). Not watchable, but every byte a validator checks is real."""
    rng = random.Random(seed)
    taken = taken or _dt.datetime(2026, 7, 4, 10, 0, 0) + _dt.timedelta(minutes=seed * 7)
    epoch = int((taken - _dt.datetime(1904, 1, 1)).total_seconds())
    samples = []
    for i in range(frames):
        n = max(200, int(rng.gauss(avg_frame * (4 if i % 30 == 0 else 1), avg_frame * 0.2)))
        nal_type = 5 if i % 30 == 0 else 1
        nal = bytes([0x60 | nal_type if nal_type == 5 else 0x40 | nal_type]) + rng.randbytes(n - 1)
        samples.append(struct.pack(">I", len(nal)) + nal)
    sps = bytes([0x67, 0x64, 0x00, 0x28, 0xAC, 0xD9, 0x40, 0x78, 0x02, 0x27, 0xE5, 0x84])
    pps = bytes([0x68, 0xEB, 0xE3, 0xCB, 0x22, 0xC0])
    avcc = _box(b"avcC", bytes([1, 0x64, 0x00, 0x28, 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps
                + b"\x01" + struct.pack(">H", len(pps)) + pps)
    avc1 = _box(b"avc1", bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HH", 1920, 1080)
                + struct.pack(">II", 0x00480000, 0x00480000) + bytes(4) + struct.pack(">H", 1)
                + bytes(32) + struct.pack(">Hh", 0x18, -1) + avcc)

    def build(mdat_data_offset: int) -> bytes:
        offsets, pos = [], mdat_data_offset
        for s in samples:
            offsets.append(pos)
            pos += len(s)
        stsd = _full(b"stsd", 0, 0, struct.pack(">I", 1) + avc1)
        stts = _full(b"stts", 0, 0, struct.pack(">III", 1, frames, 1001))
        stss = _full(b"stss", 0, 0, struct.pack(">I", (frames + 29) // 30)
                     + b"".join(struct.pack(">I", i + 1) for i in range(0, frames, 30)))
        stsc = _full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
        stsz = _full(b"stsz", 0, 0, struct.pack(">II", 0, frames) + b"".join(struct.pack(">I", len(s)) for s in samples))
        stco = _full(b"stco", 0, 0, struct.pack(">I", frames) + b"".join(struct.pack(">I", o) for o in offsets))
        stbl = _box(b"stbl", stsd + stts + stss + stsc + stsz + stco)
        dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1) + _full(b"url ", 0, 1, b"")))
        minf = _box(b"minf", _full(b"vmhd", 0, 1, bytes(8)) + dinf + stbl)
        hdlr = _full(b"hdlr", 0, 0, bytes(4) + b"vide" + bytes(12) + b"VideoHandler\x00")
        mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", epoch, epoch, 30000, frames * 1001, 0x55C4, 0))
        mdia = _box(b"mdia", mdhd + hdlr + minf)
        tkhd = _full(b"tkhd", 0, 3, struct.pack(">IIIII", epoch, epoch, 1, 0, frames * 1001 // 30)
                     + bytes(8) + bytes(8) + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
                     + struct.pack(">II", 1920 << 16, 1080 << 16))
        trak = _box(b"trak", tkhd + mdia)
        mvhd = _full(b"mvhd", 0, 0, struct.pack(">IIIII", epoch, epoch, 30000, frames * 1001, 0x10000)
                     + struct.pack(">H", 0x100) + bytes(10)
                     + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + bytes(24)
                     + struct.pack(">I", 2))
        return _box(b"moov", mvhd + trak)

    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2avc1mp41")
    payload = b"".join(samples)
    mdat_header = struct.pack(">I", 8 + len(payload)) + b"mdat"
    if moov_first:
        moov = build(0)
        moov = build(len(ftyp) + len(moov) + 8)
        return ftyp + moov + mdat_header + payload
    moov = build(len(ftyp) + 8)
    return ftyp + mdat_header + payload + moov
