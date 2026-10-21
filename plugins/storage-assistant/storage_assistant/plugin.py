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
from . import executor
from .cleanup import OPT_INS, Cleanup
from .contracts import ID, VERSION, obj, validate
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
    "2) storage_start_scan scans everything (including .git, node_modules and iCloud-only files; very large folders can take "
    "several minutes) and its result shows the person a live progress view that turns into the treemap by itself when done. "
    "If it returns state running, tell the person it is scanning, then call storage_scan_status (each call waits up to ~25 s) "
    "until the state is no longer running before answering. Never start a second scan for the same folder. Do not pass limits. "
    "If the person asks about their whole laptop or home folder, explain that one folder is scanned at a time and suggest "
    "~/Documents, ~/Downloads, ~/Desktop or a projects folder instead of opening the picker. "
    "3) The finished scan result already shows the person a treemap, so do NOT call storage_render_view after a scan. "
    "Answer using real names and paths from storage_query (largest_files, children, by_category; drill in with withinNodeId). "
    "Call storage_render_view only when the person asks to see a subfolder, the other size measure or file types. "
    "Report sizes in MB/GB and describe coverage using the coverageSummary field (do not infer limits from the limits settings). "
    "Categories and age never prove something is safe to delete; allocated estimates are not reclaimable bytes. "
    "There is no delete capability: suggest what to review, and the person decides in Finder. "
    "When the person wants to free space, call storage_suggest_cleanup after a scan. Explain the top candidates and their reasons. You cannot delete, move or evict anything: the person reviews and clicks in the view. Never claim something was deleted unless they tell you or a receipt says so.")
PLUGIN_LIMITS = {"maxEntries": 1000000, "maxDepth": 128, "maxDurationMs": 900000}
SCAN_WAIT = 25
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
                      "and in the local browser view). A finished scan already shows one; use this only when the person asks to see a subfolder, "
                      "the other size measure or file types. "
                      "Use withinNodeId to open a subfolder; selectionIds highlights items you mention.")

VIEW_ONLY = "View-only: called by the Storage Assistant view after the person clicks."

PLUGIN_TOOLS = [
    ("storage_choose_folder", "Select the ONE folder to measure. Pass path when the person names a folder "
     "(absolute or starting with ~/, e.g. ~/Downloads). Omit path only if they did not name one; a native picker opens. "
     "Home itself, system, Library, volume roots and links are refused. Returns a grant; replaces any current selection.",
     obj({"path": {"type": "string", "maxLength": 1024}})),
    ("storage_open_local_view", "Open the full-size treemap in the person's web browser on this Mac. "
     "Use when they want a bigger view or the inline view is unavailable.", obj()),
    ("storage_suggest_cleanup", "Suggest items worth reviewing for freeing space in the scanned folder, with evidence-based reasons "
     "(read-only; nothing is changed). Shows the person the treemap in cleanup mode, where only they can act. Call after a scan; "
     "use withinNodeId to focus on a subfolder.",
     obj({"scanId": ID, "scanVersion": VERSION, "withinNodeId": ID}, ["scanId", "scanVersion"])),
    ("storage_cleanup_preview", VIEW_ONLY + " Previews moving the selected items to the Trash, deleting them or evicting iCloud copies; "
     "returns what would happen and a one-time token. Changes nothing.",
     obj({"scanId": ID, "scanVersion": VERSION, "nodeIds": {"type": "array", "items": ID, "minItems": 1, "maxItems": 500},
          "action": {"type": "string", "enum": ["trash", "delete", "evict"]}}, ["scanId", "scanVersion", "nodeIds", "action"])),
    ("storage_cleanup_execute", VIEW_ONLY + " Carries out a previewed cleanup plan identified by its token and returns a receipt.",
     obj({"token": ID}, ["token"])),
    ("storage_cleanup_settings", VIEW_ONLY + " Switches optional cleanup checks on or off and can open the Trash in Finder.",
     obj({"optIn": obj({"gitHealth": {"type": "boolean"}, "exactDuplicates": {"type": "boolean"}}),
          "openTrash": {"type": "boolean"}}, ["optIn"])),
]
APP_ONLY = {"storage_cleanup_preview", "storage_cleanup_execute", "storage_cleanup_settings"}
SCHEMAS = {name: schema for name, _, schema in PLUGIN_TOOLS}


