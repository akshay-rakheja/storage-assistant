import errno
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from storage_assistant.contracts import QUERY, validate, tools_catalog
from storage_assistant.demo import create_demo
from storage_assistant.engine import Engine
from storage_assistant.mcp import Protocol, call, load_session
from storage_assistant.policy import DATALESS, Grant, StorageError, identity, opaque, placeholder
from storage_assistant.worker import walk


def wait(engine, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = engine.status(engine.scan["scanId"])
        if state["state"] != "running":
            return state
        time.sleep(.01)
    raise AssertionError("scan did not finish")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "selected"
        self.root.mkdir()
        self.engine = Engine()

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def scan(self, limits=None):
        self.engine.select(str(self.root))
        self.engine.start(self.engine.grant.grant_id, opaque(), limits)
        return wait(self.engine)

    def query(self, **args):
        defaults = {"scanId": self.engine.scan["scanId"], "scanVersion": self.engine.scan["scanVersion"],
                    "basis": "logical", "aggregate": "largest_files", "pageSize": 100}
        defaults.update(args)
        return self.engine.query(defaults)

    def events(self, **kwargs):
        _, fd, _ = __import__('storage_assistant.policy', fromlist=['open_root']).open_root(str(self.root))
        result = []
        try:
            walk(fd, {"maxEntries": 100, "maxDepth": 8, "maxDurationMs": 3000}, result.append, **kwargs)
        finally:
            os.close(fd)
        return result


class AccountingTests(Fixture):
    def test_exact_hidden_sparse_hardlinks_and_unchanged_target(self):
        (self.root / "b.pdf").write_bytes(b"a" * 1234)
        os.link(self.root / "b.pdf", self.root / "a.pdf")
        (self.root / ".hidden").write_bytes(b"b" * 50)
        with (self.root / "sparse.mov").open("wb") as stream:
            stream.truncate(1_000_000)
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.iterdir()}
        status = self.scan()
        self.assertEqual(status["logicalBytes"], "1001284")
        self.assertLess(int(status["allocatedEstimateBytes"]), int(status["logicalBytes"]))
        local = self.engine.local_state()["nodes"]
        a = next(n for n in local if n["displayLabel"] == "a.pdf")
        b = next(n for n in local if n["displayLabel"] == "b.pdf")
        self.assertEqual(a["logicalBytes"], "1234")
        self.assertEqual(b["logicalBytes"], "0")
        self.assertEqual(b["hardlinkAliasOf"], a["nodeId"])
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(status["coverage"], "complete")

    def test_empty_and_directory_overhead_not_counted(self):
        (self.root / "empty").mkdir()
        status = self.scan()
        self.assertEqual(status["logicalBytes"], "0")
        self.assertEqual(status["allocatedEstimateBytes"], "0")

    def test_categories_sum_without_directory_double_counting(self):
        (self.root / "media").mkdir()
        (self.root / "media/movie.mov").write_bytes(b"x" * 800)
        (self.root / "note.pdf").write_bytes(b"x" * 100)
        self.scan()
        result = self.query(aggregate="by_category")
        self.assertEqual(sum(int(r["bytes"]) for r in result["rows"]), 900)
        self.assertEqual(result["rows"][0]["displayLabel"], "video")

    def test_large_integer_query_no_float_rounding(self):
        (self.root / "data.bin").touch()
        self.scan()
        n = next(n for n in self.engine.scan["nodes"].values() if n["kind"] == "file")
        n["rawLogicalBytes"] = "9007199254740993"
        self.engine.scan.pop("aggregatedFinal", None)  # synthetic measurement injection
        result = self.query(predicate={"minBytes": "9007199254740993"})
        self.assertEqual(result["rows"][0]["bytes"], "9007199254740993")


