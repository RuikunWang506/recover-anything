# Handoff: Recover Anything

Status on 5 November 2026. This picks up where the cloud build session left off. The project rules and the code map are in `CLAUDE.md`, so they aren't repeated here.

## Where things stand

- **Repo:** github.com/RuikunWang506/recover-anything, branch `main`, **private**.
- **Size:** about 8,000 lines of Python in `src/` and 79 tests.
- **CI:** green on all four jobs (Ubuntu with Python 3.9, Ubuntu, macOS and Windows with 3.13).
- **Self-test:** every file recovered byte for byte on all 7 simulated cards: FAT32 and exFAT; deleted, formatted, formatted then reused, and heavily fragmented.
- **Stress test:** 9 randomized cards with fixed seeds. It recovered 432 of 432 files byte for byte, including 104 of 104 fragmented photos and 32 of 32 videos (12 of them fragmented). No file was marked recovered unless it was exact.
- **Not done yet:**
  - It has never been run on a physical card.
  - It isn't on PyPI.
  - The repo is still private, so the README's `pipx install git+https://…` line only works for you.

## Pick it up on your computer

```bash
git clone https://github.com/RuikunWang506/recover-anything
cd recover-anything
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python -m pytest -q -rs
recover-anything selftest --quick
claude
```

On a Mac, the test suite briefly attaches two small disk images (they show up under `/Volumes` and are detached again). On Windows, the disk-image test needs an Administrator terminal and skips otherwise.

A good first message for Claude Code:

> Read CLAUDE.md and docs/HANDOFF.md. Run the tests and the quick self-test, and tell me the results before changing anything. Then let's work on next step 1 from the handoff.

## What the safety reviews found (all fixed, all covered by tests)

Two independent reviews tried to break the tool on purpose. Don't reintroduce any of these.

