"""Self-contained STDIO plugin server for Claude and Codex hosts.

One process owns the engine, the paired loopback view and MCP. The human picks
the folder by naming it in the conversation or in a native dialog. In this mode the human
has chosen to let the assistant see file names, relative paths and sizes (never
contents), so it can explain what takes space and show it as a treemap. The
assistant may name the folder (for example ~/Downloads); policy.open_root still
refuses home, system, volume roots, protected folders, traversal and links.
"""
import json
import sys
import threading
import time
import webbrowser
from .apps import UI_URI, call_apps, view
from .contracts import obj, validate
from .engine import Engine
from .mcp import Protocol, call
from .picker import choose_folder
from .policy import StorageError
from .server import Companion

PLUGIN_INSTRUCTIONS = (
    "Storage is read-only: sizes, names and folder structure of ONE folder the human picks; never file contents. "
    "Use only storage tools; never use shell/file tools to scan, widen scope or clean up. "
    "1) Call storage_choose_folder with the folder the person names as path (for example ~/Downloads or ~/Desktop/Projects); "
    "omit path only if they did not say which folder, which shows a native picker. "
    "2) storage_start_scan scans everything (including .git, node_modules and iCloud-only files; up to 15 minutes for huge folders) "
    "and shows a treemap when done. If it returns state running, tell the person it is still scanning and call storage_scan_status "
    "again until it finishes; each call waits up to about 50 seconds. Do not pass limits. "
    "3) ALWAYS call storage_render_view to show the person an interactive treemap of the folder, then answer using real names and "
    "paths from storage_query (largest_files, children, by_category; drill in with withinNodeId). "
    "Report sizes in MB/GB and describe coverage using the coverageSummary field (do not infer limits from the limits settings). "
    "Categories and age never prove something is safe to delete; allocated estimates are not reclaimable bytes. "
    "There is no delete capability: suggest what to review, and the person decides in Finder.")
PLUGIN_LIMITS = {"maxEntries": 600000, "maxDepth": 128, "maxDurationMs": 900000}
SCAN_WAIT = 50
GAP_WORDS = {"cloud_placeholder": "iCloud-only items not downloaded (they use no local space)",
             "symlink": "links not followed", "excluded": "credential folders and keys skipped (.ssh, .aws, .env, keys)",
             "denied": "items macOS denied access to", "changed": "items that changed during the scan",
             "other_volume": "other disks not crossed", "time_limit": "stopped at the time limit",
             "entry_limit": "stopped at the item limit", "depth_limit": "folders deeper than the depth limit",
             "allocation_unknown": "files with unknown disk usage", "provider_unknown": "items with unknown cloud status",
             "unsupported": "special files skipped", "metadata_delayed": "scan was cut short",
             "unavailable": "scanner errors"}


def coverage_summary(data, cloud_only=0):
    """Plain-language coverage so the model does not mistake configured limits for limits hit."""
    gaps = data.get("gapCounts") or {}
    cloud = (f" {cloud_only:,} iCloud-only files are counted at their full size but take no space on this Mac."
             if cloud_only else "")
    if data.get("state") == "running":
        return f"Still scanning: {data.get('observedEntries', 0):,} items so far."
    if not gaps:
        return "Complete: every item in the folder was measured." + cloud
    parts = [f"{count:,} {GAP_WORDS.get(reason, reason.replace('_', ' '))}" for reason, count in sorted(gaps.items())]
    stopped = any(r in gaps for r in ("time_limit", "entry_limit", "metadata_delayed"))
    return (("Stopped early; totals are a lower bound. " if stopped else "Everything reachable was measured. Not counted: ")
            + "; ".join(parts) + "." + cloud)
RENDER_DESCRIPTION = ("Show the person an interactive treemap and table of the scanned folder (inline in the chat where supported, "
                      "and in the local browser view). Call this after every scan and whenever they want to see or explore space. "
                      "Use withinNodeId to open a subfolder; selectionIds highlights items you mention.")