class BoundaryTests(Fixture):
    def test_escape_cycle_dangling_links_and_no_file_opens(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_bytes(b"secret")
        (self.root / "visible.txt").write_bytes(b"x")
        os.symlink(outside, self.root / "escape")
        os.symlink(self.root, self.root / "cycle")
        os.symlink("missing", self.root / "dangling")
        opened = []
        original = os.open
        def capture(path, flags, *args, **kwargs):
            opened.append((path, flags))
            return original(path, flags, *args, **kwargs)
        with patch("os.open", capture):
            events = self.events()
        names = [e["name"] for e in events if e["event"] == "node"]
        self.assertNotIn("private.txt", names)
        self.assertEqual(sum(e.get("reason") == "symlink" for e in events), 3)
        self.assertTrue(all(flags & os.O_DIRECTORY for _, flags in opened))

    def test_root_and_ancestor_links_rejected(self):
        os.symlink(self.root, self.base / "alias")
        for path in (str(self.base / "alias"), str(self.base / "alias/child"), "/", str(Path.home()), str(self.root / "../outside"), "/private/var/db", "/private/var"):
            with self.assertRaises(StorageError):
                Grant.select(path)

    def test_identity_replacement_revokes(self):
        self.scan()
        self.root.rename(self.base / "old")
        self.root.mkdir()
        with self.assertRaises(StorageError):
            self.engine.status(self.engine.scan["scanId"])
        self.assertIsNone(self.engine.grant)
        self.assertIsNone(self.engine.scan)

    def test_symlink_swap_before_directory_open_never_follows(self):
        child = self.root / "child"
        child.mkdir()
        (self.base / "outside").mkdir()
        (self.base / "outside/private.txt").write_text("secret")
        original = os.open
        def swap(path, flags, *args, **kwargs):
            if path == "child":
                child.rename(self.root / "moved")
                os.symlink(self.base / "outside", child)
            return original(path, flags, *args, **kwargs)
        with patch("os.open", swap):
            events = self.events()
        self.assertNotIn("private.txt", [e.get("name") for e in events])
        self.assertIn("changed", [e.get("reason") for e in events])

    def test_permissions_and_no_retry(self):
        (self.root / "denied").mkdir()
        original = os.open
        calls = []
        def deny(path, flags, *args, **kwargs):
            if path == "denied":
                calls.append(path)
                raise PermissionError(errno.EACCES, "private-path-not-for-output")
            return original(path, flags, *args, **kwargs)
        with patch("os.open", deny):
            events = self.events()
        self.assertEqual(calls, ["denied"])
        self.assertIn("denied", [e.get("reason") for e in events])

    def test_actual_denied_directory(self):
        denied = self.root / "denied"
        denied.mkdir()
        (denied / "hidden").write_text("synthetic")
        denied.chmod(0)
        try:
            status = self.scan()
            if os.getuid() == 0:
                self.skipTest("root bypasses fixture mode bits")
            self.assertEqual(status["gapCounts"]["denied"], 1)
            self.assertEqual(status["coverage"], "partial")
        finally:
            denied.chmod(0o700)

    def test_exclusions_special_files_and_cloud_sidecar(self):
        (self.root / ".git").mkdir()
        (self.root / ".git/private").write_text("synthetic")
        (self.root / ".env").write_text("synthetic")
        (self.root / "cloud.icloud").write_text("synthetic")
        os.mkfifo(self.root / "pipe")
        status = self.scan()
        self.assertEqual(status["logicalBytes"], "0")
        self.assertEqual(status["gapCounts"]["excluded"], 2)
        self.assertEqual(status["gapCounts"]["cloud_placeholder"], 1)
        self.assertEqual(status["gapCounts"]["unsupported"], 1)

    def test_mock_dataless_directory_is_not_opened(self):
        (self.root / "provider").mkdir()
        original = os.stat
        def dataless(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            if path == "provider":
                return types.SimpleNamespace(**({k: getattr(st, k) for k in dir(st) if k.startswith("st_")} | {"st_flags": DATALESS}))
            return st
        with patch("os.open", wraps=os.open) as opened:
            events = self.events(metadata=dataless)
        self.assertFalse(any(c.args[0] == "provider" for c in opened.call_args_list))
        self.assertIn("cloud_placeholder", [e.get("reason") for e in events])

    def test_unknown_provider_and_allocation_are_explicit(self):
        (self.root / "file").touch()
        original = os.stat
        def unsupported(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            return types.SimpleNamespace(**{k: getattr(st, k) for k in dir(st) if k.startswith("st_") and k not in ("st_flags", "st_blocks")})
        events = self.events(metadata=unsupported)
        self.assertIn("provider_unknown", [e.get("reason") for e in events])
        self.assertIn("allocation_unknown", [e.get("reason") for e in events])
        self.assertEqual(placeholder(types.SimpleNamespace()), "unknown")

    def test_mock_mount_not_opened(self):
        (self.root / "mounted").mkdir()
        original = os.stat
        def mounted(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            return types.SimpleNamespace(**({k: getattr(st, k) for k in dir(st) if k.startswith("st_")} | {"st_dev": st.st_dev + 1})) if path == "mounted" else st
        with patch("os.open", wraps=os.open) as opened:
            events = self.events(metadata=mounted)
        self.assertFalse(any(c.args[0] == "mounted" for c in opened.call_args_list))
        self.assertIn("other_volume", [e.get("reason") for e in events])


class LifecycleTests(Fixture):
    def test_worker_self_deadline_without_supervisor(self):
        from storage_assistant.policy import open_root
        _, fd, _ = open_root(str(self.root))
        script = "import sys,time; from storage_assistant import worker; worker.walk=lambda *a,**k: time.sleep(20); sys.argv=['worker','--fd',sys.argv[1],'--limits',sys.argv[2]]; worker.main()"
        start = time.monotonic()
        try:
            process = subprocess.Popen([sys.executable, "-c", script, str(fd), json.dumps({"maxDurationMs": 50})], pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            process.communicate(timeout=2)
            self.assertEqual(process.returncode, 124)
            self.assertLess(time.monotonic() - start, 1)
        finally:
            os.close(fd)

    def test_entry_depth_and_soft_duration_budgets(self):
        for n in range(20):
            (self.root / f"file{n}").touch()
        status = self.scan({"maxEntries": 4})
        self.assertEqual(status["observedEntries"], 4)
        self.assertIn("entry_limit", status["gapCounts"])
        (self.root / "deep").mkdir()
        (self.root / "deep/unseen").mkdir()
        status = self.scan({"maxDepth": 1})
        self.assertIn("depth_limit", status["gapCounts"])
        ticks = iter([0, 0, 5, 5, 5, 5])
        events = self.events(now=lambda: next(ticks, 5))
        self.assertIn("time_limit", [e.get("reason") for e in events])

    def test_hard_deadline_kills_a_real_stalled_process(self):
        self.engine.select(str(self.root))
        original = subprocess.Popen
        def stalled(*args, **kwargs):
            return original([sys.executable, "-c", "import time; time.sleep(20)"], **kwargs)
        start = time.monotonic()
        with patch("storage_assistant.engine.subprocess.Popen", stalled):
            self.engine.start(self.engine.grant.grant_id, opaque(), {"maxDurationMs": 50})
            process = self.engine.process
            result = wait(self.engine)
        self.assertLess(time.monotonic() - start, 1)
        self.assertIn("metadata_delayed", result["gapCounts"])
        self.assertIsNotNone(process.poll())

    def test_cancel_acknowledgement_and_no_continuation(self):
        self.engine.select(str(self.root))
        original = subprocess.Popen
        def stalled(*args, **kwargs):
            return original([sys.executable, "-c", "import time; time.sleep(20)"], **kwargs)
        with patch("storage_assistant.engine.subprocess.Popen", stalled):
            self.engine.start(self.engine.grant.grant_id, opaque())
            process = self.engine.process
            start = time.monotonic()
            result = self.engine.cancel(self.engine.scan["scanId"])
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(result["coverage"], "cancelled")
        self.assertIsNotNone(process.poll())

    def test_rescan_old_ids_and_request_conflict(self):
        self.scan()
        old = self.engine.scan["scanId"]
        request = opaque()
        self.engine.start(self.engine.grant.grant_id, request)
        current = wait(self.engine)
        self.assertEqual(self.engine.start(self.engine.grant.grant_id, request)["scanId"], current["scanId"])
        with self.assertRaises(StorageError):
            self.engine.start(self.engine.grant.grant_id, request, {"maxEntries": 1})
        with self.assertRaises(StorageError):
            self.engine.status(old)

    def test_expiry_wrong_grant_and_release_purge(self):
        self.scan()
        with self.assertRaises(StorageError):
            self.engine.start(opaque(), opaque())
        self.engine.grant.expires = 0
        with self.assertRaises(StorageError):
            self.engine.scopes()
        self.assertIsNone(self.engine.scan)
        self.assertEqual(self.engine.scopes()["scopes"], [])


class ContractPrivacyTests(Fixture):
    def test_strict_invalid_paths_commands_and_bounds(self):
        self.scan()
        valid = {"scanId": self.engine.scan["scanId"], "scanVersion": self.engine.scan["scanVersion"], "basis": "logical"}
        for change in ({"path": "../../etc"}, {"pageSize": 101}, {"scanVersion": True}, {"basis": "reclaimable"}, {"predicate": {"name": "secret"}}, {"predicate": {"minBytes": "-1"}}, {"command": "rm -rf"}, {"scanId": "../../outside"}):
            result = call(self.engine, "storage_query", valid | change, "http://127.0.0.1:1")
            self.assertTrue(result["isError"], change)
        self.assertTrue(call(self.engine, "storage_trash", {}, "http://127.0.0.1:1")["isError"])

    def test_payloads_omit_names_paths_identity_tokens_and_contents(self):
        private = 'client-secret-$(touch HACKED)-\n<script>.pdf'
        (self.root / private).write_text("PRIVATE_SYNTHETIC_CONTENT")
        self.scan()
        result = self.query()
        for output in (result, self.engine.status(self.engine.scan["scanId"]), call(self.engine, "storage_list_scopes", {}, "http://127.0.0.1:1")):
            text = json.dumps(output)
            for forbidden in (str(self.root), private, "PRIVATE_SYNTHETIC_CONTENT", '"modifiedNs":', '"identity":', '"relative":'):
                self.assertNotIn(forbidden, text)
        self.assertFalse((self.root / "HACKED").exists())
        self.assertEqual(result["evidence"]["scanVersion"], self.engine.version)

    def test_cursor_bound_to_query_and_snapshot(self):
        for n in range(4):
            (self.root / str(n)).touch()
        self.scan()
        first = self.query(pageSize=1)
        second = self.query(pageSize=1, cursor=first["nextCursor"])
        self.assertNotEqual(first["rows"][0]["nodeId"], second["rows"][0]["nodeId"])
        with self.assertRaises(StorageError):
            self.query(pageSize=2, cursor=first["nextCursor"])
        self.engine.cursors[first["nextCursor"]] = ("invalid", 1, 0)
        with self.assertRaises(StorageError):
            self.query(pageSize=1, cursor=first["nextCursor"])

    def test_catalog_and_stdio_lifecycle(self):
        catalog = tools_catalog()
        self.assertEqual(len(catalog), 8)
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in catalog))
        p = Protocol(lambda n, a: call(self.engine, n, a, "http://127.0.0.1:1"))
        self.assertIn("error", p.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        initialized = p.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        self.assertIsNone(p.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(len(p.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]), 8)

    def test_scope_does_not_appear_without_local_selection(self):
        self.assertEqual(self.engine.scopes()["scopes"], [])
        self.assertTrue(call(self.engine, "storage_start_scan", {"grantId": opaque(), "requestId": opaque()}, "http://127.0.0.1:1")["isError"])

    def test_render_rejects_stale_unknown_and_updates_shared_view(self):
        self.scan()
        status = self.engine.status(self.engine.scan["scanId"])
        args = {"scanId": status["scanId"], "scanVersion": status["scanVersion"], "basis": "logical", "selectionIds": [status["rootNodeId"]]}
        result = call(self.engine, "storage_render_view", args, "http://127.0.0.1:1")
        self.assertFalse(result["isError"])
        self.assertEqual(self.engine.local_state()["view"]["selectionIds"], args["selectionIds"])
        self.assertTrue(call(self.engine, "storage_render_view", args | {"selectionIds": [opaque()]}, "http://127.0.0.1:1")["isError"])


if __name__ == "__main__":
    unittest.main()
