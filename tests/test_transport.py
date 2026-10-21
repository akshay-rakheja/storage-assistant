from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from storage_assistant.engine import Engine
from storage_assistant.policy import opaque
from storage_assistant.server import Companion
from storage_assistant.mcp import load_session

ROOT = Path(__file__).resolve().parent.parent
CLEANUP_ROUTES = ("suggest", "preview", "execute", "settings", "reveal")
DAY = 86400


class LocalClient:
    """Requests from the paired local browser view (Host, Origin and X-Storage-Request as app.js sends them)."""

    def post(self, path, body, **headers):
        defaults = {"Host": self.server.host, "Origin": self.server.url,
                    "Content-Type": "application/json", "X-Storage-Request": "local-ui"}
        if self.cookie:
            defaults["Cookie"] = self.cookie
        defaults.update(headers)
        defaults = {k: v for k, v in defaults.items() if v is not None}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", path, json.dumps(body), defaults)
        response = connection.getresponse()
        data = json.loads(response.read())
        cookie = response.getheader("Set-Cookie")
        status = response.status
        connection.close()
        return status, data, cookie

    def pair(self):
        status, _, cookie = self.post("/api/pair", {"code": self.server.pairing})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0]


class TransportTests(LocalClient, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.folder = self.base / "selected"
        self.folder.mkdir()
        (self.folder / "SYNTHETIC_PRIVATE_NAME.pdf").write_text("synthetic private contents")
        self.server = Companion()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.engine.close()
        self.thread.join()
        self.tmp.cleanup()

    def test_wrong_host_origin_missing_pair_and_bridge_secret(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"Origin": None}, {"X-Storage-Request": None}):
            self.assertEqual(self.post("/api/state", {}, **headers)[0], 403)
        self.assertEqual(self.post("/api/state", {})[0], 401)
        self.assertEqual(self.post("/bridge", {"name": "storage_list_scopes", "arguments": {}}, Origin=None)[0], 403)
        self.assertEqual(self.post("/api/pair", {"code": "wrong"})[0], 403)
        self.assertEqual(self.post("/api/pair", {"code": "秘密"})[0], 403)
        self.assertEqual(self.post("/bridge", {"name": "storage_list_scopes", "arguments": {}}, Origin=None, Authorization="Bearer é")[0], 403)

    def test_local_selection_disclosure_and_private_partition(self):
        self.pair()
        self.assertEqual(self.post("/api/select", {"path": str(self.folder), "disclosureAccepted": False})[0], 400)
        self.assertEqual(self.post("/api/select", {"path": str(self.folder), "disclosureAccepted": True})[0], 200)
        engine = self.server.engine
        engine.start(engine.grant.grant_id, opaque())
        from test_storage import wait
        status = wait(engine)
        local = self.post("/api/state", {})[1]
        self.assertIn("SYNTHETIC_PRIVATE_NAME.pdf", json.dumps(local))
        result = self.post("/bridge", {"name": "storage_query", "arguments": {
            "scanId": status["scanId"], "scanVersion": status["scanVersion"], "basis": "logical", "aggregate": "largest_files"}},
            Origin=None, Authorization="Bearer " + self.server.secret)
        self.assertEqual(result[0], 200)
        payload = json.dumps(result[1])
        for forbidden in ("SYNTHETIC_PRIVATE_NAME", str(self.folder), "synthetic private contents", self.server.secret, self.server.pairing):
            self.assertNotIn(forbidden, payload)

    def test_wrong_cookie_and_unknown_path(self):
        self.pair()
        self.assertEqual(self.post("/api/state", {}, Cookie="storage_session=wrong")[0], 401)
        self.assertEqual(self.post("/api/../../etc", {})[0], 400)
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", "/../../etc/passwd")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        response.read()
        connection.close()

    def test_local_cleanup_routes_require_pairing(self):
        bodies = {"suggest": {"scanId": "0" * 32, "scanVersion": 1},
                  "preview": {"scanId": "0" * 32, "scanVersion": 1, "nodeIds": ["0" * 32], "action": "trash"},
                  "execute": {"token": "0" * 32}, "settings": {"optIn": {"gitHealth": True}},
                  "reveal": {"scanId": "0" * 32, "scanVersion": 1, "nodeId": "0" * 32, "quickLook": False}}
        for route in CLEANUP_ROUTES:
            self.assertEqual(self.post("/api/cleanup/" + route, bodies[route])[0], 401, route)
            self.assertEqual(self.post("/api/cleanup/" + route, bodies[route], **{"X-Storage-Request": None})[0], 403, route)
        self.pair()
        for route in CLEANUP_ROUTES:  # serve/demo companions have no cleanup service
            self.assertEqual(self.post("/api/cleanup/" + route, bodies[route])[0], 404, route)

    def test_real_stdio_subprocess_initialization_and_call(self):
        descriptor = self.base / "session.json"
        descriptor.write_text(json.dumps(self.server.descriptor()))
        descriptor.chmod(0o600)
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "storage_list_scopes", "arguments": {}}}]
        result = subprocess.run([sys.executable, str(ROOT / "run.py"), "attach", "--session-file", str(descriptor)],
            input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True,
            timeout=5, env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(len(replies[1]["result"]["tools"]), 8)
        self.assertEqual(replies[2]["result"]["structuredContent"]["data"]["scopes"], [])

    def test_descriptor_mode_links_and_nonlocal_endpoint_fail(self):
        descriptor = self.base / "session.json"
        descriptor.write_text(json.dumps(self.server.descriptor()))
        descriptor.chmod(0o644)
        with self.assertRaises(Exception):
            load_session(descriptor)
        descriptor.chmod(0o600)
        os.symlink(descriptor, self.base / "link")
        with self.assertRaises(Exception):
            load_session(self.base / "link")
        descriptor.write_text(json.dumps({"url": "https://attacker.example", "secret": "no"}))
        with self.assertRaises(Exception):
            load_session(descriptor)

    def test_offline_attach_returns_unavailable_without_data(self):
        descriptor = self.base / "missing.json"
        request = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "storage_list_scopes", "arguments": {}}}]
        result = subprocess.run([sys.executable, str(ROOT / "run.py"), "attach", "--session-file", str(descriptor)], input="".join(json.dumps(r) + "\n" for r in request), capture_output=True, text=True, timeout=3)
        payload = json.loads(result.stdout.splitlines()[-1])["result"]["structuredContent"]
        self.assertEqual(payload["error"]["code"], "UNAVAILABLE")
        self.assertNotIn(str(descriptor), result.stdout)

    def test_actual_demo_command_and_graceful_fixture_cleanup(self):
        descriptor = self.base / "demo-session.json"
        before = set(self.base.iterdir())
        process = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "demo", "--session-file", str(descriptor)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
        try:
            session = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    session = load_session(descriptor)
                    break
                except Exception:
                    time.sleep(.01)
            self.assertIsNotNone(session, "demo command did not start")
            connection = HTTPConnection("127.0.0.1", int(session["url"].rsplit(":", 1)[1]), timeout=2)
            connection.request("POST", "/bridge", json.dumps({"name": "storage_list_scopes", "arguments": {}}),
                               {"Content-Type": "application/json", "Authorization": "Bearer " + session["secret"]})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.loads(response.read())["structuredContent"]["data"]["scopes"]), 1)
            connection.close()
        finally:
            process.send_signal(__import__('signal').SIGINT)
            _, errors = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(errors, "")
        self.assertEqual(set(self.base.iterdir()), before)


