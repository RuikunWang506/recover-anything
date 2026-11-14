# Recover Anything: notes for Claude Code

In a new session, read `docs/HANDOFF.md` first. It has the current status, the open problems and what to do next.

Recover Anything gets deleted and formatted photos, videos and documents back from SD cards, USB sticks and disk images, without ever writing to the source. It ships as the `recover-anything` command plus an agent skill. It is pure Python 3.9+ with **no runtime dependencies** (standard library only). Pillow and pytest are dev extras, used to build the simulated test cards.

## Commands

```bash
python -m pip install -e ".[dev]"        # once, inside a virtualenv
python -m pytest -q -rs                   # about 180 tests, about 7 min
recover-anything selftest --quick         # 4 small simulated cards, about 1 min
recover-anything selftest                 # the README results table (7 cards), about 3 min
recover-anything selftest --stress 9      # the README stress numbers (seeds 6-14), about 6 min
python tools/debug_card.py 11             # rebuild one stress card and explain every miss
python tools/eval_cards.py mixed 1-20     # randomized cards shared by many devices: what came back, what's wrong
python tools/eval_cards.py one mts 1-4    # cards holding only one kind of file (mp3 ts mts flac opus mp4 jpg amr mpg doc pdf docx wav avi raw gif heic webm mkv)
python tools/mp3_stats.py LIST            # how real MP3s (paths listed in a file) use the bit reservoir
```

## Rules that must never break

