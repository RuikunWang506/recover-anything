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

## Added on 6 November 2026 (second round, local session)

- **Rebuilders use more evidence** (`reassemble.py`): clusters the filesystem marks in use or that hold folders (`dir_clusters`, `allocated_map()`) are nobody's deleted data; every rebuilder runs in rounds, so each file that checks out takes its clusters out of the others' way; clusters of files saved but not proven are tried last (`soft_map`). An MP4 stretch that no frame start can confirm is filled with the fewest jumps and stays "check that it plays".
- **Files of one kind are told apart.** `scenarios.single_kind` (a dashcam, camcorder, music stick, voice recorder) showed rebuilds stitching in other recordings' pieces: on 4 cards each, 13 wrong `.MTS`, 30 wrong `.TS`, 9 wrong MP3. Now transport streams are checked against their own clocks (`more.TsClock`), a rebuilt MP3 or `.TS` is only called recovered when a clock confirmed every join, and AMR/MPEG-PS are only `ok` if never deleted. Same cards now: 0 wrong.
- **More file types** (`formats/extra.py`): old Office (.doc/.xls/.ppt), Outlook .msg, Thumbs.db, MPEG program streams (.MPG/.VOB/.MOD), RAR 4 and 5, 7z, Photoshop, AMR.
- `tools/eval_cards.py` runs the randomized cards (`mixed` and `one KIND`) and prints what came back and what's wrong.
- **The scan skips blank space** (`carve._busy`, `carve._at_sectors`). The MPEG program-stream signature begins with zero bytes, and adding it to the signature regex made the regex try a match at nearly every byte of a card's free space: the self-test went from about 20 s to about 240 s a card. Headers that begin with zero bytes are now checked only at sector starts, and blocks of one repeated byte (zeros, or 0xFF, which some cards return for erased space) are never handed to the regex. A 0xFF-filled card was already slow before this round (4 s per 64 MB): now 64 MB of either takes 0.02 s. `tests/test_scan_speed.py` checks the time and that every header next to blank space is still found.
- **MP4/MOV/M4A without checkable frames** (AAC-only voice memos, unknown codecs) used to count every sample as checked. Now only H.264/HEVC and Motion-JPEG frames count; a deleted file with none is saved as "check that it plays" (`tests/test_video_honesty.py`).
- **AMR memos end where their sound ends.** A zero byte is a valid AMR frame header, so the blank rest of the last cluster was counted as silent frames and no carved memo came back byte for byte.
- Still open from this round:
  - A rebuilt MP3 can't prove its pieces (no clock, frames look alike), so it is saved as "check that it plays". Checking the bit reservoir (`main_data_begin` against the previous frame's data) could tie frames together.
  - Fragmented SQLite and MKV/WebM aren't rebuilt. SQLite pages describe themselves (b-tree page types, child page numbers), so a page-by-page rebuild is feasible.
  - The README's numbers still come only from the camera-photo self-test. The mixed and single-kind cards have no published numbers yet.

## Added on 6 November 2026 (third round)

