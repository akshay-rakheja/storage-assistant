import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from storage_assistant import optin
from storage_assistant.plugin import Plugin
from storage_assistant.policy import opaque

MB = 1_000_000
HAS_GIT = shutil.which("git") is not None


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(cwd)] + list(args),
                   check=True, capture_output=True)


@unittest.skipUnless(HAS_GIT, "git not installed")
class GitHealthTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.remote = self.base / "remote.git"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True, capture_output=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "origin", str(self.remote))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def commit_and_push(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def test_git_clean_and_pushed(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        health = optin.git_health(str(self.repo))
        self.assertTrue(health["clean"])
        self.assertTrue(health["pushed"])
        self.assertIsNone(health["remoteHost"])

    def test_git_dirty(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        (self.repo / "new.txt").write_text("untracked")
        health = optin.git_health(str(self.repo))
        self.assertFalse(health["clean"])

    def test_unpushed_commit_is_not_pushed(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        (self.repo / "b.txt").write_text("more")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "two")
        health = optin.git_health(str(self.repo))
        self.assertTrue(health["clean"])
        self.assertFalse(health["pushed"])

    def test_git_ignored_local_data_is_not_clean(self):
        (self.repo / ".gitignore").write_text("data/\n")
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        (self.repo / "data").mkdir()
        (self.repo / "data" / "local.db").write_text("precious")
        health = optin.git_health(str(self.repo))
        self.assertFalse(health["clean"])
        self.assertTrue(health["pushed"])

    def test_git_ignored_node_modules_is_still_clean(self):
        (self.repo / ".gitignore").write_text("node_modules/\n")
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        (self.repo / "node_modules" / "x").mkdir(parents=True)
        (self.repo / "node_modules" / "x" / "index.js").write_text("1")
        health = optin.git_health(str(self.repo))
        self.assertTrue(health["clean"])
        self.assertTrue(health["pushed"])

    def test_not_a_repo(self):
        health = optin.git_health(str(self.base))
        self.assertEqual(health, {"clean": False, "pushed": False, "remoteHost": None})


class RemoteHostTests(unittest.TestCase):
    def test_remote_host_strips_credentials(self):
        self.assertEqual(optin.remote_host("https://user:tok@github.com/a/b.git"), "github.com")
        self.assertEqual(optin.remote_host("git@github.com:a/b.git"), "github.com")
        self.assertEqual(optin.remote_host("ssh://git@example.org:22/a/b.git"), "example.org")
        self.assertIsNone(optin.remote_host("/tmp/remote.git"))
        self.assertIsNone(optin.remote_host(""))


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.root = self.base / "chosen"
        self.root.mkdir()
        self.plugin = None

    def tearDown(self):
        if self.plugin:
            self.plugin.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def scan(self):
        self.plugin = Plugin(chooser=lambda: str(self.root), opener=lambda url: True)
        call = lambda name, args: self.plugin.invoke(name, args)["structuredContent"]["data"]
        grant = call("storage_choose_folder", {})["scopes"][0]["grantId"]
        scan = call("storage_start_scan", {"grantId": grant, "requestId": opaque()})
        for _ in range(200):
            status = call("storage_scan_status", {"scanId": scan["scanId"]})
            if status["state"] != "running":
                return
            time.sleep(.05)
        self.fail("scan did not finish")

    def ids(self):
        engine = self.plugin.engine
        return {engine.relative_path(n): i for i, n in engine.scan["nodes"].items() if n["kind"] == "file"}

    def test_exact_duplicates_by_content(self):
        (self.root / "sub").mkdir()
        (self.root / "a.bin").write_bytes(b"x" * (2 * MB))
        (self.root / "sub" / "b.bin").write_bytes(b"x" * (2 * MB))
        (self.root / "c.bin").write_bytes(b"y" * (2 * MB))
        self.scan()
        ids = self.ids()
        result = optin.exact_duplicates(self.plugin.engine, list(ids.values()))
        self.assertEqual(result, {ids["sub/b.bin"]: ids["a.bin"]})

    def test_dedupe_keeps_first_by_path_and_no_chain(self):
        for name in ("z.bin", "m.bin", "b.bin"):
            (self.root / name).write_bytes(b"q" * (2 * MB))
        self.scan()
        ids = self.ids()
        result = optin.exact_duplicates(self.plugin.engine, list(ids.values()))
        self.assertEqual(result, {ids["m.bin"]: ids["b.bin"], ids["z.bin"]: ids["b.bin"]})

    def test_small_and_unknown_ids_ignored(self):
        (self.root / "a.bin").write_bytes(b"x" * 1000)
        (self.root / "b.bin").write_bytes(b"x" * 1000)
        self.scan()
        ids = self.ids()
        self.assertEqual(optin.exact_duplicates(self.plugin.engine, list(ids.values()) + ["nope"]), {})


if __name__ == "__main__":
    unittest.main()