| Problem | Fix | Test |
|---|---|---|
| Saving onto the card through an overlay, FUSE folder, encrypted folder or loop-mounted disk image | The guard follows every storage layer and refuses what it can't resolve | `test_safety.py::test_guard_follows_overlays_and_images_stored_on_the_card`, `test_guard_classifies_stacked_and_network_filesystems` |
| A source image file that is also mounted read-write | Refused when the output sits on a mount backed by that file | `test_guard_protects_an_image_that_is_mounted` |
| Windows: `\\.\E:\`, `\\?\Volume{…}\`, `\\localhost\E$\`, junctions and symlinks to the card | The final path is resolved first; prefixes and shares the PC serves itself are mapped back to their drive; `.vhd` outputs are refused | `test_guard_platforms.py` (stubbed), `test_guard_real.py` (real `.vhd` in CI) |
| macOS: FUSE and FSKit mounts of the card, and disk images stored on it | The volume comes from statfs, and disk images are followed to their file; it refuses if diskutil doesn't answer | `test_guard_platforms.py`, `test_guard_real.py` (real FAT32 `.dmg` in CI) |
| Two lightly used cards of the same size got the same fingerprint, so card B could reuse card A's image and return A's photos | The fingerprint includes the partition table, each volume's boot area, its allocation tables and its file listing | `test_lightly_used_cards_of_the_same_size_get_different_fingerprints` |
| A damaged or fragmented RAW "contained" the intact photos after it, and they vanished | A file can only claim bytes it verified, or offsets its own structure points to | `test_embedded.py` |
| Under sudo, the whole output folder was re-owned, following symlinks | Only paths the run created, with `follow_symlinks=False` | `test_ownership_is_handed_back_only_for_what_the_tool_created` |
| A 0.8 MB ZIP took 1.5 GB of memory; a 20-byte video index could ask for millions of frames | Inflate in 1 MB steps; compact sample tables; linear-time sample walk | `test_zip_check.py`, `test_formats.py` |
| 42 seconds of uncompressed audio disabled rebuilding the whole video | The frame cap applies only to the H.264/HEVC tracks that are checked | `test_fragmented_video_with_long_pcm_audio_is_rebuilt` |
| Intertwined fragmented photos, and a video whose index ends in a cluster of old photo data | First-piece claims with alternating passes; judge only the bytes a file uses in its last cluster | `test_stress_cards.py` (stress seeds 10 and 11) |

The first review's fixes (the fail-closed guard, crash-proof parsers and honest labels) are in commit `e662cc6`. In the second review, the parsers survived 320,000 random format mutations and 5,000 randomized card scans without a crash or a hang.

## Added on 5 November 2026 (local session, not yet committed)

- **New file types:** MP3/MP2, FLAC, Ogg (Opus, Vorbis), MPEG-TS and AVCHD `.MTS`, MKV/WebM, SQLite, gzip, BMP (`formats/more.py`). Each one is measured to its exact length and checked: Ogg, FLAC and gzip by their checksums, SQLite with `PRAGMA integrity_check` on a temporary copy, H.264/H.265 in MKV frame by frame, MP3 and MPEG-TS frame by frame.
- **Folders by what a file is:** HEIC/AVIF/CR3 go to Photos (they were in Videos), WAV/M4A/MP3/FLAC/Ogg to a new Audio folder. `--only` now also filters files the folders name, and accepts `audio`.
- **Rebuilding fragmented recordings and archives** (`reassemble_stream.py`): MP3, MPEG-TS, Ogg, FLAC, gzip.
- **Randomized mixed-device cards** (`scenarios.mixed_device`): photos, videos, music, voice memos, dashcam and camcorder clips, screen recordings, databases, logs and scans, 3 rounds of save/delete, formatted. On 20 FAT32 cards and 8 exFAT cards, nothing was marked recovered that wasn't byte for byte a real file (numbers in the commit message).
- **Honesty rules found by those cards** (see CLAUDE.md rule 3): no-checksum types need a never-deleted file; stream ends and starts must be proven; a newer file of the same container (HEIC in an old MP4's space) or the same kind (a longer clip in an old clip's space) no longer passes as the old file.
- Still open from this work:
  - Fragmented gzip: garbage often decodes as "copy earlier text", so only the final checksum catches it and can't say which cluster was wrong. It never produces a wrong file, but rebuilding is hit or miss.
  - Fragmented MKV/WebM and SQLite aren't rebuilt yet. SQLite pages describe themselves, so a page-by-page rebuild is feasible.
  - BMP, HEIC and WebM from deleted files are saved but labeled "not proven". HEIC's HEVC tiles and WebM's VP9 frames have no internal checks.
  - Not yet: NTFS/ext4/APFS names, 7z/RAR, OLE2 (old .doc/.xls), PSD, MPEG-PS (.MPG/.VOB).

## Known limitations and open problems

1. **Only simulated hardware so far.** Everything has run on card images, loop devices and CI disk images. Raw device reads on a real Mac and a real Windows PC are untested. So are permission prompts, failing cards and USB readers.
2. **Speed.** Decoding is pure Python, at roughly 1.5 MB/s per core across all cores. Above 8 GB of photos it checks structure plus a random sample (`validate.py`: `DEEP_LIMIT`, `SAMPLE`). A big card can take hours.
3. **Photo rebuilding is the hard part, and the moat.**
   - How it works (`reassemble.py`): it walks forward, skipping clusters owned by verified files. At a decode error it searches candidates, scored by DC brightness jumps across the seam (`seam_score`).
   - Why it's hard: when two photos come from the same camera with the same tables and no restart markers, another photo's data decodes without error. Only the seam score can tell them apart, and it is weak.
   - The ideas that would help most:
     - Score edges in pixels, estimated from low-frequency AC coefficients, instead of DC only.
     - Rank candidates by allocation order.
     - Assign fragments jointly across all broken photos.
   - Use `tools/debug_card.py` to see why a photo missed.
4. **Weakly checked files still block clusters while photos are rebuilt.** This covers RAW, PDF, HEIC, GIF and RIFF, and keeps the search fast. A fragmented RAW can therefore hide another photo's piece, and that photo is saved as partial.
5. **Video rebuilding** covers MP4/MOV/M4V/3GP with an H.264 or HEVC index. `_materialize` and `_check` rebuild sets on every step, which may be slow for multi-GB videos on small clusters (untested at that size). AVCHD (`.MTS`) is found and rebuilt by `reassemble_stream.py`.
6. **Containers that don't point to their contents.** Some previews may appear as extra photos: Nikon MakerNote previews, CR3 previews, JPEGs inside H.264 MP4s. These are duplicates, not losses.
7. **Carving edge cases:**
   - A PDF is measured only up to the next carved header, so a PDF holding a sector-aligned JPEG is cut short.
   - ZIP64 archives get a structure check only.
   - AVI files over 1 GB (OpenDML) come out in pieces.
8. **Save-guard trade-offs.** These outputs are refused, and you can override with `--trust-output-location` when you're sure:
   - Windows `.vhd`/`.vhdx` and Storage Spaces. `GetStorageDependencyInformation` could follow them instead.
   - macOS outputs when `diskutil` doesn't answer.
   - FUSE folders that don't name where they store files.
   - ZFS without `zpool` available.
9. **No names on NTFS, APFS or ext4.** Their files are carved without names.
10. **The reviewers' reproducer scripts lived in the cloud session and aren't in the repo.** Their cases are covered by the tests in the table above.

## Next steps

### 0. Open item: macOS disk-image test leaves disks attached (found 5 November 2026)

- On a Mac running macOS 26, `test_mac_card_image_and_an_image_stored_on_it` left both test disks (`RACARD` and `RAINNER`) attached.
  - `hdiutil detach -force` fails with "timeout for DiskArbitration expired".
  - The FAT32 driver process (`com.apple.fskit.msdos`) for the inner image is frozen. That image file sits on the outer disk, so neither can detach.
  - The old test ignored the detach result, so it still passed.
- **Fixed in the test:** `_detach()` in `tests/test_guard_real.py` now retries once, then fails with a message naming every disk still attached. Two stubbed tests cover it.
- [ ] Restart the Mac to clear the stuck `RACARD` and `RAINNER` disks. They can't be ejected.
- [ ] After the restart, rerun `python -m pytest -q -rs tests/test_guard_real.py`.
  - If it fails on the detach every time, the nested FAT32 image hangs under FSKit. Consider an inner image with another filesystem (for example HFS+), still stored on the card.
- [ ] Check whether CI's macOS job shows the same hang.

### 1. Test on a real card (before any launch)

1. Take a spare SD card and a camera. Shoot about 50 photos and a few videos.
2. Delete about 15 of them and shoot 20 more, so new files fill the holes and fragment.
3. Format the card in the camera.
4. On a Mac, run:
   ```bash
   recover-anything devices
   sudo recover-anything triage /dev/rdiskN
   sudo recover-anything recover /dev/rdiskN -o ~/Desktop/Recovered
   ```
5. Repeat on Windows from an Administrator terminal with `\\.\PhysicalDriveN`.

What to check:
- The card is found and named correctly.
- Permission messages make sense.
- Speed is acceptable.
- Saving onto the card is refused: try `-o /Volumes/NO\ NAME/x`.
- The report reads well.
- Counts look right compared with what you shot.

Turn anything that goes wrong into an issue, and add a simulated scenario that reproduces it.

### 2. Launch prep (needs your accounts)

- [ ] Delete this file, or move it out of the repo, if you'd rather it not be public.
- [ ] Make the repo public. Add a description, topics and a social preview.
  - Topics: `data-recovery`, `photo-recovery`, `sd-card`, `file-carving`, `forensics`, `claude-code`, `agent-skill`.
  - Social preview: `docs/before-after.jpg` works.
- [ ] Publish to PyPI.
  - Check that the name `recover-anything` is free.
  - Set up PyPI Trusted Publishing for this repo.
  - Add a release workflow (none exists yet) that builds and uploads on a `v*` tag, then tag `v0.1.0`.
  - Switch the README quick start to `pipx install recover-anything`.
- [ ] Add a short demo GIF of a recovery at the top of the README (for example with vhs or asciinema). This is the biggest lever for stars.
- [ ] Add issue templates: "Real card report", which asks for `triage` output (it contains no file contents), and "Bug".
- [ ] Launch posts: Show HN, r/photography, r/datarecovery and r/ClaudeAI, plus awesome lists for Claude Code skills and forensics.

### 3. Roadmap

- Faster checking, for example an optional native JPEG decoder used only when installed, keeping zero required dependencies.
- Better photo rebuilding (see limitation 3).
- HEIC and RAW reassembly, and reassembly for videos whose index is gone.
- NTFS undelete with names, and AVCHD (`.MTS`).
- Phone backups and chat apps; an MCP server; a small desktop app.

## Debugging recipes

**Why did a stress card miss a file?**

```bash
python tools/debug_card.py 11                          # seed 11, as in selftest --stress
python tools/debug_card.py 11 --explain IMG_0050.JPG    # explain a file even if it was recovered
```

It prints each missed file's real cluster pieces, which file last wrote the clusters around each break, and what was found at its first cluster.

**Trace the photo rebuilder's decisions.** Wrap `_Assembler.run`, `_Assembler._fix` and `_Assembler._search` in `recover_anything/reassemble.py`, then run `scan()`:

```python
from recover_anything import reassemble as R
orig = R._Assembler._search
def search(self, dec, path, buf, states, j, rejected, mine):
    got = orig(self, dec, path, buf, states, j, rejected, mine)
    print("slot", j, "after cluster", path[j - 1], "->", got)
    return got
R._Assembler._search = search
```

**Root-only guard tests (Linux):** `sudo .venv/bin/python -m pytest -q tests/test_safety.py`.

**CI:** `gh run list` and `gh run view <id> --log-failed`.

## How we got here

1. **The idea.** It was picked from the idea list as the most promising open-source project. An early experiment showed carving recovers deleted and formatted cards fine, but only 1 of 30 fragmented photos. That fragmentation problem became the focus.
2. **v0.1 (built in the cloud session):**
   - FAT/exFAT parsing with deleted entries and folders that survive a format.
   - Every-sector carving for 8 format families.
   - The pure-Python JPEG decoder, photo and video rebuilding, and the simulated cards and self-test.
   - `hunt`, the report and the agent skill.
3. **Review 1.** The save guard was rebuilt on device identity and made fail-closed, and the parsers were hardened against fuzzing (`e662cc6`).
4. **Review 2 and the stress misses.** These were fixed in `881f50f` through `73dcd4f` (see the table above). This handoff came after them.
