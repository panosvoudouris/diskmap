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
import ctypes
import errno
import gzip
import hashlib
import heapq
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from types import SimpleNamespace

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


# ----------------------------------------------------------- directory listing
# getattrlistbulk(2) returns names, types and sizes for a whole directory in a few
# system calls instead of one stat() per entry. Falls back to scandir+stat on
# volumes that don't support it.
class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.getattrlistbulk.argtypes = [ctypes.c_int, ctypes.POINTER(_AttrList), ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.c_uint64]
_libc.getattrlistbulk.restype = ctypes.c_int
_CMN_NAME, _CMN_DEVID, _CMN_OBJTYPE, _CMN_MODTIME = 0x1, 0x2, 0x8, 0x400
_CMN_FILEID, _CMN_ERROR, _CMN_RETURNED = 0x02000000, 0x20000000, 0x80000000
_FILE_LINKCOUNT, _FILE_TOTALSIZE, _FILE_ALLOCSIZE = 0x1, 0x2, 0x4
_ATTRS = _AttrList(5, 0, _CMN_RETURNED | _CMN_NAME | _CMN_DEVID | _CMN_OBJTYPE | _CMN_MODTIME
                   | _CMN_FILEID | _CMN_ERROR, 0, 0,
                   _FILE_LINKCOUNT | _FILE_TOTALSIZE | _FILE_ALLOCSIZE, 0)
_BUF_SIZE = 256 * 1024
_buf = None
VNON, VREG, VDIR = 0, 1, 2  # fsobj_type_t values; anything else is skipped


def _listdir_bulk(path):
    global _buf
    if _buf is None:
        _buf = ctypes.create_string_buffer(_BUF_SIZE)
    unpack = struct.unpack_from
    out = []
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        while True:
            n = _libc.getattrlistbulk(fd, ctypes.byref(_ATTRS), _buf, _BUF_SIZE, 0)
            if n < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), path)
            if n == 0:
                return out
            raw = _buf.raw
            off = 0
            for _ in range(n):
                length, cmn, _v, _d, fil, _f = unpack("I5I", raw, off)
                p = off + 24
                name, dev, typ, mtime, ino, err, nlink, size, alloc = None, 0, VNON, 0.0, 0, 0, 1, 0, 0
                # attributes are packed in bit order, each 4-byte aligned
                if cmn & _CMN_NAME:
                    name_off, name_len = unpack("iI", raw, p)
                    name = raw[p + name_off:p + name_off + name_len - 1].decode("utf-8", "surrogateescape")
                    p += 8
                if cmn & _CMN_DEVID:
                    dev, = unpack("i", raw, p)
                    p += 4
                if cmn & _CMN_OBJTYPE:
                    typ, = unpack("I", raw, p)
                    p += 4
                if cmn & _CMN_MODTIME:
                    sec, nsec = unpack("qq", raw, p)
                    mtime = sec + nsec / 1e9
                    p += 16
                if cmn & _CMN_FILEID:
                    ino, = unpack("Q", raw, p)
                    p += 8
                if cmn & _CMN_ERROR:
                    err, = unpack("I", raw, p)
                    p += 4
                if fil & _FILE_LINKCOUNT:
                    nlink, = unpack("I", raw, p)
                    p += 4
                if fil & _FILE_TOTALSIZE:
                    size, = unpack("q", raw, p)
                    p += 8
                if fil & _FILE_ALLOCSIZE:
                    alloc, = unpack("q", raw, p)
                off += length
                out.append((name or "?", VNON if err else typ, dev, mtime, ino, nlink, size, alloc))
    finally:
        os.close(fd)


def _listdir_stat(path):
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                out.append((e.name, VNON, 0, 0.0, 0, 1, 0, 0))
                continue
            typ = VREG if stat.S_ISREG(st.st_mode) else VDIR if stat.S_ISDIR(st.st_mode) else -1
            out.append((e.name, typ, st.st_dev, st.st_mtime, st.st_ino, st.st_nlink,
                        st.st_size, st.st_blocks * 512))
    return out


def listdir(path):
    """[(name, type, dev, mtime, inode, nlink, logical_size, allocated_bytes)] for a directory."""
    try:
        return _listdir_bulk(path)
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS):
            raise
    return _listdir_stat(path)


