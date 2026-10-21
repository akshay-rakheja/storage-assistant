import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from storage_assistant import signals
from storage_assistant.plugin import Plugin
from storage_assistant.policy import opaque

DAY = 86400
MB = 1_000_000


class FakeSpotlight:
    def __init__(self, by_name=None):
        self.by_name = by_name or {}
        self.calls = []

    def metadata(self, paths):
        self.calls.append(list(paths))
        return {p: self.by_name[os.path.basename(p)] for p in paths if os.path.basename(p) in self.by_name}


class FakeChecks:
    def __init__(self, icloud=lambda path: False, git=None, duplicates=None):
        self.icloud, self.git, self.duplicates = icloud, git, duplicates or {}
        self.git_calls = []

    def is_icloud_synced(self, abs_path):
        return self.icloud(abs_path)

    def git_health(self, abs_path):
        self.git_calls.append(abs_path)
        return self.git or {"clean": False, "pushed": False, "remoteHost": None}

    def exact_duplicates(self, engine, node_ids):
        return self.duplicates


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.root = self.base / "chosen"
        self.root.mkdir()
        self.apps = self.base / "Applications"
        self.apps.mkdir()
        self.now = time.time()
        self.plugin = None

    def tearDown(self):
        if self.plugin:
            self.plugin.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def make(self, relative, size=100, age_days=60, sparse=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            if sparse:
                f.truncate(size)
            else:
                f.write(b"x" * size)
        when = self.now - age_days * DAY
        os.utime(path, (when, when))
        return path

    def age_dirs(self, days=60):
        when = self.now - days * DAY
        for d, _, _ in os.walk(self.root):
            os.utime(d, (when, when))

    def scan(self):
        self.plugin = Plugin(chooser=lambda: str(self.root), opener=lambda url: True)
        call = lambda name, args: self.plugin.invoke(name, args)["structuredContent"]["data"]
        grant = call("storage_choose_folder", {})["scopes"][0]["grantId"]
        scan = call("storage_start_scan", {"grantId": grant, "requestId": opaque()})
        for _ in range(200):
            status = call("storage_scan_status", {"scanId": scan["scanId"]})
            if status["state"] != "running":
                self.status = status
                return status
            time.sleep(.05)
        self.fail("scan did not finish")

    def suggest(self, spotlight=None, checks=None, opt_in=frozenset(), within=None):
        return signals.suggest(self.plugin.engine, self.status["scanId"], self.status["scanVersion"], within,
                               now=self.now, spotlight=spotlight or FakeSpotlight(), applications=(str(self.apps),),
                               opt_in=opt_in, checks=checks or FakeChecks())

    def by_path(self, result):
        return {c["path"]: c for c in result["candidates"]}

    def node_modules_project(self):
        self.make("p/package.json")
        self.make("p/index.js")
        self.make("p/node_modules/left-pad/index.js", 2 * MB)
        self.make("p/node_modules/left-pad/package.json")

    def test_node_modules_inactive_project_is_safe(self):
        self.node_modules_project()
        self.age_dirs()
        self.scan()
        result = self.suggest()
        c = self.by_path(result)["p/node_modules"]
        self.assertEqual(c["tier"], "safe")
        self.assertTrue(c["preTicked"])
        self.assertEqual(c["reasons"], [{"code": "rebuildable", "text": "Rebuilds with npm install"}])
        self.assertIn("delete", c["actions"])
        self.assertIn("trash", c["actions"])
        self.assertEqual(c["displayLabel"], "node_modules")
        self.assertEqual(c["kind"], "directory")
        self.assertEqual(c["freesBytes"], c["freesBytes"].strip())
        self.assertEqual(result["totals"]["safeBytes"], c["freesBytes"])
        self.assertEqual(result["totals"]["glanceBytes"], "0")
        self.assertEqual(len(result["candidates"]), 1)  # nothing inside the node_modules (ancestor wins)
        self.assertLess(c["modifiedAt"], self.now - 59 * DAY)

    def test_node_modules_active_project_is_glance(self):
        self.node_modules_project()
        self.make("p/src/a.js", age_days=3)
        self.age_dirs()
        self.scan()
        c = self.by_path(self.suggest())["p/node_modules"]
        self.assertEqual(c["tier"], "glance")
        self.assertFalse(c["preTicked"])
        self.assertNotIn("delete", c["actions"])

    def test_node_modules_without_marker_not_suggested(self):
        self.make("q/node_modules/x/index.js", 2 * MB)
        self.make("q/readme.txt")
        self.age_dirs()
        self.scan()
        self.assertEqual(self.suggest()["candidates"], [])

    def test_recently_changed_node_modules_not_suggested(self):
        self.node_modules_project()
        self.make("p/node_modules/new/index.js", age_days=2)
        self.age_dirs()
        self.scan()
        self.assertNotIn("p/node_modules", self.by_path(self.suggest()))

    def test_installer_for_installed_app(self):
        self.make("Ollama.dmg", 2 * MB)
        self.make("Unknown-1.2.dmg", 2 * MB)
        (self.apps / "Ollama.app" / "Contents").mkdir(parents=True)
        self.age_dirs()
        self.scan()
        found = self.by_path(self.suggest())
        c = found["Ollama.dmg"]
        self.assertEqual(c["tier"], "safe")
        self.assertIn({"code": "installed_app", "text": "Ollama is already installed"}, c["reasons"])
        self.assertNotIn("Unknown-1.2.dmg", found)

    def test_redownloadable_from_spotlight(self):
        self.make("tool.zip", 2 * MB)
        self.age_dirs()
        self.scan()
        url = "https://user:secret@github.com/x/y/releases/download/v1/tool.zip"
        spotlight = FakeSpotlight({"tool.zip": {"lastUsed": None, "whereFroms": [url, "https://github.com/x/y"]}})
        c = self.by_path(self.suggest(spotlight))["tool.zip"]
        self.assertEqual(c["tier"], "safe")
        self.assertEqual(c["reasons"], [{"code": "redownloadable", "text": "Downloaded from github.com, can be fetched again"}])
        self.assertNotIn("secret", repr(c))
        self.assertNotIn("releases", repr(c))
        self.assertTrue(spotlight.calls and all(p.startswith(str(self.root)) for p in spotlight.calls[0]))

    def test_untouched_large_file_is_glance(self):
        self.make("old.mov", 50 * MB, age_days=900, sparse=True)
        self.age_dirs()
        self.scan()
        last_used = self.now - 730 * DAY
        spotlight = FakeSpotlight({"old.mov": {"lastUsed": last_used, "whereFroms": []}})
        c = self.by_path(self.suggest(spotlight))["old.mov"]
        self.assertEqual(c["tier"], "glance")
        self.assertFalse(c["preTicked"])
        self.assertEqual(c["actions"], ["trash"])
        self.assertEqual(c["reasons"][0]["code"], "untouched")
        self.assertEqual(c["reasons"][0]["text"], "Untouched since " + str(time.localtime(last_used).tm_year))

    def test_untouched_falls_back_to_modification_time_and_respects_recent_use(self):
        self.make("old.mov", 50 * MB, age_days=900, sparse=True)
        self.make("used.mov", 50 * MB, age_days=900, sparse=True)
        self.age_dirs()
        self.scan()
        spotlight = FakeSpotlight({"used.mov": {"lastUsed": self.now - 10 * DAY, "whereFroms": []}})
        found = self.by_path(self.suggest(spotlight))
        self.assertEqual(found["old.mov"]["reasons"][0]["code"], "untouched")
        self.assertNotIn("used.mov", found)

    def test_recent_file_never_suggested(self):
        self.make("new.mov", 50 * MB, age_days=2, sparse=True)
        self.make("new.iso.crdownload", 2 * MB, age_days=1)
        self.age_dirs()
        self.scan()
        spotlight = FakeSpotlight({"new.mov": {"lastUsed": self.now - 900 * DAY, "whereFroms": []}})
        self.assertEqual(self.suggest(spotlight)["candidates"], [])

    def test_icloud_only_never_suggested_and_icloud_copy_offers_evict(self):
        self.make("Mobile Documents/dataless.mov", 20 * MB, sparse=True)
        self.make("cloud/local.mov", 20 * MB, sparse=True)
        self.make("cloud/gone.mov", 20 * MB, sparse=True)
        self.make("plain/local.bin", 20 * MB, sparse=True)
        self.age_dirs()
        self.scan()
        with self.plugin.engine.lock:
            for node in self.plugin.engine.scan["nodes"].values():
                if node["name"] == "gone.mov":
                    node["placeholder"] = "yes"  # simulate a dataless (iCloud-only) file
        checks = FakeChecks(icloud=lambda path: "/cloud/" in path)
        found = self.by_path(self.suggest(checks=checks))
        self.assertNotIn("cloud/gone.mov", found)
        self.assertNotIn("Mobile Documents/dataless.mov", found)  # sync roots are skipped by the scanner
        self.assertNotIn("plain/local.bin", found)
        c = found["cloud/local.mov"]
        self.assertEqual(c["actions"], ["evict"])
        self.assertEqual(c["reasons"], [{"code": "icloud_copy", "text": "Also in iCloud"}])

    def test_default_checks_detect_mobile_documents(self):
        checks = signals.Checks()
        home = os.path.expanduser("~")
        self.assertTrue(checks.is_icloud_synced(home + "/Library/Mobile Documents/com~apple~CloudDocs/a.mov"))
        self.assertFalse(checks.is_icloud_synced(str(self.root / "a.mov")))

    def test_spotlight_parses_batched_mdls_output(self):
        seen = []
        out = ('2024-03-01 10:00:00 +0000\0(\n    "https://github.com/x/y/a.zip",\n    "https://github.com/x/y"\n)'
               '\0(null)\0(null)').encode()
        def run(argv, **kwargs):
            seen.append(argv)
            return type("R", (), {"stdout": out})()
        result = signals.Spotlight(run).metadata(["/a/one.zip", "/a/two.zip", "relative.zip"])
        self.assertEqual(seen[0][:2], ["/usr/bin/mdls", "-raw"])
        self.assertEqual(seen[0][-2:], ["/a/one.zip", "/a/two.zip"])
        self.assertEqual(result["/a/one.zip"]["whereFroms"], ["https://github.com/x/y/a.zip", "https://github.com/x/y"])
        self.assertEqual(time.gmtime(result["/a/one.zip"]["lastUsed"]).tm_year, 2024)
        self.assertEqual(result["/a/two.zip"], {"lastUsed": None, "whereFroms": []})
        self.assertEqual(signals.Spotlight(lambda argv, **k: type("R", (), {"stdout": b"x"})()).metadata(["/a", "/b"]), {})

    def test_whole_repo_requires_git_optin_and_no_skipped_descendants(self):
        self.make("repo/.git/HEAD")
        self.make("repo/.git/objects/pack/p.pack", 3 * MB)
        self.make("repo/main.py")
        self.make("repo/big.bin", 30 * MB, age_days=900, sparse=True)
        self.make("other/.git/HEAD")
        self.make("other/main.py")
        self.make("other/.env")
        self.age_dirs()
        self.scan()
        clean = {"clean": True, "pushed": True, "remoteHost": "github.com"}
        self.assertEqual(self.suggest(checks=FakeChecks(git=clean))["candidates"], [])  # no opt-in
        checks = FakeChecks(git=clean)
        found = self.by_path(self.suggest(checks=checks, opt_in=frozenset({"gitHealth"})))
        self.assertNotIn("other", found)  # .env was skipped by the scan
        c = found["repo"]
        self.assertEqual(c["tier"], "safe")
        self.assertEqual(c["reasons"], [{"code": "repo_clean", "text": "Pushed to github.com, nothing unsaved"}])
        self.assertEqual(set(found), {"repo"})  # nothing inside the repo on its own
        dirty = FakeChecks(git={"clean": False, "pushed": True, "remoteHost": "github.com"})
        self.assertEqual(self.suggest(checks=dirty, opt_in=frozenset({"gitHealth"}))["candidates"], [])

    def test_grouped_entries_not_candidates(self):
        for folder in ("a", "b"):
            for i in range(30):
                self.make(f"{folder}/img{i}.jpg", 900_000, age_days=900, sparse=True)
        self.age_dirs(900)
        self.scan()
        with self.plugin.engine.lock:
            grouped = [n for n in self.plugin.engine.scan["nodes"].values() if "groupedFiles" in n]
        self.assertEqual(len(grouped), 2)
        self.assertEqual(self.suggest()["candidates"], [])

    def test_app_bundle_copy_is_safe(self):
        self.make("Downloads/Cyberduck.app/Contents/Info.plist")
        self.make("Downloads/Cyberduck.app/Contents/MacOS/Cyberduck", 3 * MB, age_days=900)
        (self.apps / "Cyberduck.app" / "Contents").mkdir(parents=True)
        self.age_dirs()
        self.scan()
        found = self.by_path(self.suggest())
        c = found["Downloads/Cyberduck.app"]
        self.assertEqual(c["tier"], "safe")
        self.assertEqual(c["reasons"], [{"code": "app_copy", "text": "Copy in Applications"}])
        self.assertEqual(set(found), {"Downloads/Cyberduck.app"})

    def test_unzipped_archive_and_possible_duplicate_are_glance(self):
        self.make("data.zip", 2 * MB)
        self.make("data/table.csv", 2 * MB)
        self.make("a/clip.mp4", 20 * MB, sparse=True)
        self.make("backup/clip.mp4", 20 * MB, sparse=True)
        self.age_dirs()
        self.scan()
        found = self.by_path(self.suggest())
        self.assertEqual(found["data.zip"]["tier"], "glance")
        self.assertEqual(found["data.zip"]["reasons"], [{"code": "unzipped", "text": "Already unzipped"}])
        self.assertNotIn("a/clip.mp4", found)
        dup = found["backup/clip.mp4"]
        self.assertEqual(dup["tier"], "glance")
        self.assertEqual(dup["reasons"], [{"code": "possible_duplicate", "text": "Possible duplicate of a/clip.mp4"}])
        self.assertEqual(set(found), {"data.zip", "backup/clip.mp4"})

    def test_exact_duplicate_optin_is_safe(self):
        self.make("a/clip.mp4", 20 * MB, sparse=True)
        self.make("backup/clip.mp4", 20 * MB, sparse=True)
        self.age_dirs()
        self.scan()
        with self.plugin.engine.lock:
            ids = {self.plugin.engine.relative_path(n): k for k, n in self.plugin.engine.scan["nodes"].items()}
        checks = FakeChecks(duplicates={ids["backup/clip.mp4"]: ids["a/clip.mp4"]})
        dup = self.by_path(self.suggest(checks=checks, opt_in=frozenset({"exactDuplicates"})))["backup/clip.mp4"]
        self.assertEqual(dup["tier"], "safe")
        self.assertEqual(dup["reasons"][0], {"code": "exact_duplicate", "text": "Exact duplicate of a/clip.mp4"})

    def test_partial_download_is_safe(self):
        self.make("big.iso.crdownload", 2 * MB)
        self.age_dirs()
        self.scan()
        c = self.by_path(self.suggest())["big.iso.crdownload"]
        self.assertEqual(c["tier"], "safe")
        self.assertEqual(c["reasons"], [{"code": "partial_download", "text": "Unfinished download"}])
        self.assertEqual(c["actions"], ["trash", "delete"])

    def test_within_node_and_sorting(self):
        self.make("x/one.part", 3 * MB)
        self.make("y/two.part", 5 * MB)
        self.make("y/three.part", 1 * MB)
        self.make("y/old.mov", 50 * MB, age_days=900, sparse=True)
        self.age_dirs()
        self.scan()
        everything = self.suggest()["candidates"]
        self.assertEqual([c["path"] for c in everything], ["y/two.part", "x/one.part", "y/three.part", "y/old.mov"])
        with self.plugin.engine.lock:
            y = next(k for k, n in self.plugin.engine.scan["nodes"].items() if n["name"] == "y")
        self.assertEqual({c["path"] for c in self.suggest(within=y)["candidates"]}, {"y/two.part", "y/three.part", "y/old.mov"})


if __name__ == "__main__":
    unittest.main()
