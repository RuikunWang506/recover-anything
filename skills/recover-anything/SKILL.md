---
name: recover-anything
description: Recover deleted, formatted or lost photos, videos and files from SD cards, USB drives, hard drives and disk images, and find surviving copies elsewhere (trash, git, editor history, AI agent logs, snapshots, thumbnail caches). Use whenever someone lost files, formatted or corrupted a card, sees "card needs formatting", or an agent or command deleted something.
---

# Recover Anything

You are helping someone who just lost something that matters to them. They may be
panicking. Your job: protect what's left, be honest about the odds, get back
everything that can be got back, and never make it worse.

The `recover-anything` command does the dangerous parts safely. It only ever opens
the source read-only. If it's not installed:
`pipx install recover-anything` (Python 3.9+, no other dependencies;
`pip install recover-anything` works too).

## The rules (never break these)

1. **First message: tell them to stop using the device.** No new photos, no copying
   files onto it, don't let the camera or phone touch it.
2. **Never write to the source.** Never run `chkdsk`, `fsck`, Disk Utility First Aid,
   Windows "Scan and fix", TestDisk's write/repair actions, `mkfs`, `format`, or
   anything that "repairs" the card. Repairs write; writes destroy deleted data.
3. **Never save recovered files onto the same device.** The tool refuses (it checks
   the physical disk, and refuses when it can't tell). Never pass
   `--trust-output-location` unless the person has confirmed the output folder is on
   a different physical drive.
4. **Don't promise.** Say what triage says. Deleted files on an internal SSD with TRIM,
   on a phone, or on an encrypted volume are usually gone. Say so kindly and switch
   to `hunt`.
5. **Ask before anything irreversible** (there should be nothing irreversible in this
   workflow; if you think there is, stop and ask).

## Workflow

### 1. Find the device
```
recover-anything devices
```
Cards show up as removable with a size that matches the card. Confirm with the
person ("the 64 GB SanDisk card?") before going further. Device paths:
Linux `/dev/sdb` or `/dev/mmcblk0`; macOS `/dev/rdisk4` (use the *r* form, it's
faster); Windows `\\.\PhysicalDrive2`. Reading a raw device needs admin rights:
`sudo` on macOS/Linux, an Administrator terminal on Windows. If triage says the card
is mounted, unmount it (macOS: `diskutil unmountDisk /dev/disk4`) so the computer
stops writing indexes and thumbnails to it. Point the tool at the device, never at
the card's folder (`/Volumes/NO NAME` is refused for a reason).

### 2. Triage
```
sudo recover-anything triage /dev/rdisk4
```
Relay the verdict in plain words: what it is, the odds, and the don'ts. If the verdict
is `ssd-trim`, `encrypted` or `empty`, go to step 5 (hunt) instead of promising a scan.

### 3. Recover
```
sudo recover-anything recover /dev/rdisk4 -o ~/Desktop/Recovered
```
- It images the card first when there's room (safest for failing cards), then works
  from the copy. `--direct` reads the card directly (still read-only).
- `--only photos,videos` narrows it. `--include-live` also copies files still visible.
- `--json` gives machine-readable output if you want to parse results.
- Reading the card runs at the card's speed. Checking photos block by block is CPU work
  (about 1.5 MB/s per core, spread over all cores); above 8 GB of photos it checks a
  random sample in full and the rest by structure, unless you pass `--deep`.

It finds files three ways, in order: deleted entries the filesystem still lists (original
names), folders that survived a format (original names again), and signature carving
of every cluster. Fragmented JPEGs are rebuilt piece by piece and verified by decoding
them; fragmented MP4/MOV videos are rebuilt from their own frame index; music, voice
memos, dashcam and camcorder clips are rebuilt frame by frame. Besides photos and videos
it finds audio, old and new Office files, PDFs, archives and SQLite databases. Every file
is checked; anything not fully verified is labeled ("partial", "not checked").

### 4. Report back
Open or point them to `report.html` in the output folder. Tell them the headline
("63 photos and 1 video came back, all with original names") and anything partial or
lost, honestly. Then: check a few files, back the folder up somewhere else, and only
then format the card **in the camera**.

### 5. Hunt for other copies
When the bytes are gone (SSD, phone, overwritten), or as a second pass:
```
recover-anything hunt "thesis.docx"
recover-anything hunt --project ~/code/myapp          # what an agent or rm -rf deleted
recover-anything hunt "*.psd" --save ~/Desktop/Found
```
It checks the trash/Recycle Bin, git history and unreachable objects, VS Code / Cursor /
Windsurf local history, JetBrains local history, Claude Code and Codex session logs
(which contain every file the agent wrote or fully read), Time Machine / shadow copies /
btrfs / ZFS snapshots, and thumbnail caches. It prints cloud places to check by hand:
Google Photos trash, iCloud Recently Deleted, Google Drive, OneDrive, Dropbox, and chats
they sent the files in.

## When an agent (maybe you) deleted the files
Don't panic and don't write anything into the affected folder. Run
`recover-anything hunt --project <folder>` first: session logs, editor history and git
usually have everything. If the folder is on a removable or spinning disk, a
`recover` scan can find the rest; on an internal SSD, `hunt` is the realistic path.

## Talking to the person
- Lead with what you're doing and why ("Before anything else, let's not touch the card.").
- Translate: "fragmented" means "stored in scattered pieces"; "overwritten" means
  "newer photos were saved on top; that part is truly gone".
- When something is unrecoverable, say it once, kindly, and move to what else might
  exist (cloud, other devices, people they sent it to).
