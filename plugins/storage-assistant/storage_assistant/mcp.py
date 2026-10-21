"""STDIO MCP 2025-11-25 adapter. No shell, model API or arbitrary path tool."""
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from . import __version__
from .contracts import SCHEMAS, tools_catalog, validate
from .policy import StorageError

INSTRUCTIONS = (
    "Storage is read-only. Human selects one folder in the local companion. Use only storage tools; "
    "never use shell/file tools to scan, widen scope, inspect names/contents or clean up. "
    "Start scan, poll, then query current scan/version. Cite returned evidence, byte basis, coverage and gaps. "
    "Aliases are intentional: names stay local. Categories/age do not prove safe deletion or duplicates. "
    "Allocated estimates are not reclaimable bytes. Unsupported requests must be declined."
)


def call(engine, name, args, url):
    try:
        if name not in SCHEMAS:
            raise StorageError("UNSUPPORTED", "This capability is not available in the read-only catalog.")
        validate(args, SCHEMAS[name])
        if name == "storage_list_scopes":
            data = engine.scopes()
        elif name == "storage_start_scan":
            data = engine.start(args["grantId"], args["requestId"], args.get("limits"))
        elif name == "storage_scan_status":
            data = engine.status(args["scanId"])
        elif name == "storage_cancel_scan":
            data = engine.cancel(args["scanId"])
        elif name == "storage_query":
            data = engine.query(args)
        elif name == "storage_get_node":
            data = engine.get_node(args["scanId"], args["scanVersion"], args["nodeId"])
        elif name == "storage_release_scope":
            data = engine.release(args["grantId"])
        else:
            with engine.lock:
                scan = engine.require_scan(args["scanId"], args["scanVersion"])
                ids = args.get("selectionIds", [])
                if any(node_id not in scan["nodes"] for node_id in ids):
                    raise StorageError("NOT_FOUND", "Selection is not in the current snapshot.")
                engine.view_revision += 1
                engine.view = args | {"evidence": engine.evidence(scan, ids, args["basis"]), "revision": engine.view_revision}
                data = {"localViewUrl": url, "evidence": engine.evidence(scan, ids, args["basis"]),
                        "privacy": "Names remain in the paired local browser; tool data contains aliases only."}
        envelope = {"ok": True, "data": data, "asOf": time.time()}
    except StorageError as exc:
        envelope = {"ok": False, "error": {"code": exc.code, "message": exc.message, "retryable": False}}
    except Exception:
        envelope = {"ok": False, "error": {"code": "UNAVAILABLE", "message": "Local executor unavailable.", "retryable": False}}
    return {"content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=True)}],
            "structuredContent": envelope, "isError": not envelope["ok"]}


class Protocol:
    def __init__(self, invoke, apps=False):
        self.invoke = invoke
        self.initialized = False
        self.apps = apps
        self.ui_supported = False

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "Invalid request"}}
        method = request["method"]
        if "id" not in request:
            return None
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            params = request.get("params", {})
            if not isinstance(params, dict):
                return response | {"error": {"code": -32602, "message": "Invalid initialization"}}
            caps = params.get("capabilities", {})
            extensions = caps.get("extensions", {}) if isinstance(caps, dict) else {}
            ui = extensions.get("io.modelcontextprotocol/ui", {}) if isinstance(extensions, dict) else {}
            mime_types = ui.get("mimeTypes", []) if isinstance(ui, dict) else []
            self.ui_supported = isinstance(mime_types, list) and "text/html;profile=mcp-app" in mime_types
            self.initialized = True
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}} | ({"resources": {}} if self.apps else {}),
                      "serverInfo": {"name": "storage-assistant", "version": __version__}, "instructions": INSTRUCTIONS + (
                          " ChatGPT route is synthetic-only. First query data, then call storage_render_view to render the inline alias view. Never request or infer live scope."
                          if self.apps else "")}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return response | {"error": {"code": -32002, "message": "Initialize first"}}
        elif method == "tools/list":
            catalog = tools_catalog()
            if self.apps:
                from .apps import UI_URI
                for tool in catalog:
                    if tool["name"] == "storage_render_view":
                        tool["description"] = "Render the inline alias treemap/table from a completed current snapshot. Query data first; pass owned IDs only. No folder selection or rescan."
                        if self.ui_supported:
                            tool["_meta"] = {"ui": {"resourceUri": UI_URI}}
            result = {"tools": catalog}
        elif self.apps and method == "resources/list":
            from .apps import resource_list
            result = resource_list()
        elif self.apps and method == "resources/read":
            from .apps import resource_read
            try:
                result = resource_read(request.get("params"))
            except StorageError:
                return response | {"error": {"code": -32602, "message": "Resource is unavailable; refresh connection metadata"}}
        elif method == "tools/call":
            params = request.get("params", {})
            if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str):
                return response | {"error": {"code": -32602, "message": "Invalid tool request"}}
            result = self.invoke(params["name"], params.get("arguments", {}))
        else:
            return response | {"error": {"code": -32601, "message": "Method not supported"}}
        return response | {"result": result}


def load_session(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > 4096:
            raise StorageError("NOT_AUTHORIZED", "Session descriptor must be a private local file.")
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = None
            session = json.load(stream)
        import re
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", session.get("url", "")):
            raise StorageError("NOT_AUTHORIZED", "Session descriptor is not loopback.")
        return session
    finally:
        if fd is not None:
            os.close(fd)


def bridge_invoke(session, apps=False):
    """Fixed private local hop; incoming client URLs/headers are never forwarded."""
    # Do not consult environment proxy settings for a private loopback session.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def invoke(name, arguments):
        try:
            if session is None:
                raise OSError()
            request = urllib.request.Request(session["url"] + ("/chatgpt-bridge" if apps else "/bridge"),
                json.dumps({"name": name, "arguments": arguments}).encode(),
                {"Authorization": "Bearer " + session["secret"], "Content-Type": "application/json"})
            with opener.open(request, timeout=5) as response:
                return json.load(response)
        except Exception:
            envelope = {"ok": False, "error": {"code": "UNAVAILABLE", "message": "Start or reattach the local companion session.", "retryable": False}}
            return {"structuredContent": envelope, "content": [{"type": "text", "text": json.dumps(envelope)}], "isError": True}
    return invoke


def attach(path, apps=False):
    try:
        session = load_session(path)
    except Exception:
        session = None
    protocol = Protocol(bridge_invoke(session, apps=apps), apps=apps)
    while True:
        line = sys.stdin.buffer.readline(32769)
        if not line:
            break
        try:
            if len(line) > 32768 or not line.endswith(b"\n"):
                break
            result = protocol.handle(json.loads(line))
        except Exception:
            result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
        if result is not None:
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
