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


class TransportTests(unittest.TestCase):
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