# ---------------------------------------------------------------- worker pool
# Folders SPLIT_DEPTH levels below the scan root are scanned by worker processes.
SPLIT_DEPTH = 3
_OPTS = None


def _init_worker(opts):
    global _OPTS
    _OPTS = opts


def _scan_task(path, inherited):
    sc = Scanner(_OPTS, quiet=True)
    size, node = sc.walk(path, inherited)
    return sc.state(size, node)


def _du_task(path, skip, dev):
    return du(path, skip, dev)


def _dupe_task(size, paths):
    return dupe_groups(size, paths)


class Inline:
    """Stands in for a process pool when running with --jobs 1."""
    def submit(self, fn, *args):
        f = Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, **_):
        pass


class Deferred:
    """A folder near the root whose children are still being scanned by workers."""
    __slots__ = ("name", "path", "rule", "kids")

    def __init__(self, name, path, rule, kids):
        self.name, self.path, self.rule, self.kids = name, path, rule, kids


# --------------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, opts, pool=None, quiet=False):
        self.o = opts
        self.pool = pool                           # set only in the coordinating process
        self.quiet = quiet
        self.data_bytes = 0                        # part of the scan stored on the Data volume
        self.inodes = set()
        self.links = []                            # first sighting of each hard-linked file
        self.findings = defaultdict(list)          # rule -> [(size, path)]
        self.types = defaultdict(lambda: [0, 0])   # category -> [bytes, count]
        self.largest = []                          # min-heap of (size, path)
        self.by_size = defaultdict(list)           # logical size -> [paths]
        self.denied = []
        self.files = self.dirs = self.errors = 0
        self.bytes = 0
        self.futures = []
        self.results = {}
        self.worker_links = []
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

    def file_rule(self, path, name, size, mtime):
        if name == ".DS_Store":
            return "ds_store"
        ext = os.path.splitext(name)[1].lower()
        if ext == ".ipsw":
            return "ipsw"
        if ext in INSTALLER_EXT and path.startswith(DOWNLOADS + "/"):
            return "downloads-installers"
        if ext == ".log" and size >= 50e6:
            return "big-logs"
        if size >= STALE_MIN and self.o.now - mtime > STALE_AGE:
            return "stale-large"
        return None

    # -- walking
    def progress(self, path):
        now = time.time()
        if self.quiet or now - self._last_print < 0.25:
            return
        self._last_print = now
        shown = path.replace(HOME, "~", 1)
        if len(shown) > 60:
            shown = "…" + shown[-59:]
        sys.stderr.write(f"\r\033[K  {self.files:>10,} files  {human(self.bytes):>10}  {shown}")
        sys.stderr.flush()

    def walk(self, path, inherited, depth=0):
        """(allocated_bytes, node) for a directory, or a Deferred when workers are involved."""
        try:
            entries = listdir(path)
        except PermissionError:
            self.denied.append(path)
            return 0, None
        except OSError:
            self.errors += 1
            return 0, None

        self.dirs += 1
        self.progress(path)
        name = os.path.basename(path) or path
        rule = None if inherited else self.dir_rule(path, name, {e[0] for e in entries})
        junk = inherited or rule
        o = self.o

        kids = []
        for ename, typ, dev, mtime, ino, nlink, size, alloc in entries:
            p = os.path.join(path, ename)
            if typ == VDIR:
                if p in o.skip or (o.one_fs and dev != o.dev):
                    continue
                if self.pool and depth + 1 >= SPLIT_DEPTH:
                    fut = self.pool.submit(_scan_task, p, junk)
                    self.futures.append(fut)
                    kids.append(fut)
                else:
                    kids.append(self.walk(p, junk, depth + 1))
            elif typ == VREG:
                s, cat = self.file(p, ename, dev, mtime, ino, nlink, size, alloc, junk)
                kids.append((s, {"n": ename, "s": s, "t": cat}))
            elif typ == VNON:
                self.errors += 1
            # symlinks, sockets, devices are skipped
        if self.pool:
            return Deferred(name, path, rule, kids)
        return self.finish(name, path, rule, kids)

    def finish(self, name, path, rule, kids):
        total = small_size = small_count = 0
        keep = []
        for size, child in kids:
            total += size
            if child is not None and size >= self.o.min_size:
                keep.append(child)
            elif size:
                small_size += size
                small_count += 1
        node = {"n": name, "s": total, "d": 1}
        if rule:
            node["j"] = rule
            self.findings[rule].append((total, path))
        if keep:
            keep.sort(key=lambda k: -k["s"])
            if small_size:
                keep.append({"n": f"{small_count:,} smaller items", "s": small_size, "o": 1})
            node["c"] = keep
        return total, node

    def file(self, path, name, dev, mtime, ino, nlink, size, alloc, junk):
        if nlink > 1:  # count hard-linked files once
            key = (dev, ino)
            if key in self.inodes:
                return 0, "other"
            self.inodes.add(key)
            self.links.append((key, alloc, path, dev))
        self.files += 1
        self.bytes += alloc  # space actually allocated (0 for iCloud-evicted files)
        if dev == self.o.data_dev:
            self.data_bytes += alloc
        cat = category(name)
        t = self.types[cat]
        t[0] += alloc
        t[1] += 1
        item = (alloc, path)
        if len(self.largest) < 200:
            heapq.heappush(self.largest, item)
        elif alloc > self.largest[0][0]:
            heapq.heapreplace(self.largest, item)
        if alloc >= self.o.dupe_min:
            self.by_size[size].append(path)
        if not junk:
            rule = self.file_rule(path, name, alloc, mtime)
            if rule:
                self.findings[rule].append((alloc, path))
        return alloc, cat

    # -- combining worker results
    def state(self, size, node):
        return {"size": size, "node": node, "files": self.files, "dirs": self.dirs,
                "errors": self.errors, "bytes": self.bytes, "data_bytes": self.data_bytes,
                "findings": dict(self.findings), "types": dict(self.types),
                "largest": self.largest, "by_size": dict(self.by_size),
                "denied": self.denied, "links": self.links}

    def merge(self, r):
        for k in ("files", "dirs", "errors", "bytes", "data_bytes"):
            setattr(self, k, getattr(self, k) + r[k])
        for rule, items in r["findings"].items():
            self.findings[rule].extend(items)
        for cat, (b, c) in r["types"].items():
            self.types[cat][0] += b
            self.types[cat][1] += c
        for item in r["largest"]:
            if len(self.largest) < 200:
                heapq.heappush(self.largest, item)
            elif item[0] > self.largest[0][0]:
                heapq.heapreplace(self.largest, item)
        for size, paths in r["by_size"].items():
            self.by_size[size].extend(paths)
        self.denied.extend(r["denied"])
        self.worker_links.append(r["links"])

    def resolve(self, item):
        if isinstance(item, Future):
            return self.results[item]
        if isinstance(item, Deferred):
            return self.finish(item.name, item.path, item.rule, [self.resolve(k) for k in item.kids])
        return item

    def uncount(self, tree, path, size, dev):
        """Remove a hard-linked file that two workers both counted."""
        self.files -= 1
        self.bytes -= size
        if dev == self.o.data_dev:
            self.data_bytes -= size
        t = self.types[category(path)]
        t[0] -= size
        t[1] -= 1
        chain, node = [tree], tree
        for part in os.path.relpath(path, self.o.root).split(os.sep):
            nxt = next((c for c in node.get("c", ()) if c["n"] == part and not c.get("o")), None)
            if nxt is None:
                break
            chain.append(nxt)
            node = nxt
        if "c" in node:  # the file is folded into this folder's "smaller items"
            other = next((c for c in node["c"] if c.get("o")), None)
            if other is None:
                return
            other["s"] -= size
        for n in chain:
            n["s"] -= size

    def start(self):
        self.t0 = time.time()
        self.top = self.walk(self.o.root, None)

    def wait(self):
        if self.pool:
            done = 0
            for fut in as_completed(self.futures):
                r = fut.result()
                self.merge(r)
                self.results[fut] = (r["size"], r["node"])
                done += 1
                now = time.time()
                if now - self._last_print > 0.25 or done == len(self.futures):
                    self._last_print = now
                    sys.stderr.write(f"\r\033[K  {self.files:>10,} files  {human(self.bytes):>10}  "
                                     f"{done:,}/{len(self.futures):,} folders")
            total, tree = self.resolve(self.top)
            seen = set()
            for links in [self.links] + self.worker_links:
                for key, size, path, dev in links:
                    if key in seen:
                        self.uncount(tree, path, size, dev)
                    seen.add(key)
            total = tree["s"] if tree else 0
        else:
            total, tree = self.top
        sys.stderr.write("\r\033[K")
        if tree is None:
            sys.exit(f"Cannot read {self.o.root}")
        tree["n"] = self.o.root
        self.duration = time.time() - self.t0
        return tree


