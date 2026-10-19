# diskmap

Interactive treemap of what's using space on your Mac, plus a junk finder. Read-only: it never deletes anything.

```sh
./diskmap.py              # scan your home folder
./diskmap.py / --open     # scan the whole disk and open the report
open report/index.html    # view; rerun the scan and reload to refresh
```

No dependencies beyond the system `python3`. d3 is vendored in `report/`, so the page works offline.

## The report

- **Map**: click a block to zoom in, Esc/Backspace or the breadcrumb to go up. Your zoom position is kept in the URL, so reloading after a rescan keeps your place. Color the map by folder, by file type, or junk only. Junk folders get a dashed red outline.
- **Junk**: caches, logs, Trash, Xcode DerivedData/simulators/device support, iOS backups, Docker, package-manager caches, `node_modules`, virtualenvs, build caches, `.DS_Store`, installers left in Downloads, and large files untouched for 6+ months. Each item is tagged *safe* or *review* and comes with a clean-up command you can copy.
- **Changes**: what grew, shrank, appeared or disappeared since the previous scan of the same folder, plus trend charts of free space and folder size across scans. The map's "by change" color mode shows growth in red and shrinkage in blue.
- **Disk**: where all the used space goes, including what macOS calls "System Data". It splits the disk into this scan, the other big places on the Data volume (`/Applications`, `/Library`, `/private/var`, other users), whatever is left over, the macOS system volumes, swap, purgeable space and local Time Machine snapshots.
- **Largest**: the biggest files and folders.
- **Duplicates**: identical files ≥ 20 MB, matched by hash.
- **Types**: space by file type (video, images, disk images…).
- **Scan**: stats, Time Machine local snapshots, and the folders that couldn't be read.

## Options

| flag | |
|---|---|
| `--min-size 2M` | smallest item drawn on its own; smaller items are grouped |
| `--skip PATH` | skip a folder (repeatable) |
| `--one-fs` | don't cross into other volumes |
| `--no-dupes` / `--dupe-min 20M` | duplicate detection settings |
| `--open` | open the report when done |
| `--compare latest\|oldest\|DATE` | which earlier scan to compare with, e.g. `--compare 2026-10-01` |
| `--keep 30` | scans kept per folder in `report/history/` |
| `--no-history` | don't save or compare this scan |
| `--no-system` | skip sizing `/Applications`, `/Library` etc. for the Disk tab |
| `--jobs N` | worker processes (default: your Mac's performance-core count; `1` = single process) |

## Notes

- Speed: folders are listed with macOS's `getattrlistbulk` (one system call per batch of entries instead of one `stat` per file), and the tree is split across worker processes. The scan prints a per-phase timing line at the end. More workers than performance cores makes it *slower*, because they contend for filesystem locks.
- Each scan saves a compressed list of folder and file sizes (items above `--min-size`) in `report/history/`. Scans are compared only with earlier scans of the same folder.
- Sizes are the space actually allocated on disk (`st_blocks`), so iCloud files that are only in the cloud count as 0. Hard links are counted once. APFS clones can't be detected, so some "duplicates" may share blocks.
- macOS hides some folders (Mail, Messages, Safari…) unless your terminal has **Full Disk Access** (System Settings → Privacy & Security). The Scan tab lists what was skipped.
