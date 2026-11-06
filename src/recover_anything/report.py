"""A plain-language report: what came back, what didn't, and why."""
from __future__ import annotations

import base64
import html
import io
import os

from .carve import group_of
from .exif import jpeg_exif

MAX_THUMBS = 400


def _thumb(path: str, kind: str, partial: bool) -> str | None:
    try:
        with open(path, "rb") as f:
            head = f.read(256 * 1024)
    except OSError:
        return None
    data = None
    if kind == "jpeg" and not partial:
        data = jpeg_exif(head)[1]
    if data is None:
        try:
            from PIL import Image, ImageFile
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with Image.open(path) as im:
                im.draft("RGB", (320, 240))
                im = im.convert("RGB")
                im.thumbnail((240, 180))
                b = io.BytesIO()
                im.save(b, "JPEG", quality=70)
                data = b.getvalue()
        except Exception:
            return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _human(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return str(n)


def summarize(res, written) -> dict:
    counts = {"photo": 0, "video": 0, "audio": 0, "document": 0, "other": 0}
    partial = 0
    reassembled = 0
    named = 0
    unconfirmed = 0
    for r in written:
        if r.status in ("partial", "corrupt"):
            partial += 1
            continue
        if r.status == "unverified" and r.kind != "other":
            unconfirmed += 1
        counts[group_of(r.kind, r.ext)] += 1
        if r.meta.get("reassembled_from"):
            reassembled += 1
        if r.name:
            named += 1
    lost = [r for r in res.found if r.status == "lost" and r.name]
    return {"counts": counts, "partial": partial, "reassembled": reassembled, "named": named, "lost": lost,
            "total": sum(counts.values()), "unconfirmed": unconfirmed}


def headline(s: dict) -> str:
    c = s["counts"]
    parts = []
    for key, one, many in (("photo", "photo", "photos"), ("video", "video", "videos"),
                           ("audio", "audio file", "audio files"), ("document", "document", "documents"), ("other", "other file", "other files")):
        if c[key]:
            parts.append("%d %s" % (c[key], one if c[key] == 1 else many))
    if not parts:
        return "Nothing recoverable was found"
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1] + " recovered"
    return parts[0] + " recovered"


CSS = """
:root{--bg:#f7f5f0;--card:#fff;--ink:#1d1b16;--muted:#6b665c;--line:#e6e1d6;--ok:#1f7a4d;--okbg:#e3f3ea;
--warn:#8a5a00;--warnbg:#fbefd5;--bad:#9b2c2c;--badbg:#f8e1e1;--accent:#2d5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#15140f;--card:#1f1d17;--ink:#f1ede4;--muted:#a49d8f;--line:#34312a;
--ok:#6fd39e;--okbg:#173527;--warn:#f0c46a;--warnbg:#3a2d10;--bad:#f19a9a;--badbg:#3d1c1c;--accent:#8aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1080px;margin:0 auto;padding:40px 16px 80px}
.kicker{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-size:clamp(28px,5vw,44px);line-height:1.1;margin:6px 0 12px;letter-spacing:-.02em}
.lede{color:var(--muted);max-width:720px;margin:0 0 28px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 32px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.stat b{display:block;font-size:28px;letter-spacing:-.02em}.stat span{color:var(--muted);font-size:14px}
h2{font-size:20px;margin:36px 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
figure a{position:relative;display:block}.badge{position:absolute;top:8px;left:8px;box-shadow:0 1px 3px rgba(0,0,0,.25)}
figure img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;background:var(--line)}
.ph{display:flex;align-items:center;justify-content:center;aspect-ratio:4/3;background:var(--line);color:var(--muted);font-size:13px}
figcaption{padding:8px 10px;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:99px;margin-right:4px}
.chip{color:var(--muted);background:var(--line)}.ok{color:var(--ok);background:var(--okbg)}.warn{color:var(--warn);background:var(--warnbg)}.bad{color:var(--bad);background:var(--badbg)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.note{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:14px 16px;margin:12px 0}
ul.next{padding-left:20px}ul.next li{margin:6px 0}
footer{margin-top:48px;color:var(--muted);font-size:13px}
.wrap{overflow-x:auto}
"""