# ------------------------------------------------------------------ duplicates
SAMPLE_CHUNKS, SAMPLE_SIZE = 16, 64 * 1024  # 1 MB read per file for the first pass
FULL_HASH_MAX = 512e6                       # bigger files are compared by samples only


def _hash(path, size, sampled):
    h = hashlib.blake2b(digest_size=16)
    sampled = sampled and size > SAMPLE_CHUNKS * SAMPLE_SIZE
    try:
        with open(path, "rb") as f:
            if not sampled:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            else:
                for i in range(SAMPLE_CHUNKS):
                    f.seek(int((size - SAMPLE_SIZE) * i / (SAMPLE_CHUNKS - 1)))
                    h.update(f.read(SAMPLE_SIZE))
    except OSError:
        return None
    return h.hexdigest()


def dupe_groups(size, paths):
    """Same-size files: compare samples first, then confirm with a full hash if not huge."""
    quick = defaultdict(list)
    for p in paths:
        q = _hash(p, size, sampled=True)
        if q:
            quick[q].append(p)
    sampled = size > FULL_HASH_MAX
    groups = []
    for same in quick.values():
        if len(same) < 2:
            continue
        full = {"": same}
        if not sampled:
            full = defaultdict(list)
            for p in same:
                h = _hash(p, size, False)
                if h:
                    full[h].append(p)
        for ps in full.values():
            if len(ps) > 1:
                groups.append({"size": size, "paths": sorted(ps), "sampled": sampled})
    return groups


