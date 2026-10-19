#!/usr/bin/env python3
"""
diskmap: scan a folder tree and write an interactive treemap report.

    ./diskmap.py                  # scan your home folder
    ./diskmap.py / --open         # scan the whole Mac, then open the report
    ./diskmap.py ~/git --min-size 100K

Read-only: it never deletes anything. The clean-up commands shown in the
report are suggestions for you to run yourself.
"""
import argparse
import hashlib
import heapq
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections import defaultdict

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- junk rules
# safety: "safe"   = regenerated automatically, fine to delete
#         "review" = probably junk, but look before deleting
#         "info"   = managed by macOS, shown so you know where space went
RULES = {
    "trash": ("Trash", "safe",
              "Files already moved to the Trash.",
              "Finder → Empty Trash"),
    "caches": ("User caches", "safe",
               "Per-app caches. Apps rebuild them as needed; quit apps first.",
               "rm -rf ~/Library/Caches/*"),
    "app-caches": ("App caches (Electron / Chromium / sandboxed apps)", "safe",
                   "Browser-engine caches inside Application Support and app containers "
                   "(Slack, VS Code, Teams, Discord, …). Quit the app before deleting.",
                   "Quit the app, then delete the listed folder"),
    "logs": ("Logs", "safe",
             "Application and system logs.",
             "rm -rf ~/Library/Logs/*"),
    "xcode-derived": ("Xcode DerivedData", "safe",
                      "Build products and indexes; Xcode rebuilds them.",
                      "rm -rf ~/Library/Developer/Xcode/DerivedData"),
    "xcode-archives": ("Xcode Archives", "review",
                       "Archived app builds. Keep the ones you need for crash symbolication.",
                       "Xcode → Organizer → Archives"),
    "xcode-devicesupport": ("Xcode device support", "safe",
                            "Debug symbols for every iOS/watchOS version you've connected; re-downloaded on demand.",
                            "rm -rf ~/Library/Developer/Xcode/*DeviceSupport"),
    "simulators": ("iOS Simulators", "review",
                   "Simulator devices and runtimes.",
                   "xcrun simctl delete unavailable"),
    "sim-caches": ("Simulator caches", "safe",
                   "CoreSimulator caches.",
                   "rm -rf ~/Library/Developer/CoreSimulator/Caches"),
    "ios-backups": ("iPhone / iPad backups", "review",
                    "Local device backups. Delete old ones if you use iCloud backup.",
                    "Finder → your device → Manage Backups"),
    "docker": ("Docker data", "review",
               "Docker's VM disk (images, containers, volumes, build cache).",
               "docker system prune -a --volumes"),
    "pkg-caches": ("Package manager caches", "safe",
                   "Download caches for npm, pnpm, yarn, pip, Homebrew, Go, Cargo, Gradle, Maven, CocoaPods…",
                   "npm cache clean --force · pnpm store prune · brew cleanup --prune=all · go clean -modcache · pip cache purge"),
    "node_modules": ("node_modules", "review",
                     "Installed JS dependencies. Safe to delete in projects you aren't working on; `npm install` restores them.",
                     "npx npkill   (interactive finder/deleter)"),
    "venv": ("Python virtualenvs", "review",
             "Virtual environments (contain pyvenv.cfg). Recreate from requirements files.",
             "rm -rf <project>/.venv"),
    "build-cache": ("Build output & tool caches", "safe",
                    ".next, .turbo, .gradle, .terraform, .tox, Rust target/ dirs and folders tagged with CACHEDIR.TAG.",
                    "Delete the folder; the next build recreates it"),
    "pycache": ("Python bytecode caches", "safe",
                "__pycache__, .pytest_cache, .mypy_cache, .ruff_cache.",
                "find . -name __pycache__ -type d -prune -exec rm -rf {} +"),
    "ds_store": (".DS_Store files", "safe",
                 "Finder view metadata. Tiny each, but there are usually thousands.",
                 "find ~ -name .DS_Store -delete"),
    "downloads-installers": ("Installers & archives in Downloads", "review",
                             ".dmg / .pkg / .zip / .iso files left in Downloads after installing.",
                             "Delete the ones you've already installed/extracted"),
    "ipsw": ("iOS firmware files", "safe",
             ".ipsw restore images; re-downloaded when needed.",
             "Delete the listed files"),
    "big-logs": ("Large log files", "review",
                 "Single .log files over 50 MB.",
                 "Truncate or delete"),
    "stale-large": ("Large files untouched for 6+ months", "review",
                    "Files over 500 MB not modified in 180 days. Candidates for archiving to external storage.",
                    "Move to an external drive or delete"),
    "sys-caches": ("System caches", "review",
                   "Shared caches under /Library/Caches.",
                   "sudo rm -rf /Library/Caches/*   (careful)"),
    "tmp": ("Per-user temp & caches (/var/folders)", "info",
            "macOS-managed temp space; cleared on reboot / automatically. Don't delete by hand.",
            "Restart your Mac"),
    "updates": ("Downloaded macOS updates", "review",
                "Installer payloads for pending macOS updates.",
                "Install or cancel the update in System Settings"),
    "vm": ("Swap & sleep image", "info",
           "Virtual memory files managed by macOS.",
           "Close memory-hungry apps / restart"),
}

