"""Cleanup service: preview, single-use approval token, execute and receipt (synthetic folders only)."""
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from storage_assistant.cleanup import Cleanup
from storage_assistant.executor import FakeExecutor
from storage_assistant.plugin import Plugin
from storage_assistant.policy import StorageError, opaque

DAY = 86400
MB = 1_000_000


class FakeSpotlight:
    def metadata(self, paths):
        return {}


class FakeChecks:
    def is_icloud_synced(self, abs_path):
        return False

    def git_health(self, abs_path):
        return {"clean": False, "pushed": False, "remoteHost": None}

    def exact_duplicates(self, engine, node_ids):
        return {}


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.root = self.base / "chosen"
        self.root.mkdir()
        self.apps = self.base / "Applications"
        self.apps.mkdir()
        self.trash = self.base / "FakeTrash"
        self.trash.mkdir()
        self.now = time.time()
        self.offset = 0
        self.plugin = None
        self.executor = FakeExecutor(str(self.trash))

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

    def call(self, name, args):
        body = self.plugin.invoke(name, args)["structuredContent"]
        self.assertTrue(body["ok"], body)
        return body["data"]

    def wait(self, scan_id):
        for _ in range(400):
            status = self.call("storage_scan_status", {"scanId": scan_id})
            if status["state"] != "running":
                return status
            time.sleep(.05)
        self.fail("scan did not finish")

    def scan(self):
        self.plugin = Plugin(chooser=lambda: str(self.root), opener=lambda url: True)
        self.grant = self.call("storage_choose_folder", {})["scopes"][0]["grantId"]
        scan = self.call("storage_start_scan", {"grantId": self.grant, "requestId": opaque()})
        self.status = self.wait(scan["scanId"])
        self.cleanup = Cleanup(self.plugin.engine, self.executor, clock=lambda: self.now + self.offset,
                               spotlight=FakeSpotlight(), checks=FakeChecks(), applications=(str(self.apps),))
        return self.status

    def node_id(self, relative):
        engine = self.plugin.engine
        with engine.lock:
            for node in engine.scan["nodes"].values():
                if engine.relative_path(node) == relative:
                    return node["nodeId"]
        self.fail("no node " + relative)

    def suggest(self):
        return {c["path"]: c for c in self.cleanup.suggest(self.status["scanId"], self.status["scanVersion"])["candidates"]}

    def preview(self, paths, action="trash", ids=()):
        return self.cleanup.preview(self.status["scanId"], self.status["scanVersion"],
                                    [self.node_id(p) for p in paths] + list(ids), action)

    def skipped(self, plan):
        return {s["displayLabel"]: s["reason"] for s in plan["skipped"]}

    def two_partials(self):
        self.make("a.crdownload", 3000)
        self.make("b.part", 5000)
        self.age_dirs()
        self.scan()
        found = self.suggest()
        self.assertEqual(found["a.crdownload"]["tier"], "safe")
        self.assertEqual(found["b.part"]["tier"], "safe")

    def node_modules(self):
        self.make("p/package.json")
        self.make("p/index.js")
        self.make("p/node_modules/left-pad/index.js", 2 * MB)
        self.make("p/node_modules/left-pad/package.json")
        self.age_dirs()

    def test_preview_then_trash(self):
        self.two_partials()
        plan = self.preview(["a.crdownload", "b.part"])
        self.assertEqual(plan["action"], "trash")
        self.assertEqual(plan["count"], 2)
        self.assertEqual(len(plan["token"]), 32)
        self.assertAlmostEqual(plan["expiresAt"], self.now + 300)
        self.assertEqual(plan["skipped"], [])
        self.assertEqual({i["path"] for i in plan["items"]}, {"a.crdownload", "b.part"})
        self.assertEqual(plan["freesBytes"], str(sum(int(i["freesBytes"]) for i in plan["items"])))
        for item in plan["items"]:
            self.assertEqual(set(item), {"nodeId", "displayLabel", "path", "freesBytes", "reasons", "warnings"})
        version = self.status["scanVersion"]
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual(receipt["action"], "trash")
        self.assertEqual(len(receipt["done"]), 2)
        self.assertEqual(receipt["skipped"], [])
        self.assertEqual(sorted(os.listdir(self.trash)), ["a.crdownload", "b.part"])
        self.assertFalse((self.root / "a.crdownload").exists())
        self.assertIsInstance(receipt["freeSpaceBefore"], int)
        self.assertIsInstance(receipt["freeSpaceAfter"], int)
        self.assertEqual(receipt["freedEstimateBytes"], plan["freesBytes"])
        self.assertTrue(receipt["putBack"])
        self.assertTrue(all(d["trashPath"].startswith(str(self.trash)) for d in receipt["done"]))
        self.assertEqual(receipt["trashNote"], "Space is freed when you empty the Trash.")
        self.assertEqual([c[0] for c in self.executor.calls], ["trash", "trash"])
        self.assertEqual({c[1] for c in self.executor.calls},
                         {str(self.root / "a.crdownload"), str(self.root / "b.part")})
        engine = self.plugin.engine
        self.assertEqual(engine.view, {})
        self.assertEqual(engine.scan["scanVersion"], version + 1)  # rescan of the same grant started
        self.assertEqual(engine.grant.grant_id, self.grant)
        status = self.wait(engine.scan["scanId"])
        self.assertNotEqual(status["scanId"], self.status["scanId"])

    def test_token_single_use(self):
        self.two_partials()
        plan = self.preview(["a.crdownload"])
        first = self.cleanup.execute(plan["token"])
        self.assertEqual(len(first["done"]), 1)
        calls = list(self.executor.calls)
        version = self.plugin.engine.scan["scanVersion"]
        with self.assertRaises(StorageError) as caught:
            self.cleanup.execute(plan["token"])
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(self.executor.calls, calls)
        self.assertEqual(self.plugin.engine.scan["scanVersion"], version)  # no second rescan
        with self.assertRaises(StorageError) as caught:
            self.cleanup.execute("0" * 32)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_token_single_use_without_rescan(self):
        self.two_partials()
        plan = self.preview(["a.crdownload"])
        os.unlink(self.root / "a.crdownload")  # first execute acts on nothing, so no rescan happens
        first = self.cleanup.execute(plan["token"])
        self.assertEqual(first["done"], [])
        self.assertIsNone(first["rescan"])
        self.make("a.crdownload", 3000)
        with self.assertRaises(StorageError) as caught:
            self.cleanup.execute(plan["token"])
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(self.executor.calls, [])

    def test_identity_read_at_preview_is_bound(self):
        self.two_partials()
        plan = self.preview(["a.crdownload"])
        record = self.cleanup.tokens[plan["token"]]
        record["items"][0]["identity"] = (0, 0, 0, 0)  # as if another file now had this name
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual(receipt["skipped"][0]["reason"], "Changed since you reviewed it")
        self.assertEqual(self.executor.calls, [])

    def test_token_expires(self):
        self.two_partials()
        plan = self.preview(["a.crdownload"])
        self.offset = 301
        with self.assertRaises(StorageError) as caught:
            self.cleanup.execute(plan["token"])
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(self.executor.calls, [])
        self.assertTrue((self.root / "a.crdownload").exists())

    def test_rescan_invalidates_token(self):
        self.two_partials()
        plan = self.preview(["a.crdownload", "b.part"])
        rescan = self.call("storage_start_scan", {"grantId": self.grant, "requestId": opaque()})
        self.wait(rescan["scanId"])
        with self.assertRaises(StorageError) as caught:
            self.cleanup.execute(plan["token"])
        self.assertEqual(caught.exception.message, "This folder was rescanned. Review again.")
        self.assertEqual(self.executor.calls, [])
        self.assertTrue((self.root / "a.crdownload").exists())
        with self.assertRaises(StorageError) as caught:  # an old review cannot be refreshed either
            self.preview(["a.crdownload"])
        self.assertEqual(caught.exception.message, "This folder was rescanned. Review again.")

    def test_folder_and_child_selected(self):
        self.node_modules()
        self.scan()
        folder = self.suggest()["p/node_modules"]
        plan = self.preview(["p/node_modules/left-pad/index.js", "p/node_modules"])
        self.assertEqual([i["path"] for i in plan["items"]], ["p/node_modules"])
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["freesBytes"], folder["freesBytes"])
        self.assertEqual(plan["skipped"], [{"nodeId": self.node_id("p/node_modules/left-pad/index.js"),
                                            "displayLabel": "index.js", "reason": "Included in its folder"}])
        self.assertEqual(plan["items"][0]["reasons"], folder["reasons"])

    def test_changed_after_review_is_skipped(self):
        self.two_partials()
        target = self.make("../outside.txt", 10)
        plan = self.preview(["a.crdownload", "b.part"])
        os.unlink(self.root / "a.crdownload")
        os.symlink(target, self.root / "a.crdownload")
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual([d["path"] for d in receipt["done"]], ["b.part"])
        self.assertEqual(receipt["skipped"], [{"nodeId": self.node_id_from(plan, "a.crdownload"),
                                               "displayLabel": "a.crdownload", "reason": "Changed since you reviewed it"}])
        self.assertTrue(target.exists())
        self.assertTrue(os.path.islink(self.root / "a.crdownload"))
        self.assertEqual(self.executor.calls, [("trash", str(self.root / "b.part"))])
        done = next(i for i in plan["items"] if i["path"] == "b.part")
        self.assertEqual(receipt["freedEstimateBytes"], done["freesBytes"])

    def node_id_from(self, plan, path):
        return next(i["nodeId"] for i in plan["items"] if i["path"] == path)

    def test_moved_out_after_review_is_skipped(self):
        self.two_partials()
        plan = self.preview(["a.crdownload"])
        os.rename(self.root / "a.crdownload", self.base / "a.crdownload")
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual(receipt["done"], [])
        self.assertEqual(receipt["skipped"][0]["reason"], "Changed since you reviewed it")
        self.assertEqual(receipt["freedEstimateBytes"], "0")
        self.assertTrue((self.base / "a.crdownload").exists())

    def test_replaced_with_same_size_file_is_skipped(self):
        self.two_partials()
        plan = self.preview(["a.crdownload"])
        os.unlink(self.root / "a.crdownload")
        self.make("a.crdownload", 3000)  # same name, size and time, different file
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual(receipt["skipped"][0]["reason"], "Changed since you reviewed it")
        self.assertEqual(self.executor.calls, [])

    def test_denied_item_does_not_stop_others(self):
        self.executor = FakeExecutor(str(self.trash), fail={"b.part": PermissionError(1, "Operation not permitted")})
        self.two_partials()
        plan = self.preview(["a.crdownload", "b.part"])
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual([d["path"] for d in receipt["done"]], ["a.crdownload"])
        self.assertEqual([(s["displayLabel"], s["reason"]) for s in receipt["skipped"]],
                         [("b.part", "macOS denied access")])
        self.assertTrue((self.root / "b.part").exists())
        self.assertEqual([c[0] for c in self.executor.calls], ["trash"])  # never falls back to delete

    def test_too_many_inside_is_reported(self):
        from storage_assistant.executor import CleanupRefused
        self.node_modules()
        self.executor = FakeExecutor(str(self.trash), fail={"node_modules": CleanupRefused("too_many")})
        self.scan()
        self.suggest()
        plan = self.preview(["p/node_modules"], "delete")
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual(receipt["skipped"][0]["reason"], "Too many files inside to check safely")
        self.assertTrue((self.root / "p/node_modules/left-pad/index.js").exists())

    def test_delete_only_for_safe_tier(self):
        self.node_modules()
        self.make("old.bin", 12 * MB, age_days=400, sparse=True)
        self.age_dirs()
        self.scan()
        found = self.suggest()
        self.assertEqual(found["old.bin"]["tier"], "glance")
        plan = self.preview(["old.bin", "p/node_modules"], "delete")
        self.assertEqual(self.skipped(plan), {"old.bin": "Not offered for this action"})
        self.assertEqual([i["path"] for i in plan["items"]], ["p/node_modules"])
        self.assertTrue(plan["items"][0]["warnings"])
        receipt = self.cleanup.execute(plan["token"])
        self.assertEqual([d["path"] for d in receipt["done"]], ["p/node_modules"])
        self.assertIsNone(receipt["trashNote"])
        self.assertFalse(receipt["putBack"])
        self.assertFalse((self.root / "p/node_modules").exists())
        self.assertTrue((self.root / "p/index.js").exists())
        self.assertEqual(self.executor.calls, [("delete", "node_modules")])
        self.wait(self.plugin.engine.scan["scanId"])

    def test_delete_and_evict_need_a_current_suggestion(self):
        self.node_modules()
        self.scan()
        plan = self.preview(["p/node_modules"], "delete")  # suggest was never called
        self.assertEqual(self.skipped(plan), {"node_modules": "Not offered for this action"})
        self.assertIsNone(plan["token"])
        self.assertEqual(plan["count"], 0)

    def test_preview_rejects_protected_recent_icloud_and_outside(self):
        self.make("script.py", 2 * MB)
        self.make("new.crdownload", 100, age_days=1)
        self.make(".photo.jpg.icloud", 200)
        self.make("ok.crdownload", 100)
        self.age_dirs()
        self.scan()
        self.suggest()
        plan = self.preview(["script.py", "new.crdownload", ".photo.jpg.icloud", "ok.crdownload", "."],
                            ids=[opaque()])
        reasons = {s["displayLabel"]: s["reason"] for s in plan["skipped"] if s["displayLabel"] != "chosen"}
        self.assertEqual(reasons["script.py"], "Not offered for this action")
        self.assertEqual(reasons["new.crdownload"], "Changed in the last 7 days")
        self.assertEqual(reasons[".photo.jpg.icloud"], "Only in iCloud: deleting it frees nothing here")
        outside = [s for s in plan["skipped"] if s["reason"] == "Outside the selected folder"]
        self.assertEqual(len(outside), 1)
        self.assertEqual(outside[0]["displayLabel"], "Unknown item")
        root = [s for s in plan["skipped"] if s["nodeId"] == self.node_id(".")]
        self.assertEqual(root[0]["reason"], "Not offered for this action")
        self.assertEqual([i["path"] for i in plan["items"]], ["ok.crdownload"])
        for action in ("delete", "evict"):
            again = self.preview(["script.py", "new.crdownload", ".photo.jpg.icloud"], action)
            self.assertEqual(again["items"], [])

    def test_bounds(self):
        self.two_partials()
        for ids, action in (([opaque() for _ in range(501)], "trash"), ([], "trash"),
                            ([self.node_id("b.part")], "shred"), ("b.part", "trash")):
            with self.assertRaises(StorageError) as caught:
                self.cleanup.preview(self.status["scanId"], self.status["scanVersion"], ids, action)
            self.assertEqual(caught.exception.code, "INVALID_INPUT")
        plan = self.cleanup.preview(self.status["scanId"], self.status["scanVersion"],
                                    [opaque() for _ in range(500)], "trash")
        self.assertEqual(len(plan["skipped"]), 500)

    def test_delete_folder_with_new_file_inside_is_skipped(self):
        for extra in ("p/node_modules/left-pad/new.js", "p/node_modules/.env"):
            with self.subTest(extra=extra):
                self.node_modules()
                self.scan()
                self.assertEqual(self.suggest()["p/node_modules"]["tier"], "safe")
                plan = self.preview(["p/node_modules"], "delete")
                self.assertEqual(plan["count"], 1)
                when = os.stat(self.root / "p/node_modules/left-pad").st_mtime
                self.make(extra, 10, age_days=60)  # an old modification time does not hide it
                os.utime(self.root / "p/node_modules/left-pad", (when, when))  # node_modules itself is unchanged
                receipt = self.cleanup.execute(plan["token"])
                self.assertEqual(receipt["done"], [])
                self.assertEqual(receipt["skipped"][0]["reason"], "Changed since you reviewed it")
                self.assertTrue((self.root / "p/node_modules/left-pad/index.js").exists())
                self.assertEqual(self.executor.calls, [])
                self.plugin.close()
                self.plugin = None
                shutil.rmtree(self.root)
                self.root.mkdir()

    def test_delete_refuses_link_out_of_folder(self):
        self.node_modules()
        outside = self.make("../elsewhere/keep.txt", 10)
        os.symlink(outside, self.root / "p/node_modules/escape")
        os.symlink("left-pad/index.js", self.root / "p/node_modules/inside")
        self.age_dirs()
        for link in ("escape", "inside"):
            os.utime(self.root / "p/node_modules" / link, (self.now - 60 * DAY,) * 2, follow_symlinks=False)
        self.scan()
        self.assertIn("p/node_modules", self.suggest())
        plan = self.preview(["p/node_modules"], "delete")
        self.assertEqual(self.skipped(plan), {"node_modules": "Not offered for this action"})
        os.unlink(self.root / "p/node_modules/escape")

    def test_set_opt_in(self):
        self.two_partials()
        self.assertEqual(self.cleanup.set_opt_in("gitHealth", True), {"optIn": {"gitHealth": True, "exactDuplicates": False}})
        self.assertEqual(self.cleanup.set_opt_in("gitHealth", False)["optIn"]["gitHealth"], False)
        for name, value in (("shell", True), ("gitHealth", "yes")):
            with self.assertRaises(StorageError) as caught:
                self.cleanup.set_opt_in(name, value)
            self.assertEqual(caught.exception.code, "INVALID_INPUT")
        plan = self.preview(["a.crdownload"], "delete")  # opt-in change forgets earlier suggestions
        self.assertEqual(self.skipped(plan), {"a.crdownload": "Not offered for this action"})


if __name__ == "__main__":
    unittest.main()