def find_dupes(by_size, pool):
    futures = [pool.submit(_dupe_task, s, ps) for s, ps in by_size.items() if len(ps) > 1]
    groups = []
    for i, fut in enumerate(as_completed(futures)):
        sys.stderr.write(f"\r\033[K  checking duplicates {i + 1}/{len(futures)}")
        groups.extend(fut.result())
    sys.stderr.write("\r\033[K")
    groups.sort(key=lambda g: -g["size"] * (len(g["paths"]) - 1))
    return groups[:300]


# ------------------------------------------------------------- disk accounting
DATA_VOLUME = "/System/Volumes/Data"
# Places on the Data volume outside your home folder, sized for the System Data view
OUTSIDE_DIRS = [
    ("/Applications", "Applications"),
    ("/Library", "/Library (shared app data, fonts, drivers)"),
    ("/Users", "Other users & /Users/Shared"),
    ("/private/var", "/private/var (system databases, temp, logs)"),
    ("/opt", "/opt (Homebrew, etc.)"),
    ("/usr/local", "/usr/local"),
]
VOLUME_NOTES = {
    "System": "The sealed, read-only macOS system. Same size on every Mac running this macOS version.",
    "Preboot": "Boot files and Cryptexes (Safari and rapid security updates). Managed by macOS.",
    "Recovery": "macOS Recovery.",
    "VM": "Swap space. Grows when memory runs short and shrinks after a restart.",
    "Update": "Staged macOS updates.",
    "Data": "Everything you and your apps write: home folders, apps, /Library, /private/var, snapshots.",
}


def _run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def _plist(cmd):
    try:
        return plistlib.loads(_run(cmd))
    except Exception:  # empty or malformed output
        return None


def _important_free():
    """Free space counting what macOS can purge on demand (what Finder shows as available)."""
    js = ('ObjC.import("Foundation");var r=$.NSURL.fileURLWithPath("/").resourceValuesForKeysError('
          '$(["NSURLVolumeAvailableCapacityForImportantUsageKey"]),null);'
          'ObjC.unwrap(r.objectForKey("NSURLVolumeAvailableCapacityForImportantUsageKey"))')
    try:
        return int(_run(["osascript", "-l", "JavaScript", "-e", js]).strip())
    except ValueError:
        return None