EXACT_DIRS = {
    "~/.Trash": "trash",
    "~/Library/Caches": "caches",
    "~/Library/Logs": "logs",
    "~/Library/Developer/Xcode/DerivedData": "xcode-derived",
    "~/Library/Developer/Xcode/Archives": "xcode-archives",
    "~/Library/Developer/Xcode/iOS DeviceSupport": "xcode-devicesupport",
    "~/Library/Developer/Xcode/watchOS DeviceSupport": "xcode-devicesupport",
    "~/Library/Developer/Xcode/macOS DeviceSupport": "xcode-devicesupport",
    "~/Library/Developer/CoreSimulator/Devices": "simulators",
    "~/Library/Developer/CoreSimulator/Caches": "sim-caches",
    "~/Library/Application Support/MobileSync/Backup": "ios-backups",
    "~/Library/Containers/com.docker.docker": "docker",
    "~/.docker/desktop": "docker",
    "~/.npm": "pkg-caches",
    "~/.pnpm-store": "pkg-caches",
    "~/Library/pnpm/store": "pkg-caches",
    "~/.yarn/berry/cache": "pkg-caches",
    "~/.cache": "pkg-caches",
    "~/.gradle/caches": "pkg-caches",
    "~/.m2/repository": "pkg-caches",
    "~/.cargo/registry": "pkg-caches",
    "~/go/pkg/mod": "pkg-caches",
    "~/.cocoapods/repos": "pkg-caches",
    "~/.bun/install/cache": "pkg-caches",
    "~/.nuget/packages": "pkg-caches",
    "/Library/Caches": "sys-caches",
    "/Library/Logs": "logs",
    "/private/var/log": "logs",
    "/private/var/folders": "tmp",
    "/Library/Updates": "updates",
    "/private/var/vm": "vm",
}
EXACT_DIRS = {os.path.expanduser(k): v for k, v in EXACT_DIRS.items()}

NAME_DIRS = {
    "node_modules": "node_modules",
    "__pycache__": "pycache", ".pytest_cache": "pycache",
    ".mypy_cache": "pycache", ".ruff_cache": "pycache",
    ".next": "build-cache", ".nuxt": "build-cache", ".turbo": "build-cache",
    ".parcel-cache": "build-cache", ".gradle": "build-cache",
    ".terraform": "build-cache", ".tox": "build-cache",
    "DerivedData": "xcode-derived",
}
# Chromium/Electron cache folder names, matched only inside app support dirs
APP_CACHE_NAMES = {
    "Cache", "Caches", "CachedData", "Code Cache", "GPUCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "GrShaderCache",
    "CacheStorage", "CachedExtensionVSIXs", "Crashpad",
}
APP_CACHE_PARENTS = ("/Library/Application Support/", "/Library/Containers/",
                     "/Library/Group Containers/")
