import io
import json
import os
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
os.environ["STORAGE_ASSISTANT_UPDATE_CHECK"] = "0"  # plugin processes started by tests stay offline
UI = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}


VIEW_ONLY = ("storage_cleanup_preview", "storage_cleanup_execute", "storage_cleanup_settings", "storage_cleanup_candidates")


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
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for n, t in tools.items() if n not in VIEW_ONLY))
        self.assertEqual(self.rpc("resources/list")["result"]["resources"][0]["uri"], UI_URI)
        plain = Plugin(chooser=lambda: None, opener=lambda url: True)
        try:
            plain.protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            listed = plain.protocol.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
            render = next(t for t in listed if t["name"] == "storage_render_view")
            self.assertEqual(render["_meta"]["ui"]["resourceUri"], UI_URI)
        finally:
            plain.close()

    def test_cleanup_tools_listed_with_visibility(self):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        for name in VIEW_ONLY:
            self.assertEqual(tools[name]["_meta"], {"ui": {"visibility": ["app"]}})
            self.assertTrue(tools[name]["description"].startswith(
                "View-only: called by the Storage Assistant view after the person clicks."))
            # Listing candidates only reads; the other view-only tools change files or settings.
            self.assertEqual(tools[name]["annotations"]["readOnlyHint"], name == "storage_cleanup_candidates")
        suggest = tools["storage_suggest_cleanup"]
        self.assertNotIn("_meta", suggest)
        self.assertTrue(suggest["annotations"]["readOnlyHint"])

    def test_only_scan_and_render_show_a_view(self):
        """One view per turn: a scan plus suggestions in the same turn must not open a second view."""
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        tools = self.rpc("tools/list")["result"]["tools"]
        with_view = {t["name"] for t in tools if t.get("_meta", {}).get("ui", {}).get("resourceUri")}
        self.assertEqual(with_view, {"storage_start_scan", "storage_render_view"})
        for name in with_view:
            schema = next(t for t in tools if t["name"] == name)["inputSchema"]
            self.assertEqual(schema["properties"]["freeUpSpace"], {"type": "boolean"})

    def test_non_apps_client_gets_no_action_tools(self):
        """C1: without MCP Apps there is no view to click, so the model never sees or reaches the action tools."""
        for capabilities in ({}, {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html"]}}},
                             {"extensions": {"io.modelcontextprotocol/ui": {}}}):
            with self.subTest(capabilities=capabilities):
                plain = Plugin(chooser=lambda: self.choice, opener=lambda url: True)
                try:
                    plain.protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-11-25", "capabilities": capabilities}})
                    listed = {t["name"] for t in plain.protocol.handle(
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
                    self.assertFalse(listed & set(VIEW_ONLY), listed)
                    self.assertIn("storage_suggest_cleanup", listed)
                    self.assertIn("storage_open_local_view", listed)
                    for name, args in (("storage_cleanup_preview", {"scanId": "ab" * 16, "scanVersion": 1,
                                                                    "nodeIds": ["cd" * 16], "action": "delete"}),
                                       ("storage_cleanup_execute", {"token": "ab" * 16}),
                                       ("storage_cleanup_settings", {"optIn": {"gitHealth": True}, "openTrash": True})):
                        body = plain.protocol.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                      "params": {"name": name, "arguments": args}})["result"]["structuredContent"]
                        self.assertFalse(body["ok"])
                        self.assertEqual(body["error"]["code"], "UNSUPPORTED")
                        self.assertIn("storage_open_local_view", body["error"]["message"])
                    self.assertEqual(plain.cleanup.opt_in, set())  # the refused settings call changed nothing
                finally:
                    plain.close()

    def test_apps_client_can_call_action_tools(self):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        listed = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue(set(VIEW_ONLY) <= listed)
        body = self.tool("storage_cleanup_settings", {"optIn": {"gitHealth": False}})
        self.assertTrue(body["ok"], body)

    def test_suggest_returns_candidates_without_a_view(self):
        status = self.scanned(UI)
        result = self.tool("storage_suggest_cleanup", {"scanId": status["scanId"], "scanVersion": status["scanVersion"]})
        self.assertTrue(result["ok"], result)
        self.assertIn("candidates", result["data"])
        self.assertNotIn("view", result["data"])

    def test_free_up_space_puts_suggestions_in_the_one_view(self):
        status = self.scanned(UI)
        snapshot = {"scanId": status["scanId"], "scanVersion": status["scanVersion"]}
        shown = self.tool("storage_render_view", snapshot | {"basis": "logical", "freeUpSpace": True})
        self.assertTrue(shown["ok"], shown)
        self.assertIn("view", shown["data"])
        self.assertIn("candidates", shown["data"])
        self.assertTrue(shown["data"]["freeUpSpace"])
        plain = self.tool("storage_render_view", snapshot | {"basis": "logical"})
        self.assertNotIn("candidates", plain["data"])
        grant = self.tool("storage_list_scopes")["data"]["scopes"][0]["grantId"]
        started = self.tool("storage_start_scan", {"grantId": grant, "requestId": opaque(), "freeUpSpace": True})
        self.assertTrue(started["ok"], started)
        self.assertTrue(started["data"]["freeUpSpace"])
        if started["data"]["state"] != "running":
            self.assertIn("candidates", started["data"])

    def test_many_suggestions_still_fit_the_host_output_cap(self):
        # 129 suggestions once made a 65 KB scan result; Claude Code dropped it, view included.
        for i in range(150):
            modules = self.fixture / f"project-{i:03}" / "node_modules" / "pkg"
            modules.mkdir(parents=True)
            (modules / "index.js").write_bytes(b"x" * (1000 + i))
            (modules.parent.parent / "package.json").write_text("{}")
        real = self.plugin.cleanup.clock
        self.plugin.cleanup.clock = lambda: real() + 30 * 86400  # the projects were last touched a month ago
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        grant = self.tool("storage_choose_folder")["data"]["scopes"][0]["grantId"]
        response = self.rpc("tools/call", {"name": "storage_start_scan",
                                           "arguments": {"grantId": grant, "requestId": opaque(), "freeUpSpace": True}})["result"]
        data = response["structuredContent"]["data"]
        self.assertNotEqual(data["state"], "running")
        self.assertIn("view", data)
        self.assertLessEqual(len(data["candidates"]), 20)
        self.assertGreaterEqual(data["candidatesTotal"], 150)
        self.assertIn("candidatesNote", data)
        self.assertLess(len(response["content"][0]["text"]), 60000)
        sizes = [int(c["freesBytes"]) for c in data["candidates"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        snapshot = {"scanId": data["scanId"], "scanVersion": data["scanVersion"]}
        suggested = self.tool("storage_suggest_cleanup", snapshot)
        self.assertLessEqual(len(suggested["data"]["candidates"]), 20)
        # The view fetches every suggestion, and can act on ones the assistant never saw.
        everything = self.tool("storage_cleanup_candidates", snapshot)
        self.assertTrue(everything["ok"], everything)
        self.assertEqual(len(everything["data"]["candidates"]), data["candidatesTotal"])
        self.assertNotIn("candidatesTotal", everything["data"])
        shown = {c["nodeId"] for c in data["candidates"]}
        hidden = next(c for c in everything["data"]["candidates"] if c["nodeId"] not in shown)
        preview = self.tool("storage_cleanup_preview", snapshot | {"nodeIds": [hidden["nodeId"]], "action": "trash"})
        self.assertTrue(preview["ok"], preview)

    def test_bad_request_id_says_what_an_identifier_looks_like(self):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        grant = self.tool("storage_choose_folder")["data"]["scopes"][0]["grantId"]
        started = self.tool("storage_start_scan", {"grantId": grant, "requestId": "docs-scan-1"})
        self.assertFalse(started["ok"])
        self.assertIn("32 lowercase hex", started["error"]["message"])

    def test_person_can_ask_for_optional_checks_in_chat(self):
        status = self.scanned(UI)
        snapshot = {"scanId": status["scanId"], "scanVersion": status["scanVersion"]}
        result = self.tool("storage_suggest_cleanup", snapshot | {"checks": {"exactDuplicates": True}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.plugin.cleanup.opt_in, {"exactDuplicates"})
        self.assertEqual(result["data"]["checks"], {"gitHealth": False, "exactDuplicates": True})

    def test_scan_results_carry_folder_and_date_for_a_lasting_snapshot(self):
        status = self.scanned(UI)
        finished = self.rpc("tools/call", {"name": "storage_scan_status", "arguments": {"scanId": status["scanId"]}})["result"]
        shown = self.rpc("tools/call", {"name": "storage_render_view", "arguments": {
            "scanId": status["scanId"], "scanVersion": status["scanVersion"], "basis": "logical"}})["result"]
        for result in (finished, shown):
            self.assertEqual(result["_meta"]["storageAssistant"]["folder"], self.choice)  # for the view only
            self.assertNotIn(self.choice, json.dumps(result["structuredContent"]))
            self.assertNotIn(self.choice, json.dumps(result["content"]))
        self.assertIsInstance(finished["structuredContent"]["data"]["scannedAt"], (int, float))
        self.assertEqual(shown["structuredContent"]["data"]["scannedAt"], finished["structuredContent"]["data"]["scannedAt"])

    def test_every_reply_about_the_folder_shows_a_view(self):
        from storage_assistant.plugin import PLUGIN_INSTRUCTIONS, RENDER_DESCRIPTION
        self.assertIn("Every reply about the folder shows ONE view", PLUGIN_INSTRUCTIONS)
        self.assertNotIn("do NOT call storage_render_view", PLUGIN_INSTRUCTIONS)
        self.assertIn("every reply", RENDER_DESCRIPTION)

    def test_update_notice_reaches_the_view_only(self):
        from storage_assistant.updates import newer
        self.assertTrue(newer("v0.6.8", "0.6.7"))
        self.assertTrue(newer("v0.10.0", "0.9.9"))
        self.assertFalse(newer("v0.6.7", "0.6.7"))
        self.assertFalse(newer("not-a-version", "0.6.7"))
        self.assertIsNone(self.plugin.updates.available)  # tests and the default never reach the network
        status = self.scanned(UI)
        self.plugin.updates.available = {"version": "9.9.9", "url": "https://example.invalid/x.mcpb"}
        result = self.rpc("tools/call", {"name": "storage_scan_status", "arguments": {"scanId": status["scanId"]}})["result"]
        self.assertEqual(result["_meta"]["storageAssistant"]["update"]["version"], "9.9.9")
        self.assertNotIn("9.9.9", json.dumps(result["structuredContent"]))

    def test_only_tools_that_change_files_are_labelled_destructive(self):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        listing = tools["storage_cleanup_candidates"]
        self.assertEqual(listing["annotations"], {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
        self.assertEqual(listing["_meta"]["ui"]["visibility"], ["app"])  # still for the view only
        for name in ("storage_cleanup_preview", "storage_cleanup_execute", "storage_cleanup_settings"):
            self.assertTrue(tools[name]["annotations"]["destructiveHint"], name)

    def test_execute_via_protocol_requires_preview_token(self):
        self.scanned(UI)
        result = self.tool("storage_cleanup_execute", {"token": "ab" * 16})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_ui_bundle_has_cleanup_controls(self):
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": UI})
        html = self.rpc("resources/read", {"uri": UI_URI})["result"]["contents"][0]["text"]
        for marker in ('id="tray"', 'id="review"', 'id="confirm-delete"', 'id="receipt"',
                       "Move ", " items to Trash", "Delete permanently", "This can't be undone.", "Open Trash",
                       "Safe to clear", "Worth a glance", "Review…", 'id="suggest"', "Suggest what to clear", "Use Put Back in Finder to restore",
                       'aria-modal="true"', "The result is unknown", "Refresh the map"):
            self.assertIn(marker, html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

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

    def _scan_rows(self):
        status = self.scanned()
        rows = self.tool("storage_query", {"scanId": status["scanId"], "scanVersion": status["scanVersion"],
                                           "basis": "logical", "aggregate": "largest_files", "pageSize": 100})["data"]["rows"]
        return status, rows

    def test_small_installer_stays_selectable(self):
        for name, size in (("Documents/setup.dmg", 10_000), ("Documents/x.crdownload", 5_000)):
            (self.fixture / name).write_bytes(b"\0" * size)
        _, rows = self._scan_rows()
        labels = [r["displayLabel"] for r in rows]
        self.assertIn("setup.dmg", labels)
        self.assertIn("x.crdownload", labels)
        for r in rows:
            if r["displayLabel"] in ("setup.dmg", "x.crdownload"):
                self.assertNotIn("groupedFiles", r)

    def test_nodes_keep_modified_time(self):
        _, rows = self._scan_rows()
        row = next(r for r in rows if r["displayLabel"] == "sparse.mov")
        self.assertAlmostEqual(row["modifiedAt"], os.stat(self.fixture / "Films/sparse.mov").st_mtime, delta=2)

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

    def test_slow_tool_call_does_not_block_other_requests(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "storage_choose_folder", "arguments": {}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
        stdin = io.BytesIO(b"".join(json.dumps(l).encode() + b"\n" for l in lines))
        stdout = io.StringIO()
        serve(stdin, stdout, chooser=lambda: time.sleep(2) or None, opener=lambda url: True)
        order = [json.loads(l)["id"] for l in stdout.getvalue().splitlines()]
        self.assertEqual(order, [1, 3, 2])  # the ping is answered while the picker is still open

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


class MetadataWordingTests(unittest.TestCase):
    """Shipped descriptions match the product: it clears space, but only on the person's click."""

    def test_descriptions_no_longer_claim_read_only(self):
        import json as _json
        from pathlib import Path as _Path
        from storage_assistant import picker
        root = _Path(__file__).resolve().parent.parent
        codex = _json.loads((root / "plugins/storage-assistant/plugin.json").read_text())
        claude = _json.loads((root / "plugins/storage-assistant/.claude-plugin/plugin.json").read_text())
        mcpb = _json.loads((root / "packaging/mcpb/manifest.json").read_text())
        self.assertEqual(codex["description"], claude["description"])
        texts = [codex["description"], codex["extensions"]["com.openai"]["interface"]["shortDescription"],
                 mcpb["description"], mcpb["long_description"], picker.PROMPT]
        for text in texts:
            lowered = text.lower()
            for stale in ("read-only", "read only", "nothing is changed", "never opens or deletes"):
                self.assertNotIn(stale, lowered, text)
        for text in (codex["description"], mcpb["description"], mcpb["long_description"], picker.PROMPT):
            self.assertIn("unless you click", text)
        self.assertNotIn('"', picker.PROMPT)  # embedded in an AppleScript string literal

    def test_model_facing_text_matches_click_only_cleanup(self):
        """I1: the model is told scanning is read-only, files change only on the person's click, and it cannot act."""
        from pathlib import Path as _Path
        import storage_assistant
        from storage_assistant import apps, plugin as plugin_module
        root = _Path(__file__).resolve().parent.parent
        skill = (root / "plugins/storage-assistant/skills/storage-inspection/SKILL.md").read_text()
        instructions = plugin_module.PLUGIN_INSTRUCTIONS
        for text in (instructions, skill):
            lowered = text.lower()
            for stale in ("storage is read-only", "there is no delete capability", "decides in finder",
                          "no deletion capability", "read-only storage tools"):
                self.assertNotIn(stale, lowered)
            self.assertIn("only when the person clicks", lowered)
            self.assertIn("storage_open_local_view", text)
            self.assertIn("Free up space", text)
            self.assertIn("cannot", lowered)
        cli_source = (root / "plugins/storage-assistant/storage_assistant/__main__.py").read_text()
        for doc in (storage_assistant.__doc__, cli_source, apps.resource_list()["resources"][0]["description"]):
            for stale in ("No content or mutation API", "Read-only alias treemap", "Read-only metadata companion"):
                self.assertNotIn(stale, doc)


class ViewReceiptTests(unittest.TestCase):
    """I2 and I6 in both views (source-level: the views have no JS test harness)."""
    PKG = ROOT / "plugins/storage-assistant/storage_assistant"
    REFUSALS = "['INVALID_INPUT','STALE_SCAN','SCOPE_EXPIRED','NOT_AUTHORIZED','CONFLICT','UNSUPPORTED']"

    def source(self, name):
        return (self.PKG / name).read_text().replace(" ", "").replace('"', "'")

    def function(self, text, header):
        start = text.index(header)
        depth, i = 0, text.index("{", start)
        for j in range(i, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                return text[start:j + 1]
        self.fail(header)

    def test_only_pre_action_codes_count_as_refused(self):
        for name in ("inline/app.js", "web/app.js"):
            with self.subTest(view=name):
                text = self.source(name)
                self.assertIn("REFUSALS=newSet(" + self.REFUSALS + ")", text)
                self.assertIn("refused:REFUSALS.has(", text)
                self.assertNotIn("{refused:true}", text)

    def test_snapshot_survives_a_restart_or_expired_grant(self):
        inline = self.source("inline/app.js")
        fail = self.function(inline, "functionfail(code)")
        self.assertIn("LASTING.has(code)", fail)
        self.assertIn("LASTING=newSet(['NOT_AUTHORIZED','SCOPE_EXPIRED','STALE_SCAN','NOT_FOUND'])", inline)
        controls = self.function(inline, "functioncontrols()")
        self.assertIn("frozen", controls)
        html = (self.PKG / "inline/index.html").read_text()
        self.assertIn('id="rescan"', html)
        rescan = self.function(inline, "asyncfunctionrescan()")
        self.assertIn("storage_choose_folder", rescan)
        self.assertIn("storage_start_scan", rescan)

    def test_view_tries_tools_before_going_read_only(self):
        inline = self.source("inline/app.js")
        check = self.function(inline, "asyncfunctioncheckTools()")
        self.assertIn("storage_list_scopes", check)
        self.assertIn("caps.serverTools", check)

    def test_view_offers_the_update(self):
        html = (self.PKG / "inline/index.html").read_text()
        self.assertIn('id="update"', html)
        self.assertIn("ui/open-link", self.source("inline/app.js"))

    def test_suggest_button_reenables_with_the_other_controls(self):
        """A view load marks the view busy while it redraws; controls() must re-enable the button afterwards."""
        controls = self.function(self.source("inline/app.js"), "functioncontrols()")
        self.assertIn("'suggest'", controls)

    def test_failed_rescan_keeps_the_receipt(self):
        inline = self.source("inline/app.js")
        fail = self.function(inline, "functionfail(code)")
        self.assertNotIn("$('receipt').hidden=true", fail)
        self.assertIn("receipt-only", fail)
        web = self.source("web/app.js")
        leave = self.function(web, "functionleaveCleanup()")
        self.assertNotIn("'receipt'", leave)
        css = (self.PKG / "inline/style.css").read_text()
        self.assertIn("#view.receipt-only>:not(#receipt){display:none}", css.replace(" ", ""))
