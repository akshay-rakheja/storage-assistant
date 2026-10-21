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

    def marker_script(self, name):
        """An executable that would create a marker file if git ever ran it."""
        marker = self.base / (name + ".marker")
        script = self.base / (name + ".sh")
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
        script.chmod(0o755)
        return script, marker

    def test_fsmonitor_from_repo_config_never_runs(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        script, marker = self.marker_script("fsmonitor")
        git(self.repo, "config", "core.fsmonitor", str(script))
        health = optin.git_health(str(self.repo))
        self.assertFalse(marker.exists(), "git ran the repo's core.fsmonitor command")
        self.assertFalse(health["clean"])

    def test_clean_filter_from_repo_config_never_runs(self):
        (self.repo / "a.txt").write_text("hi")
        (self.repo / ".gitattributes").write_text("*.txt filter=evil\n")
        self.commit_and_push()
        script, marker = self.marker_script("filter")
        git(self.repo, "config", "filter.evil.clean", str(script))
        later = time.time() + 5
        os.utime(self.repo / "a.txt", (later, later))  # stat-dirty: status would re-hash through the clean filter
        health = optin.git_health(str(self.repo))
        self.assertFalse(marker.exists(), "git ran the repo's clean filter")
        self.assertFalse(health["clean"])

    def test_filter_in_info_attributes_with_config_never_runs(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        script, marker = self.marker_script("info")
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
        git(self.repo, "config", "filter.evil.clean", str(script))
        later = time.time() + 5
        os.utime(self.repo / "a.txt", (later, later))
        self.assertFalse(optin.git_health(str(self.repo))["clean"])
        self.assertFalse(marker.exists())

    def test_gitattributes_filter_alone_is_not_clean(self):
        (self.repo / "a.txt").write_text("hi")
        (self.repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        self.commit_and_push()
        self.assertFalse(optin.git_health(str(self.repo))["clean"])

    def test_config_outside_allowlist_is_not_clean(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        self.assertTrue(optin.git_health(str(self.repo))["clean"])  # default keys plus remote/branch are allowed
        for key, value in (("include.path", "/dev/null"), ("core.hooksPath", "/tmp"), ("diff.x.textconv", "cat"),
                           ("core.pager", "less"), ("user.name", "t")):
            with self.subTest(key=key):
                git(self.repo, "config", key, value)
                self.assertFalse(optin.git_health(str(self.repo))["clean"])
                git(self.repo, "config", "--unset", key)

    def test_missing_developer_tools_never_runs_git(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        original_check, original_git = optin.XCODE_SELECT, optin._git
        calls = []
        optin.XCODE_SELECT = ["/usr/bin/false"]
        optin._git = lambda *a: calls.append(a) or ""
        try:
            health = optin.git_health(str(self.repo))
        finally:
            optin.XCODE_SELECT, optin._git = original_check, original_git
        self.assertEqual(health, {"clean": False, "pushed": False, "remoteHost": None})
        self.assertEqual(calls, [])

    def test_remote_host_comes_from_config(self):
        (self.repo / "a.txt").write_text("hi")
        self.commit_and_push()
        git(self.repo, "remote", "set-url", "--push", "origin", str(self.remote))  # remote.origin.pushurl: not allowed
        self.assertFalse(optin.git_health(str(self.repo))["clean"])
        git(self.repo, "config", "--unset", "remote.origin.pushurl")
        git(self.repo, "config", "remote.origin.url", "https://user:tok@github.com/a/b.git")
        self.assertEqual(optin.git_health(str(self.repo))["remoteHost"], "github.com")


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