1. **Never write to the source.** Sources are opened read-only (`source.py`). No code path may open a source device or source image for writing.
2. **The save guard fails closed.** `devices.check_output(source, out_dir)` must prove that writes into the output can't land on the source. It follows every layer: partitions, LVM/LUKS/RAID, loop devices and the image files behind them, overlay and FUSE folders, btrfs and ZFS member disks. On Windows it also follows links, junctions, `\\?\` paths and shares served by the machine itself. On macOS it follows APFS containers and attached disk images. If it can't tell, it refuses. A reason containing a word in `devices.DEFINITE` ("same disk", "folder", "itself") can never be waved through by `--trust-output-location`.
3. **"ok" means verified.**
   - A file is only marked recovered when its check passed. For a JPEG, it decoded to its last block with the right block count, and a deleted or rebuilt one also matches the thumbnail the camera stored in it (`jpeg.Thumb`): the decoder falls back into step after damage, so a run of another photo's data can decode cleanly. Each piece a rebuild placed after a jump must also cover some of the picture the thumbnail shows (`Thumb.reach`): at the edges it doesn't reach, nothing but the decoder checks a piece. For a video, every frame start matched its index. For a PNG or ZIP, the checksums matched.
   - Everything else is `partial`, `corrupt` or `unverified`, saved under "Partly recovered" with a label.
   - The stress test counts files "marked recovered but wrong". That count must stay 0. So must the `mixed_device` scenarios (`tests/test_more_formats.py`).
   - A HEIC or AVIF picture is checked like a video's frames: every tile listed in its item table must start with a valid HEVC/AV1 unit whose lengths fill the tile exactly (`others.validate_heif`).
   - Video in MKV and WebM is checked frame by frame like an MP4's: H.264/H.265 frames by their units, VP9 by its header and tile sizes, VP8 by its partition sizes, AV1 by its unit sizes (`more.VIDEO_FRAME_CHECKS`, run on 204,638 frames of 606 real WebM files without one rejected). Compressed or encrypted frames can't be checked.
   - A type with no checksums (BMP, CR3 pictures, WAV and WebP, a RAW's sensor data, uncompressed or encrypted PDF streams) is `ok` only for a file that was never deleted (`validate.layout_known`): a deleted file's clusters can be reused and freed again without a trace. PDF, AVI and GIF are checked by their contents (`formats/checks.py`).
   - A stream without an end marker (MP3, MPEG-TS) is `ok` only when its end is proven: a length in the file or the folder entry, a tag, or frames that stop inside the cluster holding the last checked header (`more.same_cluster`). A carved one must also mark its start (an ID3 tag or Xing header; transport-stream counters at 0), or it may be the second half of a recording.
   - Files of one kind fragment into each other's holes, and another recording's data passes checks that junk fails: MP3 frames, transport-stream counters, AMR and MPEG-PS frames all look alike from file to file. So a rebuilt transport stream is `ok` only when the recording's own clock (`more.TsClock`: the PCR, and an `.MTS` file's per-packet time stamps) checked out after every join. MP3 has no clock, only its bit reservoir (`more.Reservoir`): a frame can't point further back than the room the frames before it left (no real file breaks this, so a carved MP3 stops there), and one that points exactly there is tied to them. A tie is weak (in real files, a frame of another recording ties about one time in 370, and frames of any recording point at 0 or the encoder's ceiling: `tools/mp3_stats.py`), so a rebuilt MP3 is `ok` only when every join is the first cluster it could go on in (`reassemble_stream._first_choice`: nothing skipped but clusters in use, claimed or empty) and ties at a value the song seldom points at. A rebuilt MKV or WebM is `ok` only when a checked header (a block, a cluster) falls in the cluster right after every join, and the join is the first cluster it could go on in, as for MP3: another video's frames pass the same checks, and inside a frame's data nothing is checked. AMR and MPEG-PS are `ok` only for a file that was never deleted. `tools/eval_cards.py one KIND` must report 0 wrong for every kind.
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
| `fs/ntfs.py` | NTFS from the MFT: every record, live or deleted, with its name, parent folder (from parent links, not folder indexes) and runlist, so a deleted file's pieces are known exactly; `chain` is `runlist`. Compressed, encrypted and sparse files aren't read |
| `fs/fat.py`, `fs/exfat.py` | Folder walk including deleted entries, and folders that survived a format; `dir_clusters` and `allocated_map()` tell the rebuilders which clusters can't hold deleted data |
| `carve.py` | Signature scan of every 512-byte sector, exact length of each hit. Blank blocks (all zeros or all 0xFF) are skipped, and headers that begin with zero bytes are checked only at sector starts (`AT_SECTOR`): never add a zero-led signature to the regex |
| `formats/jpeg.py` | JPEG header parser, length measurement, resumable pure-Python baseline decoder, and the EXIF-thumbnail check (`Thumb`: every block's brightness against the thumbnail, over runs of MCUs in decoding order) |
| `formats/others.py` | MP4/MOV sample tables, HEIC/AVIF item tables (`heif_items`), PNG, GIF, RIFF/AVI, TIFF/RAW, PDF, ZIP |
| `formats/more.py` | MP3/MP2 (with Layer III's bit reservoir: `mp3_side`, `Reservoir`), FLAC, Ogg, MPEG-TS/MTS, MKV/WebM (each frame checked: `VIDEO_FRAME_CHECKS`), SQLite, gzip, BMP: probe, exact length, honest check. Holds the registry (`KINDS`) for these and `extra.py` |
| `formats/extra.py` | Old Office and other OLE2 files, MPEG program streams, RAR, 7z, Photoshop, AMR |
| `formats/checks.py` | Content checks: PDF (every object where its table says, every compressed stream's checksum), AVI (the chunk chain, every frame decoded), GIF (every frame decoded to the right number of pixels), TIFF/RAW (previews decoded; a camera's sensor data can't be) |
| `validate.py` | Per-file checks; deep, fast and sampled modes; process pool; ZIP checksums |
| `pipeline.py` | `scan()`: filesystem pass, carve, check, de-duplicate, rebuild, drop files that are part of other files |
| `reassemble.py` | Rebuilds fragmented JPEGs: gap skipping, first-piece claims, seam scores; a finished picture that doesn't match its thumbnail sends the search back to where it stops matching. `reassemble_all` runs every rebuilder in rounds (each file that checks out takes its clusters out of the others' way) and builds the maps they share: clusters in use or holding folders (blocked), and clusters of files saved but not proven (`soft_map`, tried last) |
| `reassemble_video.py` | Rebuilds fragmented MP4/MOV from the video's own frame index, and HEIC photos from their item table (each tile's start is a checkpoint); an unconfirmable stretch is filled with the fewest jumps (`_fewest_jumps`) and stays "check that it plays" |
| `reassemble_stream.py` | Rebuilds fragmented MP3, MPEG-TS, Ogg, FLAC, gzip, ZIP (Word, Excel...), PDF, Motion-JPEG AVI (every frame decoded), GIF (every picture decoded) and MKV/WebM (every frame checked by its codec; the file's own length ends it: `ends_known`) with resumable unit-by-unit walkers; a join counts only when a checksum or the recording's clock confirms it (for MP3: the bit reservoir, at a join no other cluster could have filled). A ZIP's directory and a PDF's table must list exactly what was walked. Where nothing can be checked until further on (data stored uncompressed), a walker says how far (`horizon`) and the search reaches that far. Stretches nothing checks (AVI sound, GIF comments) are `weak`: a rebuild with a join inside one is only "check that it plays". Walker states are kept per cluster so a trial starts just before its slot; within a search round, slots where the path jumped are tried first |
| `organize.py`, `report.py` | Output folders, names, dates, `recovery.json`, self-contained `report.html` |
| `trash.py` | Files a computer moved into the trash folder on the card (`.Trashes`, `.Trash-<uid>/files`, `$RECYCLE.BIN`), and photos an Android gallery renamed to `.trashed-<expiry>-NAME`: deleted by the user though still allocated, so `recover` copies them by default; original names from `.trashinfo` and `$I` records; the trash's own records are never output |
| `clutter.py` | Files a system leaves on a card for itself (a Mac's `._` companions, `.DS_Store`, `.Spotlight-V100`, `.fseventsd`, `System Volume Information`): never saved, never reported lost, never counted. `Thumbs.db` is not clutter: it holds previews of deleted photos |
| `triage.py`, `hunt.py` | Honest odds and don'ts; other places copies survive (Claude Code transcripts, git, editors, trash, snapshots) |
| `selftest.py`, `simcard/` | Simulated FAT32/exFAT cards, test photos and videos, disaster scenarios; `simcard/files.py` builds real music, voice memo, dashcam, camcorder, MKV, SQLite, gzip, BMP, Office 97, RAR, 7z, Photoshop, PDF, Word, WAV, Motion-JPEG AVI, Canon RAW and GIF files from fixed seeds; `scenarios.mixed_device` is a randomized card shared by many devices, `scenarios.single_kind` one holding a single kind, `scenarios.mac_copied` photos with a Mac's `._` companions and system files, `scenarios.trashed` photos moved into the card's trash folders, `scenarios.android_trashed` a phone gallery's trash, `scenarios.ntfs_drive` an external NTFS drive (`simcard/ntfs.py` writes real MFT records, runlists and $Bitmap; NTFS hands a new file the lowest free record), `scenarios.unopenable` a card whose boot sector or partition table was wiped (no computer can open it, so `recover` copies every listed file: `Volume.os_cannot_open`) (the simulated cards' `move` renames like an OS: the data stays put) |

## Conventions

- **User-facing text** (CLI messages, report, README, SKILL.md): plain, short sentences that say what happened and what to do. No jargon.
- **The skill exists twice and must stay identical:** `skills/recover-anything/SKILL.md` and `src/recover_anything/skill/SKILL.md` (the packaged copy that `install-skill` uses).
- **Every bug fix gets a test that fails without the fix.** New disaster scenarios go in `simcard/scenarios.py`.
- **Tests that need root and loop devices** run on Linux only and skip elsewhere (run `sudo python -m pytest` to include them). `tests/test_ntfs_real.py` builds a real NTFS volume with `mkntfs` and `ntfs-3g` (needs root and the `ntfs-3g` package); CI runs it on Ubuntu as its own step. `tests/test_guard_real.py` attaches real disk images: hdiutil on macOS, diskpart `.vhd` on Windows (needs admin). On macOS, never store a FAT or exFAT image on the FAT test card: one FSKit volume on another freezes at the unmount and jams every disk-image operation on the Mac (the image on the card is HFS+ for that reason).
- **CI** (`.github/workflows/ci.yml`) runs Ubuntu 22.04 with Python 3.9, plus the latest Ubuntu, macOS and Windows with 3.13. Use `gh run list` and `gh run view --log-failed` to read failures. The real-disk details are printed in the step "save guard on real disk images (details)". Its `package` job builds the package and checks the wheel installs and runs. `main` has a branch rule that requires all five jobs by name before a pull request can merge: rename a job or change the matrix and the rule on GitHub must change too.
- **Releases** (`.github/workflows/release.yml`): pushing a tag `vX.Y.Z` that matches `version` in `pyproject.toml` builds the package and publishes it to PyPI (Trusted Publishing, set up on pypi.org).