NO_VIEW = ("Cleanup actions run only from the person's click in a view, and this host has no inline view. "
           "Call storage_open_local_view and ask the person to review and act there.")


def _refuse_without_view():
    raise StorageError("UNSUPPORTED", NO_VIEW)


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
                changes = name in APP_ONLY
                if changes and not self.ui_supported:
                    # No MCP Apps view to click: the action tools do not exist for this client.
                    continue
                entry = {"name": name, "description": description, "inputSchema": schema,
                         "annotations": {"readOnlyHint": not changes, "destructiveHint": changes, "openWorldHint": False}}
                if changes:
                    entry["_meta"] = {"ui": {"visibility": ["app"]}}
                elif name == "storage_suggest_cleanup":
                    entry["_meta"] = {"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI}
                extra.append(entry)
            tools = extra + response["result"]["tools"]
            for tool in tools:
                # Advertise the UI to every host: ones without MCP Apps ignore _meta.
                # start_scan carries it too, so a treemap appears after every scan
                # without relying on the model to call render_view.
                if tool["name"] == "storage_render_view":
                    tool["description"] = RENDER_DESCRIPTION
                if tool["name"] in ("storage_render_view", "storage_start_scan"):
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
        self.cleanup = Cleanup(self.engine, executor.Executor())
        self.companion = Companion(self.engine, cleanup=self.cleanup)
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
        if name in APP_ONLY and not self.protocol.ui_supported:
            # Only an MCP Apps view (the person's click) may reach the action tools; other hosts use the paired
            # local view, which calls the executor directly and never goes through MCP.
            return envelope(_refuse_without_view)
        if name.startswith("storage_cleanup_") or name == "storage_suggest_cleanup":
            return envelope(lambda: self.cleanup_tool(name, args))
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

    def cleanup_tool(self, name, args):
        validate(args, SCHEMAS[name])
        if name == "storage_suggest_cleanup":
            data = self.cleanup.suggest(args["scanId"], args["scanVersion"], args.get("withinNodeId"))
            try:
                data["view"] = view(self.engine, {"scanId": args["scanId"], "scanVersion": args["scanVersion"],
                                                  "basis": "logical"})["view"]
            except StorageError:
                pass
            return data
        if name == "storage_cleanup_preview":
            return self.cleanup.preview(args["scanId"], args["scanVersion"], args["nodeIds"], args["action"])
        if name == "storage_cleanup_execute":
            return self.cleanup.execute(args["token"])
        for key, enabled in args["optIn"].items():
            self.cleanup.set_opt_in(key, enabled)
        if args.get("openTrash"):
            self.cleanup.open_trash()
        with self.cleanup.lock:
            return {"optIn": {n: n in self.cleanup.opt_in for n in OPT_INS}}

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
            return sum(n.get("cloudOnlyFiles", 1 if n["placeholder"] == "yes" else 0)
                       for n in scan["nodes"].values() if n["kind"] == "file")

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
    """Read JSON-RPC lines; tool calls run concurrently so a long scan wait never
    blocks pings, UI clicks or other requests from the host."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    plugin = Plugin(**kwargs)
    write_lock = threading.Lock()
    slots = threading.BoundedSemaphore(8)
    workers = []

    def reply(result):
        if result is not None:
            with write_lock:
                print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), file=stdout, flush=True)

    def handle(request):
        try:
            reply(plugin.protocol.handle(request))
        except Exception:
            reply({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                   "error": {"code": -32603, "message": "Internal error"}})
        finally:
            slots.release()

    try:
        while True:
            line = stdin.readline(32769)
            if not line:
                break
            if len(line) > 32768 or not line.endswith(b"\n"):
                break
            try:
                request = json.loads(line)
            except Exception:
                reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
                continue
            if isinstance(request, dict) and request.get("method") == "tools/call" and "id" in request:
                slots.acquire()
                worker = threading.Thread(target=handle, args=(request,), daemon=True)
                worker.start()
                workers = [w for w in workers if w.is_alive()] + [worker]
            else:
                slots.acquire()
                handle(request)
    finally:
        for worker in workers:
            worker.join(timeout=SCAN_WAIT + 5)
        plugin.close()