- **Photos are checked against the thumbnail the camera stored in them** (`jpeg.Thumb`, `jpeg.luma_pixels`). The baseline decoder falls back into step after damage: with 100 bytes wiped, a test photo still decodes to exactly the right number of blocks at 93 of 157 places. On the photos-only cards (`eval_cards.py one jpg 3`) a run of another photo's clusters decoded cleanly inside a rebuilt photo, and its bottom rows came out 20 to 65 levels darker than its thumbnail. Now the thumbnail is decoded in full, and every block of the picture is compared with the range of thumbnail values around its spot (a thumbnail is rarely aligned to the pixel). Over a run of 32 MCUs in decoding order, a mean distance over 20 levels, or a mean step over 25 levels where the thumbnail is flat (a step in the blocks' DC values), means another picture's data. On 443 correctly recovered photos (four encoders, letterboxed thumbnails, hard edges) neither went above 6.4 and 12.6; the wrong rebuild gave 49 and 63. A rebuilt photo that fails goes back to the search where it stops matching; a deleted photo in one piece that fails is saved whole as "check it". Files the filesystem still lists aren't checked this way (an edited photo may keep a stale thumbnail).
- **Trial decodes no longer leave other photos' blocks behind.** A candidate search wrote into the block table that seam scores and the thumbnail check read, and a search that found nothing left those values there (`tests/test_thumbnail_check.py`).
- **PDF, AVI, GIF, WAV and RAW are checked by their contents** (`formats/checks.py`). New one-kind cards (`eval_cards.py one pdf|docx|wav|avi|raw|gif`, a USB stick of documents, a voice recorder, a trail camera, a DSLR) found 55 wrong PDFs, 14 wrong WAVs, 19 wrong RAWs and 1 wrong GIF on two cards each, all called recovered on a structure walk. Two were plain bugs in `measure_pdf`: it read 1 MB at a time past its limit (the next PDF's header) and took the last end marker it saw; and it took a deleted PDF's leftover tail after the file for part of it, whose table pointed past the end and listed nothing to check. Now a PDF ends at its last end marker whose startxref points back into the file, every object must sit where the table says, and every compressed stream must decompress with a matching checksum (an embedded JPEG is decoded). AVI chunks must chain, every Motion-JPEG frame is decoded (up to 64 MB a file; beyond that, its start and end markers), and the index must match. GIF frames must decode to exactly the right number of pixels. WAV audio, WebP and a RAW's sensor data can't be checked: only a file that was never deleted is called recovered. On four cards of each: PDF 78/112, DOCX 79/112, AVI 77/112, GIF 90/112 recovered, WAV 83 and RAW 77 saved exact but labeled, 0 wrong. `eval_cards.py` counts a found file that is part of a written one (a video's frame, a RAW's preview) apart from wrong ones. A PDF is measured past headers that turn up inside it (compressed data that looks like one by chance, a picture it holds), but never past the next PDF (`carve._measure_pdf`): three PDFs in one piece were lost that way. A container whose contents can't all be proven (a deleted RAW) still claims what its own structure points to (`pipeline._can_claim`), so its preview isn't listed twice.
- Still open: on `eval_cards.py one jpg 4`, one photo's own cluster (1364) is claimed as part of another broken photo's first piece, because the other photo's data decodes straight on into it. The search, unable to use 1364, finds three clusters of two other photos that finish the picture with exactly the right number of blocks; they cover only the plain bottom band every photo on that card shares, so nothing (decoder, seams, thumbnail) can tell them apart. Assigning clusters to all broken photos jointly, rather than one photo at a time, is the fix.

## Added on 7 November 2026

- **Split Word/Excel files (anything stored as ZIP) and PDFs are rebuilt** (`reassemble_stream._Zip`, `_Pdf`). Before, a split ZIP was dropped without a trace: `measure_zip` searched the rest of the card for an end record that matched, found none and gave up (a full read of the card for each one). Now it follows the members' local headers and keeps what checked out as the first piece (`_zip_walk`), looking up to 16 MB further for the end record first (an Android app keeps its signing block there). A PDF with no end marker in reach is kept as its first piece too. The ZIP walker inflates each member's data and checks its CRC, and the directory must list exactly the members walked; the PDF walker goes object by object, decompresses every Flate stream and checks every cross-reference table, and the rebuilt PDF is checked once more as a whole (`final`). Data stored without compression (Word's pictures, a font zlib couldn't shrink, kept in raw 64 KB blocks) is only checked at its end, so a walker says how far that is (`horizon`) and the search reaches that far. On four cards each: PDF 103/112 recovered (25 of 34 split ones), Word 111/112 (32 of 33), 0 wrong.
- **Split trail-camera videos (Motion-JPEG AVI) and GIFs are rebuilt** (`_Avi`, `_Gif`). The AVI walker follows the chunk chain, decodes every frame as a whole and checks the index at the end; the GIF walker decodes each picture's LZW data as it goes and wants exactly its number of pixels. Sound chunks and GIF comments can't be checked (`weak`): a rebuild with a join inside one is saved as "check that it plays", and a check that fails right after one sends the search back to where it began. On four cards each: AVI 107/112 recovered (30 of 35 split), GIF 110/112 (20 of 22), 0 wrong.
- **Faster rebuilds.** A JPEG decoder used to build its Huffman lookup tables (65,536 entries each) from scratch: they are now built once per distinct table (`jpeg._huff`), since a camera uses the same few for everything. The stream rebuild keeps the walker's state at every cluster (`_Rebuild.snaps`), so a trial piece starts just before its slot instead of re-checking everything since the last fix. An AVI card went from 85-266 s to 26-61 s.

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
- [x] Clear the stuck `RACARD` and `RAINNER` disks (they were gone by 6 November). Until the test is fixed, running it on macOS 26 jams every disk-image operation on the Mac (`hdiutil`, `diskutil`, even app installs); deselect it locally.
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
