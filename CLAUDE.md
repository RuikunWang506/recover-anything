# Recover Anything: notes for Claude Code

In a new session, read `docs/HANDOFF.md` first. It has the current status, the open problems and what to do next.

Recover Anything gets deleted and formatted photos, videos and documents back from SD cards, USB sticks and disk images, without ever writing to the source. It ships as the `recover-anything` command plus an agent skill. It is pure Python 3.9+ with **no runtime dependencies** (standard library only). Pillow and pytest are dev extras, used to build the simulated test cards.

## Commands

```bash
python -m pip install -e ".[dev]"        # once, inside a virtualenv
python -m pytest -q -rs                   # about 100 tests, about 5 min
recover-anything selftest --quick         # 4 small simulated cards, about 1 min
recover-anything selftest                 # the README results table (7 cards), about 3 min
recover-anything selftest --stress 9      # the README stress numbers (seeds 6-14), about 6 min
python tools/debug_card.py 11             # rebuild one stress card and explain every miss
```

## Rules that must never break

1. **Never write to the source.** Sources are opened read-only (`source.py`). No code path may open a source device or source image for writing.
2. **The save guard fails closed.** `devices.check_output(source, out_dir)` must prove that writes into the output can't land on the source. It follows every layer: partitions, LVM/LUKS/RAID, loop devices and the image files behind them, overlay and FUSE folders, btrfs and ZFS member disks. On Windows it also follows links, junctions, `\\?\` paths and shares served by the machine itself. On macOS it follows APFS containers and attached disk images. If it can't tell, it refuses. A reason containing a word in `devices.DEFINITE` ("same disk", "folder", "itself") can never be waved through by `--trust-output-location`.
3. **"ok" means verified.**
   - A file is only marked recovered when its check passed. For a JPEG, it decoded to its last block with the right block count. For a video, every frame start matched its index. For a PNG or ZIP, the checksums matched.
   - Everything else is `partial`, `corrupt` or `unverified`, saved under "Partly recovered" with a label.
   - The stress test counts files "marked recovered but wrong". That count must stay 0. So must the `mixed_device` scenarios (`tests/test_more_formats.py`).
   - A type with no checksums (BMP, HEIC/AVIF/CR3 pictures, VP8/VP9/AV1 video) is `ok` only for a file that was never deleted (`validate.layout_known`): a deleted file's clusters can be reused and freed again without a trace.
   - A stream without an end marker (MP3, MPEG-TS) is `ok` only when its end is proven: a length in the file or the folder entry, a tag, or frames that stop inside the cluster holding the last checked header (`more.same_cluster`). A carved one must also mark its start (an ID3 tag or Xing header; transport-stream counters at 0), or it may be the second half of a recording.
4. **One bad file never stops a run.** Parsers and rebuilders run inside a try/except per file. Work on hostile or huge input is bounded (`MAX_SAMPLES`, `ZIP_CHECK_BUDGET`, `MAX_JPEG`, `MAX_MOOV`, chain limits).
5. **README numbers come from real runs.**
   - Change the results table only from `recover-anything selftest` output.
   - Change the stress paragraph only from `recover-anything selftest --stress 9` output.
   - Simulated cards are built from fixed seeds, so the numbers are reproducible. Seed every new random source to keep it that way.
6. **Under sudo, hand back only what the run created.** `cli._give_back_to_user` re-owns exactly those paths and never follows symlinks.

## Where things are

| File | What it does |
|---|---|
| `cli.py` | Commands, refusal messages, imaging before recovery, handing files back under sudo |
| `devices.py` | Disk listing, the save guard (`where`, `check_output`), system-disk detection. Linux: sysfs and mountinfo. macOS: statfs, diskutil and hdiutil. Windows: ctypes. |
| `source.py` | Read-only, aligned, cached reads; unreadable sectors become zeros and are logged |
| `imaging.py` | Card-to-image copy (GNU ddrescue when installed), card fingerprint (`identity`), safe resume and reuse |
| `volume.py` | MBR/GPT, filesystem detection, backup boot sectors |
| `fs/fat.py`, `fs/exfat.py` | Folder walk including deleted entries, and folders that survived a format |
| `carve.py` | Signature scan of every 512-byte sector, exact length of each hit |
| `formats/jpeg.py` | JPEG header parser, length measurement, resumable pure-Python baseline decoder |
| `formats/others.py` | MP4/MOV/HEIC sample tables, PNG, GIF, RIFF/AVI, TIFF/RAW, PDF, ZIP |
| `formats/more.py` | MP3/MP2, FLAC, Ogg, MPEG-TS/MTS, MKV/WebM, SQLite, gzip, BMP: probe, exact length, honest check |
| `validate.py` | Per-file checks; deep, fast and sampled modes; process pool; ZIP checksums |
| `pipeline.py` | `scan()`: filesystem pass, carve, check, de-duplicate, rebuild, drop files that are part of other files |
| `reassemble.py` | Rebuilds fragmented JPEGs: gap skipping, first-piece claims, seam scores |
| `reassemble_video.py` | Rebuilds fragmented MP4/MOV from the video's own frame index |
| `reassemble_stream.py` | Rebuilds fragmented MP3, MPEG-TS, Ogg, FLAC and gzip with resumable unit-by-unit walkers |
| `organize.py`, `report.py` | Output folders, names, dates, `recovery.json`, self-contained `report.html` |
| `triage.py`, `hunt.py` | Honest odds and don'ts; other places copies survive (Claude Code transcripts, git, editors, trash, snapshots) |
| `selftest.py`, `simcard/` | Simulated FAT32/exFAT cards, test photos and videos, disaster scenarios; `simcard/files.py` builds real music, voice memo, dashcam, MKV, SQLite, gzip and BMP files from fixed seeds; `scenarios.mixed_device` is a randomized card shared by many devices |

## Conventions

- **User-facing text** (CLI messages, report, README, SKILL.md): plain, short sentences that say what happened and what to do. No jargon.
- **The skill exists twice and must stay identical:** `skills/recover-anything/SKILL.md` and `src/recover_anything/skill/SKILL.md` (the packaged copy that `install-skill` uses).
- **Every bug fix gets a test that fails without the fix.** New disaster scenarios go in `simcard/scenarios.py`.
- **Tests that need root and loop devices** run on Linux only and skip elsewhere (run `sudo python -m pytest` to include them). `tests/test_guard_real.py` attaches real disk images: hdiutil on macOS, diskpart `.vhd` on Windows (needs admin).
- **CI** (`.github/workflows/ci.yml`) runs Ubuntu 22.04 with Python 3.9, plus the latest Ubuntu, macOS and Windows with 3.13. Use `gh run list` and `gh run view --log-failed` to read failures. The real-disk details are printed in the step "save guard on real disk images (details)".