INSTALLER_EXT = {".dmg", ".pkg", ".iso", ".zip", ".xip", ".tar", ".gz", ".tgz", ".7z", ".rar"}
DOWNLOADS = os.path.join(HOME, "Downloads")
STALE_MIN = 500e6
STALE_AGE = 180 * 86400

# ------------------------------------------------------------ file categories
CATEGORIES = {
    "video": "mp4 mov m4v mkv avi webm wmv flv mpg mpeg mts m2ts prproj fcpbundle braw r3d",
    "image": "jpg jpeg png gif heic heif tif tiff bmp webp raw cr2 cr3 nef arw dng psd ai svg ico icns",
    "audio": "mp3 m4a aac wav aif aiff flac ogg opus caf logicx band",
    "archive": "zip gz tgz bz2 xz 7z rar tar zst lz4 xip jar war",
    "disk": "dmg iso img sparseimage sparsebundle vmdk vdi qcow2 vhd vhdx raw ipsw pkg asar",
    "code": "js mjs cjs ts tsx jsx py rb go rs java kt swift c h cc cpp hpp m mm cs php sh css scss html vue map wasm",
    "doc": "pdf doc docx xls xlsx ppt pptx pages numbers key txt md rtf epub csv",
    "data": "db sqlite sqlite3 db-wal sqlite-wal json xml parquet ldb log plist realm",
    "binary": "dylib so a o dll exe bin node framework",
}
EXT_CAT = {f".{e}": cat for cat, exts in CATEGORIES.items() for e in exts.split()}


def category(name):
    ext = os.path.splitext(name)[1].lower()
    return EXT_CAT.get(ext, "other")


def parse_size(text):
    text = text.strip().upper().rstrip("B")
    mult = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    if text and text[-1] in mult:
        return int(float(text[:-1]) * mult[text[-1]])
    return int(text)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000