def _swap_used():
    out = _run(["sysctl", "-n", "vm.swapusage"]).decode(errors="replace")
    try:  # "total = 2048.00M  used = 998.69M  free = ..."
        return int(float(out.split("used =")[1].split()[0].rstrip("M")) * 1024 * 1024)
    except (IndexError, ValueError):
        return None


def disk_info():
    du = shutil.disk_usage("/")
    info = {"total": du.total, "used": du.used, "free": du.free, "volumes": [],
            "importantFree": _important_free(), "swapUsed": _swap_used(), "snapshots": []}
    out = _run(["tmutil", "listlocalsnapshots", "/"], timeout=10).decode(errors="replace")
    info["snapshots"] = [line.strip() for line in out.splitlines() if "com.apple" in line]

    me, apfs = _plist(["diskutil", "info", "-plist", "/"]), _plist(["diskutil", "apfs", "list", "-plist"])
    if me and apfs:
        for c in apfs.get("Containers", []):
            if c.get("ContainerReference") != me.get("APFSContainerReference"):
                continue
            info["total"] = c["CapacityCeiling"]
            info["free"] = c["CapacityFree"]
            info["used"] = c["CapacityCeiling"] - c["CapacityFree"]
            for v in c.get("Volumes", []):
                role = (v.get("Roles") or [v.get("Name", "?")])[0]
                info["volumes"].append({"name": v.get("Name", "?"), "role": role,
                                        "used": v.get("CapacityInUse", 0),
                                        "note": VOLUME_NOTES.get(role, "")})
    return info


def du(path, skip, dev):
    """Size-only walk of `path`, counting files on volume `dev`. Returns (bytes, files, denied)."""
    total = files = denied = 0
    seen = set()
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            entries = listdir(d)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        for name, typ, edev, _mtime, ino, nlink, _size, alloc in entries:
            if edev != dev:
                continue  # e.g. system apps that live on the sealed System volume
            if typ == VDIR:
                p = os.path.join(d, name)
                if p not in skip:
                    stack.append(p)
            elif typ == VREG:
                if nlink > 1:
                    if (edev, ino) in seen:
                        continue
                    seen.add((edev, ino))
                total += alloc
                files += 1
    return total, files, denied


def system_start(root, skip, pool):
    """Queue size-only walks of the big places on the Data volume outside the scan."""
    data_dev = os.stat(DATA_VOLUME).st_dev
    jobs = []
    for path, label in OUTSIDE_DIRS:
        if not os.path.isdir(path) or root == path or path.startswith(root.rstrip("/") + "/"):
            continue  # already inside the scan
        if path == "/Users" and root != HOME:
            continue  # would mean walking (the rest of) your home; counted in "everything else"
        if root.startswith(path + "/"):
            label = f"{path}, outside this scan"
        # one task per subfolder so the walks spread across workers
        top, futures = [0, 0, 0], []
        try:
            entries = listdir(path)
        except OSError:
            entries, top[2] = [], 1
        for name, typ, edev, _mtime, _ino, _nlink, _size, alloc in entries:
            sub = os.path.join(path, name)
            if edev != data_dev or sub in skip or sub == root:
                continue
            if typ == VDIR:
                futures.append(pool.submit(_du_task, sub, skip | {root}, data_dev))
            elif typ == VREG:
                top[0] += alloc
                top[1] += 1
        jobs.append((path, label, top, futures))
    return jobs


def system_finish(jobs, scan_data_bytes, disk):
    """Explain the Data volume: this scan + the big places outside it + whatever is left."""
    data_used = next((v["used"] for v in disk["volumes"] if v["role"] == "Data"), None)
    parts = []
    for path, label, top, futures in jobs:
        size, files, denied = top
        for fut in futures:
            s, f, d = fut.result()
            size, files, denied = size + s, files + f, denied + d
        parts.append({"path": path, "label": label, "size": size, "files": files, "denied": denied})
    known = scan_data_bytes + sum(p["size"] for p in parts)
    return {
        "dataUsed": data_used,
        "scan": scan_data_bytes,
        "parts": parts,
        "rest": None if data_used is None else data_used - known,
    }


