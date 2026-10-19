"""Tests for diskmap.py. Run from the repo root:

    python3 -m unittest discover tests

They build small folder trees in a temp directory, so the macOS-specific parts
(getattrlistbulk, APFS clones, Date Added) run for real. macOS only.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import diskmap as dm  # noqa: E402

MB = 1_000_000


def setUpModule():
    # the scanner's progress lines go to stderr; keep them out of the test output
    global _stderr
    _stderr, sys.stderr = sys.stderr, io.StringIO()


def tearDownModule():
    sys.stderr = _stderr


def write(path, size=0, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if data is not None else os.urandom(size))
    return path


def opts(root, **kw):
    o = SimpleNamespace(root=root, min_size=1, skip=set(), one_fs=False, dupe_min=MB,
                        dev=os.stat(root).st_dev, data_dev=os.stat(dm.DATA_VOLUME).st_dev,
                        now=time.time())
    o.__dict__.update(kw)
    return o


def scan(root, jobs=1, **kw):
    """Run the scanner the way main() does; returns (scanner, tree)."""
    o = opts(root, **kw)
    if jobs == 1:
        sc = dm.Scanner(o)
        sc.start()
        return sc, sc.wait()
    with ProcessPoolExecutor(jobs, initializer=dm._init_worker, initargs=(o,)) as pool:
        sc = dm.Scanner(o, pool)
        sc.start()
        return sc, sc.wait()


def find(tree, *names):
    node = tree
    for name in names:
        node = next(c for c in node.get("c", ()) if c["n"] == name)
    return node


def load_report(out):
    with open(os.path.join(out, "data.js"), encoding="utf-8") as f:
        text = f.read()
    return json.loads(text[text.index("=") + 1:].rstrip().rstrip(";"))


class TempDirTest(unittest.TestCase):
    def setUp(self):
        # realpath: /var is a symlink to /private/var, and the scanner resolves its root
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="diskmap-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)


# ------------------------------------------------------------------ helpers
class HelperTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(dm.parse_size("2M"), 2_000_000)
        self.assertEqual(dm.parse_size("100K"), 100_000)
        self.assertEqual(dm.parse_size("1.5G"), 1_500_000_000)
        self.assertEqual(dm.parse_size("20MB"), 20_000_000)
        self.assertEqual(dm.parse_size("512"), 512)

    def test_human(self):
        self.assertEqual(dm.human(999), "999 B")
        self.assertEqual(dm.human(1_500_000), "1.5 MB")
        self.assertEqual(dm.human(2_000_000_000_000), "2.0 TB")

    def test_category(self):
        self.assertEqual(dm.category("movie.MOV"), "video")
        self.assertEqual(dm.category("photo.heic"), "image")
        self.assertEqual(dm.category("Installer.dmg"), "disk")
        self.assertEqual(dm.category("main.py"), "code")
        self.assertEqual(dm.category("README"), "other")


# ------------------------------------------------------------------ listing
class ListdirTests(TempDirTest):
    def test_bulk_listing_matches_stat(self):
        write(self.p("a.bin"), 5000)
        write(self.p("empty"), 0)
        os.mkdir(self.p("sub"))
        os.symlink("a.bin", self.p("link"))
        os.link(self.p("a.bin"), self.p("hard"))

        def norm(entries):
            # everything but the change time (bulk also folds in Date Added); link count and
            # sizes only matter for files, and the bulk call doesn't report them for folders
            out = []
            for n, t, dev, _changed, ino, nlink, size, alloc in entries:
                kind = t if t in (dm.VREG, dm.VDIR) else "other"
                out.append((n, kind, dev, ino) + ((nlink, size, alloc) if kind == dm.VREG else ()))
            return sorted(out)

        self.assertEqual(norm(dm._listdir_bulk(self.tmp)), norm(dm._listdir_stat(self.tmp)))

    def test_change_time_includes_date_added(self):
        f = write(self.p("old.txt"), 10)
        os.utime(f, (946684800, 946684800))  # modified in 2000, but added just now
        (entry,) = dm.listdir(self.tmp)
        self.assertGreater(entry[3], time.time() - 3600)

    def test_unreadable_folder_raises_permission_error(self):
        os.mkdir(self.p("locked"), 0o000)
        self.addCleanup(os.chmod, self.p("locked"), 0o755)
        with self.assertRaises(PermissionError):
            dm.listdir(self.p("locked"))


# ------------------------------------------------------------------ scanner
class ScannerTests(TempDirTest):
    def build(self):
        # deep enough (> SPLIT_DEPTH) that the parallel scan hands folders to workers
        write(self.p("a", "b", "c", "d", "big.bin"), 3 * MB)
        write(self.p("a", "b", "c", "d", "small.txt"), 100)
        write(self.p("a", "b", "c2", "e", "f", "x.bin"), 2 * MB)
        write(self.p("top.bin"), 1 * MB)
        write(self.p("tiny1"), 10)
        write(self.p("tiny2"), 10)
        os.symlink("top.bin", self.p("link-to-top"))
        # the same file hard-linked into two subtrees that different workers scan
        write(self.p("h1", "x", "y", "z", "shared.bin"), 2 * MB)
        os.makedirs(self.p("h2", "x", "y", "z"))
        os.link(self.p("h1", "x", "y", "z", "shared.bin"), self.p("h2", "x", "y", "z", "same.bin"))

    def alloc(self, *parts):
        return os.stat(self.p(*parts)).st_blocks * 512

    def test_sizes_and_counts(self):
        self.build()
        sc, tree = scan(self.tmp)
        self.assertEqual(sc.files, 7)  # the hard link counts once, the symlink not at all
        self.assertEqual(tree["n"], self.tmp)
        self.assertEqual(find(tree, "a", "b", "c", "d", "big.bin")["s"], self.alloc("a", "b", "c", "d", "big.bin"))
        children = sum(c["s"] for c in tree["c"])
        self.assertEqual(children, tree["s"], "children add up to their folder")

    def test_small_items_are_grouped(self):
        self.build()
        _, tree = scan(self.tmp, min_size=500_000)
        names = [c["n"] for c in tree["c"]]
        self.assertNotIn("tiny1", names)
        other = [c for c in tree["c"] if c.get("o")]
        self.assertEqual(len(other), 1)
        self.assertIn("smaller items", other[0]["n"])

    def test_parallel_scan_matches_single_process(self):
        self.build()
        sc1, t1 = scan(self.tmp, jobs=1)
        sc4, t4 = scan(self.tmp, jobs=4)
        self.assertGreater(len(sc4.futures), 1, "work was split across processes")
        f1, f4 = dm.flatten(t1), dm.flatten(t4)
        # the hard-linked file may sit under h1 or h2 depending on scan order, but only once
        linked = os.stat(self.p("h1", "x", "y", "z", "shared.bin")).st_blocks * 512
        for f in (f1, f4):
            self.assertEqual(f.get(self.p("h1"), 0) + f.get(self.p("h2"), 0), linked)
        drop = lambda f: {k: v for k, v in f.items() if not k.startswith((self.p("h1"), self.p("h2")))}
        self.assertEqual(drop(f1), drop(f4))
        self.assertEqual((sc1.files, sc1.dirs, sc1.bytes), (sc4.files, sc4.dirs, sc4.bytes))
        self.assertEqual(dict(sc1.types), dict(sc4.types))

    def test_skip_and_denied(self):
        self.build()
        os.mkdir(self.p("locked"), 0o000)
        self.addCleanup(os.chmod, self.p("locked"), 0o755)
        sc, tree = scan(self.tmp, skip={self.p("a")})
        names = [c["n"] for c in tree["c"]]
        self.assertNotIn("a", names)
        self.assertIn(self.p("locked"), sc.denied)

    def test_folder_change_time_is_newest_inside(self):
        write(self.p("d", "f.txt"), 10)
        _, tree = scan(self.tmp)
        newest = int(dm.listdir(self.p("d"))[0][3])
        self.assertEqual(find(tree, "d")["m"], newest)
        self.assertEqual(tree["m"], newest)

    def test_junk_folders_found_in_tree(self):
        write(self.p("proj", "node_modules", "lib", "index.js"), 2000)
        write(self.p("proj", "__pycache__", "m.pyc"), 500)
        write(self.p("proj", "build", "CACHEDIR.TAG"), 50)
        write(self.p("proj", ".venv", "pyvenv.cfg"), 50)
        write(self.p("proj", ".DS_Store"), 50)
        sc, tree = scan(self.tmp)
        self.assertEqual(find(tree, "proj", "node_modules")["j"], "node_modules")
        self.assertEqual(set(sc.findings),
                         {"node_modules", "pycache", "build-cache", "venv", "ds_store"})
        # nothing inside a junk folder is reported again
        self.assertEqual(len(sc.findings["node_modules"]), 1)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.sc = dm.Scanner(SimpleNamespace(now=time.time()))

    def test_dir_rules(self):
        home = dm.HOME
        rule = self.sc.dir_rule
        self.assertEqual(rule(f"{home}/Library/Caches", "Caches", set()), "caches")
        self.assertEqual(rule(f"{home}/.Trash", ".Trash", set()), "trash")
        self.assertEqual(rule("/x/node_modules", "node_modules", set()), "node_modules")
        self.assertEqual(rule(f"{home}/Library/Application Support/Slack/Cache", "Cache", set()), "app-caches")
        self.assertIsNone(rule("/x/proj/Cache", "Cache", set()), "Cache only counts inside app folders")
        self.assertEqual(rule("/x/target", "target", {"CACHEDIR.TAG"}), "build-cache")
        self.assertEqual(rule("/x/env", "env", {"pyvenv.cfg", "bin"}), "venv")
        self.assertIsNone(rule("/x/photos", "photos", {"a.jpg"}))

    def test_file_rules(self):
        now = time.time()
        rule = self.sc.file_rule
        self.assertEqual(rule("/x/.DS_Store", ".DS_Store", 6000, now), "ds_store")
        self.assertEqual(rule("/x/iPhone.ipsw", "iPhone.ipsw", 5 * MB, now), "ipsw")
        self.assertEqual(rule(f"{dm.DOWNLOADS}/app.dmg", "app.dmg", 50 * MB, now), "downloads-installers")
        self.assertIsNone(rule("/x/app.dmg", "app.dmg", 50 * MB, now), "only installers in Downloads")
        self.assertEqual(rule("/x/server.log", "server.log", 60 * MB, now), "big-logs")
        self.assertEqual(rule("/x/movie.mov", "movie.mov", 600 * MB, now - 200 * 86400), "stale-large")
        self.assertIsNone(rule("/x/movie.mov", "movie.mov", 600 * MB, now - 10 * 86400))


# ------------------------------------------------------------------ duplicates
class DuplicateTests(TempDirTest):
    def test_identical_files_grouped_and_lookalikes_not(self):
        data = os.urandom(3 * MB)
        a = write(self.p("a.bin"), data=data)
        b = write(self.p("b.bin"), data=data)
        c = write(self.p("c.bin"), data=data[:-1] + b"\0")  # same size, one byte differs
        groups = dm.dupe_groups(len(data), [a, b, c])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["paths"], sorted([a, b]))
        self.assertEqual(groups[0]["wasted"], os.stat(a).st_blocks * 512)
        self.assertFalse(groups[0]["clones"])

    def test_apfs_clones_waste_nothing(self):
        a = write(self.p("a.bin"), 3 * MB)
        subprocess.run(["cp", "-c", a, self.p("clone.bin")], check=True)
        shutil.copyfile(a, self.p("copy.bin"))
        alloc = os.stat(a).st_blocks * 512
        self.assertEqual(dm.real_extra([a, self.p("clone.bin")], 3 * MB), (0, True))
        self.assertEqual(dm.real_extra([a, self.p("clone.bin"), self.p("copy.bin")], 3 * MB), (alloc, True))
        self.assertEqual(dm.real_extra([a, self.p("copy.bin")], 3 * MB), (alloc, False))

    def make_project(self, name, text=b"print('hi')\n"):
        write(self.p(name, "assets", "one.bin"), data=self.blob1)
        write(self.p(name, "assets", "two.bin"), data=self.blob2)
        write(self.p(name, "src", "main.py"), data=text)

    def dupe_dirs(self):
        old = dm.DUPE_DIR_MIN
        dm.DUPE_DIR_MIN = 0  # the fixture is far smaller than 50 MB
        try:
            sc, _ = scan(self.tmp)
        finally:
            dm.DUPE_DIR_MIN = old
        return dm.find_dupe_dirs(sc.dir_sigs, dm.Inline())

    def test_duplicate_folders(self):
        self.blob1, self.blob2 = os.urandom(2 * MB), os.urandom(MB)
        self.make_project("proj")
        self.make_project("proj-copy")
        self.make_project("other", text=b"print('yo')\n")  # same names and sizes, one file differs
        groups = {frozenset(g["paths"]) for g in self.dupe_dirs()}
        p = self.p
        self.assertIn({p("proj"), p("proj-copy")}, groups, "the identical pair survives the lookalike")
        self.assertIn({p("other", "assets"), p("proj", "assets"), p("proj-copy", "assets")}, groups,
                      "other/assets really is a copy, so it's still reported")
        self.assertNotIn({p("proj", "src"), p("proj-copy", "src")}, groups,
                         "folders inside a reported duplicate aren't listed again")

    def test_folder_copies_with_different_content_are_rejected(self):
        self.blob1, self.blob2 = os.urandom(2 * MB), os.urandom(MB)
        self.make_project("proj")
        self.make_project("fake")
        write(self.p("fake", "assets", "one.bin"), 2 * MB)  # same name and size, new bytes
        self.assertEqual(dm.verify_dupe_dirs(3 * MB, [self.p("proj"), self.p("fake")]), [])
        self.make_project("proj-copy")
        (group,) = dm.verify_dupe_dirs(3 * MB, [self.p("proj"), self.p("fake"), self.p("proj-copy")])
        self.assertEqual(set(group["paths"]), {self.p("proj"), self.p("proj-copy")})
        self.assertEqual(group["files"], 3)


# ------------------------------------------------------------------ untouched folders
class UntouchedTests(unittest.TestCase):
    def test_outermost_old_folders_only(self):
        old, new, cutoff = 1_000, 9_000, 5_000
        big = 200 * MB
        tree = {"n": "/r", "s": 3 * big, "d": 1, "m": new, "c": [
            {"n": "archive", "s": big, "d": 1, "m": old, "c": [
                {"n": "inner", "s": big, "d": 1, "m": old}]},
            {"n": "active", "s": big, "d": 1, "m": new, "c": [
                {"n": "old-part", "s": big, "d": 1, "m": old}]},
            {"n": "Old.app", "s": big, "d": 1, "m": old, "c": [
                {"n": "Contents", "s": big, "d": 1, "m": old}]},
            {"n": "small", "s": MB, "d": 1, "m": old},
            {"n": "file.bin", "s": big, "t": "other", "m": old},
        ]}
        paths = [p for _, p, _ in dm.untouched_folders(tree, cutoff)]
        self.assertEqual(sorted(paths), ["/r/active/old-part", "/r/archive"])


# ------------------------------------------------------------------ history
class HistoryTests(TempDirTest):
    def test_flatten_and_annotate(self):
        tree = {"n": "/r", "s": 30, "c": [{"n": "a", "s": 20}, {"n": "2 smaller items", "s": 10, "o": 1}]}
        self.assertEqual(dm.flatten(tree), {"/r": 30, "/r/a": 20})
        dm.annotate(tree, {"/r": 25, "/r/a": 20})
        self.assertEqual(tree["g"], 5)
        self.assertNotIn("g", tree["c"][0], "unchanged nodes carry no delta")

    def test_changes_point_at_the_deepest_cause(self):
        prev = {"/r": 130, "/r/a": 60, "/r/a/x": 60, "/r/b": 40, "/r/gone": 30}
        cur = {"/r": 150, "/r/a": 110, "/r/a/x": 110, "/r/b": 40}
        grew, shrank = dm.changes(cur, prev, "/r", 10)
        self.assertEqual([p for _, p, _ in grew], ["/r/a/x"], "parents explained by one child are dropped")
        self.assertEqual(shrank, [[-30, "/r/gone", "gone"]])

    def test_new_items_are_marked(self):
        grew, _ = dm.changes({"/r": 50, "/r/n": 50}, {"/r": 0}, "/r", 10)
        self.assertEqual(grew, [[50, "/r/n", "new"]])

    def test_save_baseline_and_retention(self):
        h = dm.History(self.p("hist"), "/scan/root", keep=2)
        self.assertEqual(h.baseline("latest"), (None, None))
        for i, stamp in enumerate(["2026-01-01T10:00:00", "2026-02-01T10:00:00", "2026-03-01T10:00:00"]):
            h.save({"/scan/root": i}, {"root": "/scan/root", "scanned": stamp, "total": i,
                                       "used": 0, "free": 0, "minSize": 1})
        h = dm.History(self.p("hist"), "/scan/root", keep=2)
        self.assertEqual([e["scanned"][:7] for e in h.mine], ["2026-02", "2026-03"])
        self.assertEqual(len([f for f in os.listdir(self.p("hist")) if f.endswith(".gz")]), 2)
        self.assertEqual(h.baseline("latest")[1], {"/scan/root": 2})
        self.assertEqual(h.baseline("oldest")[1], {"/scan/root": 1})
        self.assertEqual(h.baseline("2026-02")[1], {"/scan/root": 1})
        with self.assertRaises(SystemExit):
            h.baseline("2025")
        self.assertEqual(dm.History(self.p("hist"), "/other", 2).baseline("latest"), (None, None),
                         "scans of other folders are never compared")


# ------------------------------------------------------------------ system data
class SystemDataTests(TempDirTest):
    def test_outside_dirs_respect_the_scan_root(self):
        # the temp folder lives on the Data volume, so it can stand in for /Applications etc.
        for name in ("Applications", "Library"):
            write(self.p(name, "x", "f.bin"), MB)
        old = dm.OUTSIDE_DIRS
        dm.OUTSIDE_DIRS = [(self.p("Applications"), "Apps"), (self.p("Library"), "Library"),
                           ("/Users", "Users")]
        self.addCleanup(setattr, dm, "OUTSIDE_DIRS", old)
        jobs = dm.system_start(self.p("Library", "x"), set(), dm.Inline())
        labels = {path: label for path, label, _top, _f in jobs}
        self.assertEqual(labels[self.p("Applications")], "Apps")
        self.assertEqual(labels[self.p("Library")], f"{self.p('Library')}, outside this scan")
        self.assertNotIn("/Users", labels, "/Users is only sized when scanning your home folder")
        result = dm.system_finish(jobs, 0, {"volumes": [{"role": "Data", "used": 10 * MB}]})
        sizes = {p["path"]: p["size"] for p in result["parts"]}
        self.assertEqual(sizes[self.p("Applications")], os.stat(self.p("Applications", "x", "f.bin")).st_blocks * 512)
        self.assertEqual(sizes[self.p("Library")], 0, "the scanned folder isn't counted twice")
        self.assertEqual(result["rest"], 10 * MB - sizes[self.p("Applications")])


# ------------------------------------------------------------------ end to end
class CommandLineTests(TempDirTest):
    def run_cli(self, *args):
        r = subprocess.run([sys.executable, os.path.join(REPO, "diskmap.py"), *args],
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stderr

    def test_report_and_history(self):
        root, out = self.p("root"), self.p("out")
        write(self.p("root", "docs", "a.bin"), 2 * MB)
        write(self.p("root", "node_modules", "x.js"), 1000)
        common = [root, "--out", out, "--no-system", "--jobs", "2", "--min-size", "1K"]
        log = self.run_cli(*common)
        self.assertIn("time: scan", log)
        for page_file in ("index.html", "d3.min.js", "fonts/OFL.txt"):
            self.assertTrue(os.path.exists(os.path.join(out, page_file)), f"{page_file} copied to --out")
        data = load_report(out)
        for key in ("tree", "findings", "disk", "history", "dupes", "dupeDirs", "untouched", "rules"):
            self.assertIn(key, data)
        self.assertEqual(data["root"], root)
        self.assertIsNone(data["history"]["baseline"])
        self.assertIn("node_modules", data["findings"])
        self.assertNotIn("_h", json.dumps(data["tree"]), "scan-only fields are stripped")

        write(self.p("root", "docs", "b.bin"), 12 * MB)  # changes under 10 MB aren't listed
        self.run_cli(*common)
        data = load_report(out)
        base = data["history"]["baseline"]
        self.assertIsNotNone(base)
        self.assertEqual([p for _, p, _ in base["grew"]], [os.path.join(root, "docs", "b.bin")])
        self.assertEqual(len(data["history"]["trend"]), 2)

    def test_no_history(self):
        root, out = self.p("root"), self.p("out")
        write(self.p("root", "a.bin"), MB)
        self.run_cli(root, "--out", out, "--no-system", "--no-history", "--jobs", "1")
        self.assertIsNone(load_report(out)["history"])
        self.assertFalse(os.path.exists(os.path.join(out, "history")))


if __name__ == "__main__":
    unittest.main()