# --------------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, root, min_size, skip, one_fs, dupe_min):
        self.root = root
        self.min_size = min_size
        self.skip = skip
        self.one_fs = one_fs
        self.dupe_min = dupe_min
        self.dev = os.stat(root).st_dev
        self.now = time.time()
        self.inodes = set()
        self.findings = defaultdict(list)          # rule -> [(size, path)]
        self.types = defaultdict(lambda: [0, 0])   # category -> [bytes, count]
        self.largest = []                          # min-heap of (size, path)
        self.by_size = defaultdict(list)           # logical size -> [paths]
        self.denied = []
        self.files = self.dirs = self.errors = 0
        self.bytes = 0
        self._last_print = 0

    # -- rules
    def dir_rule(self, path, name, names):
        rule = EXACT_DIRS.get(path) or NAME_DIRS.get(name)
        if rule:
            return rule
        if name in APP_CACHE_NAMES and any(p in path for p in APP_CACHE_PARENTS):
            return "app-caches"
        if "CACHEDIR.TAG" in names:
            return "build-cache"
        if "pyvenv.cfg" in names:
            return "venv"
        return None

    def file_rule(self, path, name, size, st):
        if name == ".DS_Store":
            return "ds_store"
        ext = os.path.splitext(name)[1].lower()
        if ext == ".ipsw":
            return "ipsw"
        if ext in INSTALLER_EXT and path.startswith(DOWNLOADS + "/"):
            return "downloads-installers"
        if ext == ".log" and size >= 50e6:
            return "big-logs"
        if size >= STALE_MIN and self.now - st.st_mtime > STALE_AGE:
            return "stale-large"
        return None

    # -- walking
    def progress(self, path):
        now = time.time()
        if now - self._last_print < 0.25:
            return
        self._last_print = now
        shown = path.replace(HOME, "~", 1)
        if len(shown) > 60:
            shown = "…" + shown[-59:]
        sys.stderr.write(f"\r\033[K  {self.files:>10,} files  {human(self.bytes):>10}  {shown}")
        sys.stderr.flush()

    def walk(self, path, inherited):
        """Return (allocated_bytes, node) for a directory."""
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except PermissionError:
            self.denied.append(path)
            return 0, None
        except OSError:
            self.errors += 1
            return 0, None

        self.dirs += 1
        self.progress(path)
        name = os.path.basename(path) or path
        rule = None if inherited else self.dir_rule(path, name, {e.name for e in entries})
        junk = inherited or rule

        total = small_size = small_count = 0
        kids = []
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                self.errors += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                if e.path in self.skip or (self.one_fs and st.st_dev != self.dev):
                    continue
                size, child = self.walk(e.path, junk)
            elif stat.S_ISREG(st.st_mode):
                size, cat = self.file(e, st, junk)
                child = {"n": e.name, "s": size, "t": cat}
            else:
                continue  # symlinks, sockets, devices
            total += size
            if child is not None and size >= self.min_size:
                kids.append(child)
            elif size:
                small_size += size
                small_count += 1

        node = {"n": name, "s": total, "d": 1}
        if rule:
            node["j"] = rule
            self.findings[rule].append((total, path))
        if kids:
            kids.sort(key=lambda k: -k["s"])
            if small_size:
                kids.append({"n": f"{small_count:,} smaller items", "s": small_size, "o": 1})
            node["c"] = kids
        return total, node

    def file(self, entry, st, junk):
        if st.st_nlink > 1:  # count hard-linked files once
            key = (st.st_dev, st.st_ino)
            if key in self.inodes:
                return 0, "other"
            self.inodes.add(key)
        size = st.st_blocks * 512  # space actually allocated (0 for iCloud-evicted files)
        self.files += 1
        self.bytes += size
        cat = category(entry.name)
        t = self.types[cat]
        t[0] += size
        t[1] += 1
        item = (size, entry.path)
        if len(self.largest) < 200:
            heapq.heappush(self.largest, item)
        elif size > self.largest[0][0]:
            heapq.heapreplace(self.largest, item)
        if size >= self.dupe_min:
            self.by_size[st.st_size].append(entry.path)
        if not junk:
            rule = self.file_rule(entry.path, entry.name, size, st)
            if rule:
                self.findings[rule].append((size, entry.path))
        return size, cat

    def run(self):
        t0 = time.time()
        total, tree = self.walk(self.root, None)
        sys.stderr.write("\r\033[K")
        if tree is None:
            sys.exit(f"Cannot read {self.root}")
        tree["n"] = self.root
        self.duration = time.time() - t0
        return tree


# ------------------------------------------------------------------ duplicates
def _hash(path, size, sampled):
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            if not sampled:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            else:
                for i in range(16):
                    f.seek(int((size - (1 << 20)) * i / 15))
                    h.update(f.read(1 << 20))
    except OSError:
        return None
    return h.hexdigest()


def find_dupes(by_size):
    """Group same-size files by a quick hash, then a full (or sampled, if huge) hash."""
    groups = []
    candidates = [(s, ps) for s, ps in by_size.items() if len(ps) > 1]
    for i, (size, paths) in enumerate(sorted(candidates, reverse=True)):
        sys.stderr.write(f"\r\033[K  checking duplicates {i + 1}/{len(candidates)}")
        quick = defaultdict(list)
        for p in paths:
            q = _hash(p, size, sampled=True) if size > (16 << 20) else _hash(p, size, False)
            if q:
                quick[q].append(p)
        sampled = size > 2e9  # don't read multi-GB files in full
        for same in quick.values():
            if len(same) < 2:
                continue
            if sampled or size <= (16 << 20):
                full = {"x": same}
            else:
                full = defaultdict(list)
                for p in same:
                    h = _hash(p, size, False)
                    if h:
                        full[h].append(p)
            for ps in full.values():
                if len(ps) > 1:
                    groups.append({"size": size, "paths": sorted(ps), "sampled": sampled})
    sys.stderr.write("\r\033[K")
    groups.sort(key=lambda g: -g["size"] * (len(g["paths"]) - 1))
    return groups[:300]