# --------------------------------------------------------------------- history
def flatten(tree):
    """path -> size for every mapped node (the grouped 'smaller items' are left out)."""
    out = {}
    stack = [(tree, tree["n"])]
    while stack:
        n, p = stack.pop()
        out[p] = n["s"]
        for c in n.get("c", ()):
            if not c.get("o"):
                stack.append((c, os.path.join(p, c["n"])))
    return out


def annotate(tree, prev):
    """Store each node's change since the previous scan as `g`."""
    stack = [(tree, tree["n"])]
    while stack:
        n, p = stack.pop()
        g = n["s"] - prev.get(p, 0)
        if g:
            n["g"] = g
        for c in n.get("c", ()):
            if not c.get("o"):
                stack.append((c, os.path.join(p, c["n"])))


def changes(cur, prev, root, min_change):
    """The paths that explain growth/shrinkage: a parent is listed only when no single
    child accounts for most (80%) of its change."""
    delta = {p: cur.get(p, 0) - prev.get(p, 0) for p in cur.keys() | prev.keys()}
    up, down = defaultdict(int), defaultdict(int)
    for p, d in delta.items():
        if p != root:
            parent = os.path.dirname(p)
            if d > 0:
                up[parent] = max(up[parent], d)
            elif d < 0:
                down[parent] = min(down[parent], d)

    def status(p):
        return "new" if p not in prev else "gone" if p not in cur else ""

    grew = [[d, p, status(p)] for p, d in delta.items() if d >= min_change and up[p] < 0.8 * d]
    shrank = [[d, p, status(p)] for p, d in delta.items() if d <= -min_change and down[p] > 0.8 * d]
    grew.sort(reverse=True)
    shrank.sort()
    return grew[:150], shrank[:150]


class History:
    def __init__(self, folder, root, keep):
        self.folder = folder
        self.root = root
        self.keep = keep
        self.index_path = os.path.join(folder, "index.json")
        try:
            with open(self.index_path) as f:
                self.index = json.load(f)
        except (OSError, ValueError):
            self.index = []
        self.mine = [e for e in self.index if e["root"] == root]

    def baseline(self, which):
        if not self.mine:
            return None, None
        if which == "latest":
            entry = self.mine[-1]
        elif which == "oldest":
            entry = self.mine[0]
        else:
            entry = next((e for e in reversed(self.mine) if e["scanned"].startswith(which)), None)
            if entry is None:
                sys.exit(f"No earlier scan of {self.root} matches --compare {which}. Saved scans: "
                         + ", ".join(e["scanned"] for e in self.mine))
        try:
            with gzip.open(os.path.join(self.folder, entry["file"]), "rt") as f:
                return entry, json.load(f)
        except (OSError, ValueError):
            return entry, None

    def save(self, sizes, meta):
        os.makedirs(self.folder, exist_ok=True)
        tag = hashlib.blake2b(self.root.encode(), digest_size=4).hexdigest()
        meta["file"] = f"{meta['scanned'].replace(':', '').replace('-', '').replace('T', '-')}_{tag}.json.gz"
        with gzip.open(os.path.join(self.folder, meta["file"]), "wt") as f:
            json.dump(sizes, f, separators=(",", ":"))
        self.mine.append(meta)
        drop = self.mine[:-self.keep] if len(self.mine) > self.keep else []
        for e in drop:
            try:
                os.remove(os.path.join(self.folder, e["file"]))
            except OSError:
                pass
        dropped = {e["file"] for e in drop}
        self.index = [e for e in self.index if e["file"] not in dropped] + [meta]
        with open(self.index_path, "w") as f:
            json.dump(self.index, f, indent=1)

    def trend(self):
        keys = ("scanned", "total", "used", "free")
        return [{k: e[k] for k in keys} for e in self.mine]