PLUGIN_TOOLS = [
    ("storage_choose_folder", "Select the ONE folder to measure. Pass path when the person names a folder "
     "(absolute or starting with ~/, e.g. ~/Downloads). Omit path only if they did not name one; a native picker opens. "
     "Home itself, system, Library, volume roots and links are refused. Returns a grant; replaces any current selection.",
     obj({"path": {"type": "string", "maxLength": 1024}})),
    ("storage_open_local_view", "Open the full-size treemap in the person's web browser on this Mac. "
     "Use when they want a bigger view or the inline view is unavailable.", obj()),
]
SCHEMAS = {name: schema for name, _, schema in PLUGIN_TOOLS}


def envelope(fn):
    try:
        body = {"ok": True, "data": fn(), "asOf": time.time()}
    except StorageError as exc:
        body = {"ok": False, "error": {"code": exc.code, "message": exc.message, "retryable": False}}
    except Exception:
        body = {"ok": False, "error": {"code": "UNAVAILABLE", "message": "Local executor unavailable.", "retryable": False}}
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=True)}],
            "structuredContent": body, "isError": not body["ok"]}


class PluginProtocol(Protocol):
    def handle(self, request):
        response = super().handle(request)
        if response and isinstance(request, dict) and request.get("method") == "tools/list" and "result" in response:
            extra = []
            for name, description, schema in PLUGIN_TOOLS:
                extra.append({"name": name, "description": description, "inputSchema": schema,
                              "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}})
            tools = extra + response["result"]["tools"]
            for tool in tools:
                # Advertise the UI to every host: ones without MCP Apps ignore _meta.
                # start_scan carries it too, so a treemap appears after every scan
                # without relying on the model to call render_view.
                if tool["name"] == "storage_render_view":
                    tool["description"] = RENDER_DESCRIPTION
                if tool["name"] in ("storage_render_view", "storage_start_scan", "storage_scan_status"):
                    tool["_meta"] = {"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI}
                if tool["name"] == "storage_start_scan":
                    # Scan size is the grant's; the model cannot shrink it to stale maxima.
                    schema = json.loads(json.dumps(tool["inputSchema"]))
                    schema["properties"].pop("limits", None)
                    tool["inputSchema"] = schema
            response["result"]["tools"] = tools
        if response and isinstance(request, dict) and request.get("method") == "initialize" and "result" in response:
            response["result"]["instructions"] = PLUGIN_INSTRUCTIONS
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
            # Host diagnostics only (capability names), visible in the host's MCP log.
            print("storage-assistant: client capabilities " + json.dumps(capabilities)[:2000], file=sys.stderr, flush=True)
        return response


class Plugin:
    def __init__(self, chooser=choose_folder, opener=webbrowser.open):
        self.engine = Engine(share_names=True, limits=PLUGIN_LIMITS, full_coverage=True)
        self.companion = Companion(self.engine)
        self.chooser = chooser
        self.opener = opener
        self.thread = threading.Thread(target=self.companion.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.thread.start()
        self.protocol = PluginProtocol(self.invoke, apps=True)

    def invoke(self, name, args):
        started = time.monotonic()
        result = self._invoke(name, args)
        # Host diagnostics: tool name, outcome and duration only (no arguments, names or paths).
        outcome = "error " + result["structuredContent"]["error"]["code"] if result.get("isError") else "ok"
        print(f"storage-assistant: {name} {outcome} {round((time.monotonic() - started) * 1000)}ms",
              file=sys.stderr, flush=True)
        return result

    def _invoke(self, name, args):
        if name in SCHEMAS:
            def run():
                if name == "storage_choose_folder":
                    if not isinstance(args, dict) or set(args) - {"path"}:
                        raise StorageError("INVALID_INPUT", "Only an optional path is accepted.")
                    return self.choose(args)
                validate(args, SCHEMAS[name])
                return self.open_view()
            return envelope(run)
        if name in ("storage_start_scan", "storage_scan_status"):
            if name == "storage_start_scan" and isinstance(args, dict):
                args = {k: v for k, v in args.items() if k != "limits"}
            result = call(self.engine, name, args, self.companion.url)
            body = result["structuredContent"]
            if body["ok"] and body["data"]["state"] == "running":
                scan_id, deadline = body["data"]["scanId"], time.monotonic() + SCAN_WAIT
                while time.monotonic() < deadline and self.running(scan_id):
                    time.sleep(.25)
                # Totals are recomputed once, not on every wait tick.
                result = call(self.engine, "storage_scan_status", {"scanId": scan_id}, self.companion.url)
            return self.with_view(result)
        if name == "storage_render_view":
            # Highlight in the paired local view, then return the inline alias view.
            local = call(self.engine, name, args, self.companion.url)
            if local["isError"]:
                return local
            result = call_apps(self.engine, name, args)
            if not result["isError"]:
                result["structuredContent"]["data"]["privacy"] = (
                    "Names, relative paths, categories, sizes and coverage reach this assistant host; file contents are never read.")
                result["structuredContent"]["data"]["localViewAvailable"] = True
                result["content"] = [{"type": "text", "text": json.dumps(result["structuredContent"], ensure_ascii=True)}]
            return result
        return call(self.engine, name, args, self.companion.url)

    def with_view(self, result):
        """Attach the root treemap view to a finished scan result for MCP Apps hosts."""
        body = result["structuredContent"]
        if result["isError"]:
            return result
        body["data"]["coverageSummary"] = coverage_summary(body["data"], self.cloud_only())
        result["content"] = [{"type": "text", "text": json.dumps(body, ensure_ascii=True)}]
        if body["data"]["state"] == "running":
            return result
        try:
            data = view(self.engine, {"scanId": body["data"]["scanId"], "scanVersion": body["data"]["scanVersion"],
                                      "basis": "logical"})
        except StorageError:
            return result
        body["data"]["view"] = data["view"]
        result["content"] = [{"type": "text", "text": json.dumps(body, ensure_ascii=True)}]
        return result

    def running(self, scan_id):
        with self.engine.lock:
            scan = self.engine.scan
            return bool(scan and scan["scanId"] == scan_id and scan["state"] == "running" and not scan.get("collectionFinalized"))

    def cloud_only(self):
        with self.engine.lock:
            scan = self.engine.scan
            if not scan:
                return 0
            return sum(1 for n in scan["nodes"].values() if n["kind"] == "file" and n["placeholder"] == "yes")

    def choose(self, args):
        path = args.get("path")
        if path is None:
            path = self.chooser()
            if not path:
                raise StorageError("NOT_SELECTED", "The human cancelled the folder picker. Nothing was selected.")
        elif not isinstance(path, str) or not 0 < len(path) <= 1024 or "\x00" in path:
            raise StorageError("INVALID_INPUT", "Give one folder path, absolute or starting with ~/.")
        elif not path.startswith(("/", "~/")):
            raise StorageError("INVALID_INPUT", "Use an absolute path or one starting with ~/ (for example ~/Downloads).")
        self.companion.export_preview = None
        return self.engine.select(path)

    def open_view(self):
        # The pairing code travels only in the URL fragment to the local browser.
        if not self.opener(f"{self.companion.url}/#pair={self.companion.pairing}"):
            raise StorageError("UNAVAILABLE", "Could not open the local browser view.")
        return {"opened": True, "note": "The human's browser shows names locally; nothing about them is returned here."}

    def close(self):
        self.companion.shutdown()
        self.thread.join(timeout=6)
        self.engine.close()
        self.companion.server_close()


def serve(stdin=None, stdout=None, **kwargs):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    plugin = Plugin(**kwargs)
    try:
        while True:
            line = stdin.readline(32769)
            if not line:
                break
            try:
                if len(line) > 32768 or not line.endswith(b"\n"):
                    break
                result = plugin.protocol.handle(json.loads(line))
            except Exception:
                result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
            if result is not None:
                print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), file=stdout, flush=True)
    finally:
        plugin.close()