def write_html(res, written, out_dir: str) -> str:
    s = summarize(res, written)
    e = html.escape
    c = s["counts"]
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<title>Recovery report</title><style>%s</style></head><body><main>" % CSS]
    out.append("<div class='kicker'>Recover Anything &middot; recovery report</div>")
    out.append("<h1>%s</h1>" % e(headline(s)))
    shown_dir = out_dir
    home = os.path.expanduser("~")
    if home and home != "/" and shown_dir.startswith(home + os.sep):
        shown_dir = "~" + shown_dir[len(home):]
    lede = "Everything below was copied to <b>%s</b>. Nothing was written to the card." % e(shown_dir)
    out.append("<p class='lede'>%s</p>" % lede)
    stats = [(c["photo"], "photos"), (c["video"], "videos"), (c["audio"], "audio files"), (c["document"] + c["other"], "documents & other files")]
    stats = [st for st in stats if st[0]] or [(0, "files")]
    if s["named"]:
        stats.append((s["named"], "kept their original names"))
    if s["reassembled"]:
        stats.append((s["reassembled"], "rebuilt from scattered pieces"))
    if s["partial"]:
        stats.append((s["partial"], "partly recovered"))
    out.append("<div class='stats'>" + "".join("<div class='stat'><b>%d</b><span>%s</span></div>" % (n, e(t))
                                               for n, t in stats) + "</div>")
    for w in res.warnings:
        out.append("<div class='note'>%s</div>" % e(w))
    shown = 0
    media_title = "Photos and videos" if c["photo"] and c["video"] else ("Videos" if c["video"] else "Photos")
    order = lambda r: (r.meta.get("when") or "9999", (r.name or "").lower(), r.start)  # noqa: E731
    for title, keep in ((media_title, lambda r: r.status not in ("partial", "corrupt")
                         and group_of(r.kind, r.ext) in ("photo", "video")),
                        ("Partly recovered", lambda r: r.status in ("partial", "corrupt"))):
        items = sorted((r for r in written if keep(r)), key=order)
        if not items:
            continue
        out.append("<h2>%s <span class='kicker'>(%d)</span></h2>" % (e(title), len(items)))
        if title == "Partly recovered":
            out.append("<p class='lede'>Part of each of these was overwritten, damaged or could not be found. "
                       "What survived is saved in the <b>Partly recovered</b> folder.</p>")
        out.append("<div class='grid'>")
        for r in items:
            rel = os.path.relpath(r.meta["written_to"], out_dir)
            src = _thumb(r.meta["written_to"], r.kind, r.status in ("partial", "corrupt")) if shown < MAX_THUMBS else None
            shown += 1
            chip = ""
            if r.meta.get("reassembled_from"):
                chip = "<span class='chip ok'>rebuilt from %d pieces</span>" % r.meta["reassembled_from"]
            elif r.status == "partial":
                chip = "<span class='chip warn'>partial</span>"
            elif r.status == "corrupt":
                chip = "<span class='chip bad'>damaged</span>"
            elif r.status == "unverified":
                chip = "<span class='chip warn'>not checked</span>"
            elif r.meta.get("check") == "structure":
                chip = "<span class='chip'>structure checked</span>"
            img = ("<img loading='lazy' alt='' src='%s'>" % src) if src else "<div class='ph'>%s</div>" % e(
                (r.ext or "").upper().lstrip("."))
            out.append("<figure><a href='%s'>%s%s</a><figcaption>%s</figcaption></figure>" % (
                e(rel.replace(os.sep, "/")), img, chip.replace("class='chip", "class='chip badge"),
                e(os.path.basename(rel))))
        out.append("</div>")
    others = [r for r in written if r.status not in ("partial", "corrupt") and group_of(r.kind, r.ext) not in ("photo", "video")]
    if others:
        out.append("<h2>Documents and other files <span class='kicker'>(%d)</span></h2><div class='wrap'><table>"
                   "<tr><th>File</th><th>Size</th><th>Check</th></tr>" % len(others))
        for r in others:
            rel = os.path.relpath(r.meta["written_to"], out_dir)
            if r.status == "ok":
                chip = ("<span class='chip ok'>checksums verified</span>" if r.meta.get("check") == "full"
                        else "<span class='chip'>structure checked</span>")
            else:
                chip = "<span class='chip warn'>not checked</span>"
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (e(rel), _human(r.size), chip))
        out.append("</table></div>")
    if s["lost"]:
        out.append("<h2>Could not be recovered <span class='kicker'>(%d)</span></h2>" % len(s["lost"]))
        out.append("<p class='lede'>The card still remembers these names, but their contents were replaced by newer "
                   "data. No software can bring back data that has been physically overwritten.</p>")
        out.append("<div class='wrap'><table><tr><th>Name</th><th>Folder</th><th>Why</th></tr>")
        for r in s["lost"][:500]:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (e(r.name or ""), e(r.path or ""), e(r.note)))
        out.append("</table></div>")
    out.append("<h2>What to do next</h2><ul class='next'>")
    out.append("<li>Open a few files to make sure they look right, then copy this folder to a second place "
               "(another drive or cloud storage).</li>")
    out.append("<li>Only after that, format the card <b>in the camera</b> before using it again.</li>")
    if s["partial"] or s["lost"]:
        out.append("<li>Missing something? Check where else it might still exist: phone and cloud trash "
                   "(Google Photos, iCloud, OneDrive, Dropbox), messages you sent, and other cards. "
                   "<code>recover-anything hunt</code> checks your computer for copies.</li>")
    out.append("</ul>")
    out.append("<footer>Recover Anything read %s from %s in %.0f seconds and never wrote to it. "
               "Technical details for every file (where each piece came from, checksums) are in "
               "<code>recovery.json</code>.</footer>" % (_human(res.size), e(res.source), res.seconds))
    out.append("</main></body></html>")
    path = os.path.join(out_dir, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


def text_summary(res, written, out_dir: str) -> str:
    s = summarize(res, written)
    lines = [headline(s) + "."]
    if s["named"]:
        lines.append("  %d kept their original names." % s["named"])
    if s["reassembled"]:
        lines.append("  %d were rebuilt from scattered pieces." % s["reassembled"])
    if s["unconfirmed"]:
        lines.append("  %d could not be fully checked; open them to make sure they look right." % s["unconfirmed"])
    if s["partial"]:
        lines.append("  %d could only be partly recovered (in 'Partly recovered')." % s["partial"])
    if s["lost"]:
        lines.append("  %d are listed on the card but were overwritten by newer data." % len(s["lost"]))
    lines.append("Saved to: %s" % out_dir)
    lines.append("Report:   %s" % os.path.join(out_dir, "report.html"))
    return "\n".join(lines)