class LocalCleanupTests(LocalClient, unittest.TestCase):
    """The paired local view's cleanup routes, end to end through the plugin's own companion."""

    def setUp(self):
        from storage_assistant.executor import FakeExecutor
        from storage_assistant.plugin import Plugin
        from test_cleanup import FakeChecks, FakeSpotlight
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "chosen"
        self.trash = self.base / "FakeTrash"
        (self.base / "Applications").mkdir()
        self.trash.mkdir()
        old = time.time() - 60 * DAY
        for name, size in (("a.crdownload", 3000), ("b.part", 5000), ("keep/notes.zip", 100)):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
            os.utime(path, (old, old))
        for d in (self.root / "keep", self.root):
            os.utime(d, (old, old))
        self.plugin = Plugin(chooser=lambda: str(self.root), opener=lambda url: True)
        cleanup = self.plugin.cleanup
        self.executor = cleanup.executor = FakeExecutor(str(self.trash))
        cleanup.spotlight, cleanup.checks = FakeSpotlight(), FakeChecks()
        cleanup.applications = (str(self.base / "Applications"),)
        self.server = self.plugin.companion
        self.revealed = []
        self.server.reveal = lambda path, quick_look: self.revealed.append((path, quick_look))
        self.cookie = None
        self.call("storage_choose_folder", {})
        self.status = self.scan()

    def tearDown(self):
        self.plugin.close()
        self.tmp.cleanup()

    def call(self, name, args):
        body = self.plugin.invoke(name, args)["structuredContent"]
        self.assertTrue(body["ok"], body)
        return body["data"]

    def scan(self):
        grant = self.plugin.engine.grant.grant_id
        status = self.call("storage_start_scan", {"grantId": grant, "requestId": opaque()})
        for _ in range(400):
            if status["state"] != "running":
                return status
            time.sleep(.05)
            status = self.call("storage_scan_status", {"scanId": status["scanId"]})
        self.fail("scan did not finish")

    def node_id(self, relative):
        engine = self.plugin.engine
        with engine.lock:
            return next(n["nodeId"] for n in engine.scan["nodes"].values() if engine.relative_path(n) == relative)

    def scan_args(self, **extra):
        return dict({"scanId": self.status["scanId"], "scanVersion": self.status["scanVersion"]}, **extra)

    def test_unpaired_cleanup_routes_are_refused(self):
        plan = self.plugin.cleanup.preview(self.status["scanId"], self.status["scanVersion"],
                                           [self.node_id("a.crdownload")], "trash")
        bodies = {"suggest": self.scan_args(), "execute": {"token": plan["token"]},
                  "preview": self.scan_args(nodeIds=[self.node_id("a.crdownload")], action="trash"),
                  "settings": {"optIn": {"gitHealth": True}, "openTrash": True},
                  "reveal": self.scan_args(nodeId=self.node_id("a.crdownload"), quickLook=True)}
        for route in CLEANUP_ROUTES:
            self.assertEqual(self.post("/api/cleanup/" + route, bodies[route])[0], 401, route)
            self.assertEqual(self.post("/api/cleanup/" + route, bodies[route], Cookie="storage_session=wrong")[0], 401, route)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.revealed, [])
        self.assertTrue((self.root / "a.crdownload").exists())
        self.assertEqual(self.plugin.cleanup.opt_in, set())
        self.assertEqual(len(self.plugin.cleanup.execute(plan["token"])["done"]), 1)  # the token was not spent

    def test_paired_suggest_preview_execute_receipt(self):
        self.pair()
        status, body, _ = self.post("/api/cleanup/suggest", self.scan_args())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        found = {c["path"]: c for c in body["data"]["candidates"]}
        self.assertEqual(found["a.crdownload"]["tier"], "safe")
        ids = [found["a.crdownload"]["nodeId"], found["b.part"]["nodeId"]]
        status, body, _ = self.post("/api/cleanup/preview", self.scan_args(nodeIds=ids, action="trash"))
        self.assertEqual(status, 200, body)
        plan = body["data"]
        self.assertEqual((plan["action"], plan["count"], len(plan["token"])), ("trash", 2, 32))
        self.assertEqual(self.executor.calls, [])  # preview changes nothing
        status, body, _ = self.post("/api/cleanup/execute", {"token": plan["token"]})
        self.assertEqual(status, 200, body)
        receipt = body["data"]
        self.assertEqual(receipt["action"], "trash")
        self.assertEqual(sorted(d["path"] for d in receipt["done"]), ["a.crdownload", "b.part"])
        self.assertEqual((receipt["skipped"], receipt["failed"]), ([], []))
        self.assertTrue(receipt["putBack"])
        self.assertEqual(receipt["trashNote"], "Space is freed when you empty the Trash.")
        self.assertEqual(sorted(os.listdir(self.trash)), ["a.crdownload", "b.part"])
        self.assertTrue(receipt["rescan"]["started"])
        calls = list(self.executor.calls)
        status, body, _ = self.post("/api/cleanup/execute", {"token": plan["token"]})  # single use
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "INVALID_INPUT")
        self.assertEqual(self.executor.calls, calls)

    def test_paired_delete_and_bad_requests(self):
        self.pair()
        node = self.node_id("keep")
        status, body, _ = self.post("/api/cleanup/preview", self.scan_args(nodeIds=[node], action="delete"))
        self.assertEqual(status, 200, body)  # not suggested for permanent deletion: left out, no token
        self.assertIsNone(body["data"]["token"])
        self.assertEqual(body["data"]["skipped"][0]["reason"], "Not offered for this action")
        for route, args in (("preview", self.scan_args(nodeIds=[node], action="rm -rf")),
                            ("preview", self.scan_args(nodeIds=[node], action="trash", extra=1)),
                            ("suggest", {"scanId": self.status["scanId"], "scanVersion": "1"}),
                            ("execute", {"token": 5}),
                            ("settings", {"optIn": {"unknown": True}}),
                            ("reveal", self.scan_args(nodeId=node, quickLook="yes")),
                            ("unknown", {})):
            status, body, _ = self.post("/api/cleanup/" + route, args)
            self.assertIn(status, (400, 404), route)
        self.assertEqual(self.executor.calls, [])
        status, body, _ = self.post("/api/cleanup/preview", dict(self.scan_args(nodeIds=[node], action="trash"), scanVersion=99))
        self.assertEqual((status, body["code"]), (400, "STALE_SCAN"))

    def test_settings_switch_opt_ins(self):
        self.pair()
        status, body, _ = self.post("/api/cleanup/settings", {"optIn": {"gitHealth": True}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["optIn"], {"gitHealth": True, "exactDuplicates": False})
        status, body, _ = self.post("/api/cleanup/settings", {"optIn": {}})
        self.assertEqual(body["data"]["optIn"], {"gitHealth": True, "exactDuplicates": False})
        self.post("/api/cleanup/settings", {"optIn": {"gitHealth": False}})
        self.assertEqual(self.plugin.cleanup.opt_in, set())

    def test_reveal_and_quick_look_revalidate(self):
        self.pair()
        args = self.scan_args(nodeId=self.node_id("keep/notes.zip"), quickLook=False)
        status, body, _ = self.post("/api/cleanup/reveal", args)
        self.assertEqual((status, body["ok"]), (200, True), body)
        status, _, _ = self.post("/api/cleanup/reveal", dict(args, quickLook=True))
        self.assertEqual(status, 200)
        path = str(self.root / "keep/notes.zip")
        self.assertEqual(self.revealed, [(path, False), (path, True)])
        self.assertNotIn(path, json.dumps(body))  # the path stays in the executor
        status, body, _ = self.post("/api/cleanup/reveal", self.scan_args(nodeId=self.plugin.engine.scan["rootNodeId"], quickLook=False))
        self.assertEqual(status, 400)
        (self.root / "keep/notes.zip").write_bytes(b"changed")  # no longer the scanned item
        status, body, _ = self.post("/api/cleanup/reveal", args)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "This item changed since the scan. Scan again to see it.")
        (self.root / "keep/notes.zip").unlink()
        os.symlink("/etc", self.root / "keep/notes.zip")
        self.assertEqual(self.post("/api/cleanup/reveal", args)[0], 400)
        self.assertEqual(len(self.revealed), 2)

    def test_locate_returns_revalidated_absolute_path(self):
        cleanup = self.plugin.cleanup
        node = self.node_id("keep/notes.zip")
        self.assertEqual(cleanup.locate(self.status["scanId"], self.status["scanVersion"], node),
                         str(self.root / "keep/notes.zip"))
        from storage_assistant.policy import StorageError
        with self.assertRaises(StorageError) as caught:
            cleanup.locate(self.status["scanId"], self.status["scanVersion"] + 1, node)
        self.assertEqual(caught.exception.code, "STALE_SCAN")
        with self.assertRaises(StorageError):
            cleanup.locate(self.status["scanId"], self.status["scanVersion"], "0" * 32)


class RevealCommandTests(unittest.TestCase):
    def test_fixed_argv_without_shell(self):
        from unittest import mock
        from storage_assistant import macos
        with mock.patch.object(macos.subprocess, "run") as run, mock.patch.object(macos.subprocess, "Popen") as popen:
            macos.reveal("/x/-n; rm y", False)
            macos.reveal("/x/-n; rm y", True)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-R", "/x/-n; rm y"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIn("timeout", run.call_args.kwargs)
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/qlmanage", "-p", "/x/-n; rm y"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        with self.assertRaises(ValueError):
            macos.reveal("relative/path", False)