# ------------------------------------------------------------------- disk info
def disk_info():
    du = shutil.disk_usage("/")
    info = {"total": du.total, "used": du.used, "free": du.free, "snapshots": None}
    try:
        out = subprocess.run(["tmutil", "listlocalsnapshots", "/"], capture_output=True,
                             text=True, timeout=10).stdout
        info["snapshots"] = sum(1 for line in out.splitlines() if "com.apple" in line)
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Map disk usage and find junk. Read-only.")
    ap.add_argument("root", nargs="?", default=HOME, help="folder to scan (default: your home)")
    ap.add_argument("--min-size", default="2M",
                    help="smallest item drawn on the map; smaller ones are grouped (default 2M)")
    ap.add_argument("--skip", action="append", default=[], help="path to skip (repeatable)")
    ap.add_argument("--one-fs", action="store_true", help="don't cross into other volumes")
    ap.add_argument("--no-dupes", action="store_true", help="skip duplicate-file detection")
    ap.add_argument("--dupe-min", default="20M", help="smallest file checked for duplicates")
    ap.add_argument("--out", default=os.path.join(HERE, "report"), help="report folder")
    ap.add_argument("--open", action="store_true", help="open the report when done")
    args = ap.parse_args()

    root = os.path.realpath(os.path.expanduser(args.root))
    skip = {os.path.realpath(os.path.expanduser(p)) for p in args.skip}
    # Never descend into mounted volumes or the APFS data-volume mirror of /
    skip |= {"/System/Volumes", "/Volumes", "/dev", "/net", "/home"}
    skip.discard(root)

    print(f"Scanning {root} …", file=sys.stderr)
    sc = Scanner(root, parse_size(args.min_size), skip, args.one_fs, parse_size(args.dupe_min))
    tree = sc.run()
    print(f"  {sc.files:,} files, {sc.dirs:,} folders, {human(tree['s'])} in {sc.duration:.0f}s",
          file=sys.stderr)

    dupes = [] if args.no_dupes else find_dupes(sc.by_size)

    findings = {}
    for rule, items in sc.findings.items():
        items.sort(reverse=True)
        findings[rule] = {
            "total": sum(s for s, _ in items),
            "count": len(items),
            "items": [[s, p] for s, p in items[:200]],
        }

    data = {
        "root": root,
        "home": HOME,
        "scanned": time.strftime("%Y-%m-%d %H:%M"),
        "stats": {"files": sc.files, "dirs": sc.dirs, "errors": sc.errors,
                  "seconds": round(sc.duration, 1), "minSize": sc.min_size},
        "disk": disk_info(),
        "rules": {k: {"label": a, "safety": b, "desc": c, "hint": d}
                  for k, (a, b, c, d) in RULES.items()},
        "findings": findings,
        "types": dict(sc.types),
        "largest": sorted(sc.largest, reverse=True),
        "dupes": dupes,
        "denied": sc.denied[:500],
        "deniedCount": len(sc.denied),
        "tree": tree,
    }
    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "data.js")
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        f.write("window.DISKMAP = ")
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        f.write(";\n")
    os.replace(out + ".tmp", out)

    junk_safe = sum(f["total"] for r, f in findings.items() if RULES[r][1] == "safe")
    print(f"  safe-to-clear junk: {human(junk_safe)}", file=sys.stderr)
    if sc.denied:
        print(f"  {len(sc.denied)} folders were not readable (grant Full Disk Access to your "
              "terminal to include them)", file=sys.stderr)
    index = os.path.join(args.out, "index.html")
    print(f"Report: {index}", file=sys.stderr)
    if args.open:
        subprocess.run(["open", index])


if __name__ == "__main__":
    sys.setrecursionlimit(20000)
    main()