# ------------------------------------------------------------------------ main
def performance_cores():
    """More workers than performance cores is slower: they contend for filesystem locks."""
    try:
        return max(1, int(_run(["sysctl", "-n", "hw.perflevel0.physicalcpu"]).strip()))
    except ValueError:
        return min(4, os.cpu_count() or 4)


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
    ap.add_argument("--compare", default="latest", metavar="WHEN",
                    help="earlier scan to compare with: latest (default), oldest, or a date "
                         "prefix like 2026-10-01")
    ap.add_argument("--keep", type=int, default=30, help="scans kept per folder (default 30)")
    ap.add_argument("--no-history", action="store_true", help="don't save or compare scans")
    ap.add_argument("--no-system", action="store_true",
                    help="don't size /Applications, /Library etc. for the System Data view")
    ap.add_argument("--jobs", type=int, default=performance_cores(),
                    help="parallel worker processes (default: number of performance cores; "
                         "1 = single process)")
    args = ap.parse_args()

    root = os.path.realpath(os.path.expanduser(args.root))
    skip = {os.path.realpath(os.path.expanduser(p)) for p in args.skip}
    # Never descend into mounted volumes or the APFS data-volume mirror of /
    skip |= {"/System/Volumes", "/Volumes", "/dev", "/net", "/home"}
    skip.discard(root)

    print(f"Scanning {root} with {args.jobs} process{'es' if args.jobs > 1 else ''} …", file=sys.stderr)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    opts = SimpleNamespace(root=root, min_size=parse_size(args.min_size), skip=skip,
                           one_fs=args.one_fs, dupe_min=parse_size(args.dupe_min),
                           dev=os.stat(root).st_dev, data_dev=os.stat(DATA_VOLUME).st_dev,
                           now=time.time())
    pool = (ProcessPoolExecutor(args.jobs, initializer=_init_worker, initargs=(opts,))
            if args.jobs > 1 else Inline())
    timing = {}
    try:
        t = time.time()
        sc = Scanner(opts, pool if args.jobs > 1 else None)
        disk_future = ThreadPoolExecutor(1).submit(disk_info)  # diskutil/tmutil calls, ~0.3 s
        sc.start()
        # the System Data walks run in the same pool, alongside the main scan
        sys_jobs = [] if args.no_system else system_start(root, skip, pool)
        tree = sc.wait()
        timing["scan"] = time.time() - t
        print(f"  {sc.files:,} files, {sc.dirs:,} folders, {human(tree['s'])} in {sc.duration:.1f}s",
              file=sys.stderr)

        t = time.time()
        disk = disk_future.result()
        system = None if args.no_system else system_finish(sys_jobs, sc.data_bytes, disk)
        timing["system"] = time.time() - t

        t = time.time()
        dupes = [] if args.no_dupes else find_dupes(sc.by_size, pool)
        timing["duplicates"] = time.time() - t
    finally:
        pool.shutdown(cancel_futures=True)
    t = time.time()

    history = None
    if not args.no_history:
        hist = History(os.path.join(args.out, "history"), root, args.keep)
        base, prev = hist.baseline(args.compare)
        sizes = flatten(tree)
        history = {"baseline": None}
        if prev is not None:
            annotate(tree, prev)
            grew, shrank = changes(sizes, prev, root, max(10_000_000, 5 * opts.min_size))
            history["baseline"] = {**base, "grew": grew, "shrank": shrank,
                                   "totalDelta": tree["s"] - base["total"],
                                   "freeDelta": disk["free"] - base["free"]}
            print(f"  since {base['scanned'].replace('T', ' ')}: "
                  f"{'+' if tree['s'] >= base['total'] else '−'}{human(abs(tree['s'] - base['total']))}",
                  file=sys.stderr)
        hist.save(sizes, {"root": root, "scanned": started, "total": tree["s"],
                          "used": disk["used"], "free": disk["free"], "minSize": opts.min_size})
        history["trend"] = hist.trend()

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
                  "seconds": round(sc.duration, 1), "minSize": opts.min_size},
        "disk": disk,
        "system": system,
        "history": history,
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
    timing["history + report"] = time.time() - t

    junk_safe = sum(f["total"] for r, f in findings.items() if RULES[r][1] == "safe")
    print(f"  safe-to-clear junk: {human(junk_safe)}", file=sys.stderr)
    if sc.denied:
        print(f"  {len(sc.denied)} folders were not readable (grant Full Disk Access to your "
              "terminal to include them)", file=sys.stderr)
    print("  time: " + " · ".join(f"{k} {v:.1f}s" for k, v in timing.items()), file=sys.stderr)
    index = os.path.join(args.out, "index.html")
    print(f"Report: {index}", file=sys.stderr)
    if args.open:
        subprocess.run(["open", index])


if __name__ == "__main__":
    sys.setrecursionlimit(20000)
    main()
