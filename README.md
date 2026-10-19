# diskmap

Interactive treemap of what's using space on your Mac, plus a junk finder. Read-only: it never deletes anything.

```sh
./diskmap.py              # scan your home folder (~1–3 min)
./diskmap.py / --open     # scan the whole disk and open the report
open report/index.html    # view; rerun the scan and reload to refresh
```

No dependencies beyond the system `python3`. d3 is vendored in `report/`, so the page works offline.

## The report

- **Map**: click a block to zoom in, Esc/Backspace or the breadcrumb to go up. Your zoom position is kept in the URL, so reloading after a rescan keeps your place. Color the map by folder, by file type, or junk only. Junk folders get a dashed red outline.
- **Junk**: caches, logs, Trash, Xcode DerivedData/simulators/device support, iOS backups, Docker, package-manager caches, `node_modules`, virtualenvs, build caches, `.DS_Store`, installers left in Downloads, and large files untouched for 6+ months. Each item is tagged *safe* or *review* and comes with a clean-up command you can copy.
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

## Notes

- Sizes are the space actually allocated on disk (`st_blocks`), so iCloud files that are only in the cloud count as 0. Hard links are counted once. APFS clones can't be detected, so some "duplicates" may share blocks.
- macOS hides some folders (Mail, Messages, Safari…) unless your terminal has **Full Disk Access** (System Settings → Privacy & Security). The Scan tab lists what was skipped.
