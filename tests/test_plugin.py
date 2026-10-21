import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from storage_assistant.demo import create_demo
from storage_assistant.apps import UI_URI
from storage_assistant.plugin import Plugin, serve
from storage_assistant.policy import opaque

ROOT = Path(__file__).resolve().parent.parent
UI = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = create_demo(Path(self.temp.name).resolve() / "fixture")
        self.choice = str(self.fixture)
        self.opened = []
        self.plugin = Plugin(chooser=lambda: self.choice, opener=lambda url: self.opened.append(url) or True)
        self.next_id = 0

    def tearDown(self):
        self.plugin.close()
        self.temp.cleanup()

    def rpc(self, method, params=None):
        self.next_id += 1
        return self.plugin.protocol.handle({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})

    def tool(self, name, args=None):
        return self.rpc("tools/call", {"name": name, "arguments": args or {}})["result"]["structuredContent"]

    def scanned(self, capabilities=None):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": capabilities or {}})
        grant = self.tool("storage_choose_folder")["data"]["scopes"][0]["grantId"]
        scan = self.start_result = self.tool("storage_start_scan", {"grantId": grant, "requestId": opaque()})["data"]
        for _ in range(100):
            status = self.tool("storage_scan_status", {"scanId": scan["scanId"]})["data"]
            if status["state"] != "running":
                return status
            time.sleep(.05)
        self.fail("scan did not finish")

    def test_catalog_adds_picker_and_always_advertises_ui(self):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("storage_choose_folder", tools)
        self.assertEqual(list(tools["storage_choose_folder"]["inputSchema"]["properties"]), ["path"])
        self.assertFalse(tools["storage_choose_folder"]["inputSchema"]["additionalProperties"])
        self.assertEqual(tools["storage_render_view"]["_meta"]["ui"]["resourceUri"], UI_URI)
        self.assertEqual(tools["storage_start_scan"]["_meta"]["ui"]["resourceUri"], UI_URI)
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools.values()))
        self.assertEqual(self.rpc("resources/list")["result"]["resources"][0]["uri"], UI_URI)
        plain = Plugin(chooser=lambda: None, opener=lambda url: True)
        try:
            plain.protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            listed = plain.protocol.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
            render = next(t for t in listed if t["name"] == "storage_render_view")
            self.assertEqual(render["_meta"]["ui"]["resourceUri"], UI_URI)
        finally:
            plain.close()

    def test_picker_flow_returns_names_paths_and_treemap(self):
        status = self.scanned(UI)
        self.assertEqual(status["state"], "partial")  # start_scan waited for the worker to finish
        self.assertEqual(self.start_result["view"]["within"]["displayLabel"], "fixture")  # treemap attached
        self.assertEqual(self.start_result["view"]["rows"][0]["displayLabel"], "Films")
        summary = self.start_result["coverageSummary"]
        self.assertTrue(summary.startswith("Everything reachable was measured."), summary)
        self.assertIn("1 links not followed", summary)
        self.assertNotIn("limit", summary)
        self.assertEqual(status["logicalBytes"], "37041234")
        top = self.tool("storage_query", {"scanId": status["scanId"], "scanVersion": status["scanVersion"],
                                          "basis": "logical", "aggregate": "largest_files", "pageSize": 3})["data"]["rows"]
        self.assertEqual((top[0]["displayLabel"], top[0]["path"], top[0]["bytes"]), ("sparse.mov", "Films/sparse.mov", "25000000"))
        view = self.tool("storage_render_view", {"scanId": status["scanId"], "scanVersion": status["scanVersion"],
                                                 "basis": "logical", "selectionIds": [top[0]["nodeId"]]})
        self.assertTrue(view["ok"])
        self.assertIn("launch.mov", [r["displayLabel"] for r in view["data"]["view"]["rows"]])
        self.assertEqual(view["data"]["view"]["within"]["path"], "Films")
        self.assertEqual(view["data"]["view"]["breadcrumbs"][0]["displayLabel"], "fixture")
        # Relative paths only: the absolute location of the chosen folder is never returned.
        self.assertNotIn(self.temp.name, json.dumps(view))
        self.assertEqual(self.plugin.engine.view["selectionIds"], [top[0]["nodeId"]])

    def test_plugin_grant_uses_larger_limits(self):
        self.rpc("initialize")
        limits = self.tool("storage_choose_folder")["data"]["scopes"][0]["limits"]
        self.assertEqual(limits, {"maxEntries": 1000000, "maxDepth": 128, "maxDurationMs": 900000})

    def test_model_cannot_shrink_scan_limits(self):
        self.rpc("initialize")
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertNotIn("limits", tools["storage_start_scan"]["inputSchema"]["properties"])
        grant = self.tool("storage_choose_folder")["data"]["scopes"][0]["grantId"]
        scan = self.tool("storage_start_scan", {"grantId": grant, "requestId": opaque(),
                                                "limits": {"maxEntries": 2, "maxDepth": 1, "maxDurationMs": 1}})["data"]
        self.assertEqual(scan["limits"]["maxDurationMs"], 900000)
        self.assertNotIn("time_limit", scan["gapCounts"])

    def test_full_coverage_counts_dev_folders_but_skips_credentials(self):
        for rel, size in (("project/node_modules/pkg/index.js", 5000), ("project/.git/objects/pack.pack", 7000),
                          (".ssh/id_ed25519", 400), ("notes/.env", 50)):
            path = self.fixture / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\0" * size)
        status = self.scanned()
        nodes = self.plugin.engine.scan["nodes"]
        names = {n["name"] for n in nodes.values()}
        self.assertTrue({"node_modules", ".git", "1 small code file", "1 small other file"} <= names)
        self.assertNotIn("id_ed25519", names)
        project = next(n for n in nodes.values() if n["name"] == "project")
        self.assertEqual(project["logicalBytes"], "12000")  # small files are grouped, never dropped
        self.assertEqual(status["gapCounts"]["excluded"], 2)  # .ssh and notes/.env
        self.assertIn("credential folders and keys skipped", status["coverageSummary"])

    def test_cancelled_picker_selects_nothing(self):
        self.choice = None
        self.rpc("initialize")
        result = self.tool("storage_choose_folder")
        self.assertEqual(result["error"]["code"], "NOT_SELECTED")
        self.assertEqual(self.tool("storage_list_scopes")["data"]["scopes"], [])

    def test_named_folder_skips_the_picker(self):
        self.choice = None  # the picker would cancel; a named path must not need it
        self.rpc("initialize")
        result = self.tool("storage_choose_folder", {"path": str(self.fixture / "Films")})
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.tool("storage_list_scopes")["data"]["scopes"]), 1)

    def test_named_folder_policy_refusals(self):
        self.rpc("initialize")
        for path, code in (("/", "PROTECTED"), ("~", "INVALID_INPUT"), ("~/", "PROTECTED"), ("/System", "PROTECTED"),
                           ("~/Library", "PROTECTED"), (str(self.fixture / "Films" / ".." / "Sound"), "NOT_AUTHORIZED"),
                           (str(self.fixture / "Films" / "link"), "NOT_AUTHORIZED"), ("Downloads", "INVALID_INPUT"),
                           ("x" * 1025, "INVALID_INPUT")):
            with self.subTest(path=path[:40]):
                self.assertEqual(self.tool("storage_choose_folder", {"path": path})["error"]["code"], code)
        self.assertEqual(self.tool("storage_choose_folder", {"path": "/tmp", "extra": 1})["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.tool("storage_list_scopes")["data"]["scopes"], [])

    def test_local_view_pairs_via_fragment_without_returning_secret(self):
        self.rpc("initialize")
        result = self.tool("storage_open_local_view")
        self.assertTrue(result["data"]["opened"])
        self.assertEqual(self.opened, [f"{self.plugin.companion.url}/#pair={self.plugin.companion.pairing}"])
        self.assertNotIn(self.plugin.companion.pairing, json.dumps(result))

    def test_serve_loop_round_trips_json_lines(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        stdin = io.BytesIO(b"".join(json.dumps(l).encode() + b"\n" for l in lines))
        stdout = io.StringIO()
        serve(stdin, stdout, chooser=lambda: None, opener=lambda url: True)
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertIn("storage_choose_folder", replies[0]["result"]["instructions"])


class PluginProcessTests(unittest.TestCase):
    def test_real_stdio_subprocess(self):
        process = subprocess.run([sys.executable, str(ROOT / "run.py"), "plugin"], cwd=ROOT, capture_output=True,
                                 text=True, timeout=20, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
                                 input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                                       '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"storage_list_scopes","arguments":{}}}\n')
        replies = [json.loads(l) for l in process.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "storage-assistant")
        self.assertEqual(replies[1]["result"]["structuredContent"]["data"]["scopes"], [])

    def test_plugin_mode_refuses_root_argument(self):
        process = subprocess.run([sys.executable, str(ROOT / "run.py"), "plugin", "--root", "/"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=20)
        self.assertNotEqual(process.returncode, 0)




class GroupingTests(unittest.TestCase):
    def test_many_small_files_fit_under_the_item_limit_with_exact_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "many"
            expected = 0
            for d in range(30):
                folder = root / f"pkg{d}" / "lib"
                folder.mkdir(parents=True)
                for f in range(100):
                    (folder / f"m{f}.js").write_bytes(b"x" * (f + 1))
                    expected += f + 1
            (root / "big.mov").write_bytes(b"\0" * 2_000_000)
            expected += 2_000_000
            from storage_assistant import plugin as plugin_module
            original = plugin_module.PLUGIN_LIMITS
            plugin_module.PLUGIN_LIMITS = dict(original, maxEntries=200)  # 3,031 files but only ~100 stored entries
            try:
                p = Plugin(chooser=lambda: str(root), opener=lambda url: True)
                try:
                    grant = p.invoke("storage_choose_folder", {})["structuredContent"]["data"]["scopes"][0]["grantId"]
                    data = p.invoke("storage_start_scan", {"grantId": grant, "requestId": opaque()})["structuredContent"]["data"]
                    while data["state"] == "running":
                        data = p.invoke("storage_scan_status", {"scanId": data["scanId"]})["structuredContent"]["data"]
                    self.assertEqual(data["state"], "complete", data["gapCounts"])
                    self.assertEqual(data["logicalBytes"], str(expected))
                    self.assertLess(data["observedEntries"], 200)
                    rows = p.invoke("storage_query", {"scanId": data["scanId"], "scanVersion": data["scanVersion"],
                                                      "basis": "logical", "aggregate": "largest_files", "pageSize": 3})["structuredContent"]["data"]["rows"]
                    self.assertEqual(rows[0]["displayLabel"], "big.mov")
                    self.assertEqual(rows[1]["displayLabel"], "100 small code files")
                    self.assertEqual(rows[1]["groupedFiles"], 100)
                finally:
                    p.close()
            finally:
                plugin_module.PLUGIN_LIMITS = original


if __name__ == "__main__":
    unittest.main()
