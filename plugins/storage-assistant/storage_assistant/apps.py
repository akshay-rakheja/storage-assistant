"""Generic MCP Apps resource and bounded views. Never serve arbitrary resources."""
import hashlib
import json
import math
from pathlib import Path
import time
from .contracts import SCHEMAS, validate
from .policy import StorageError

ASSETS = Path(__file__).parent / "inline"
# Hosts cache UI resources by URI, so the URI carries a fingerprint of the bundle:
# any change to the view's HTML, CSS or JS gets a new address and is fetched fresh.
_FINGERPRINT = hashlib.sha256(b"".join((ASSETS / n).read_bytes() for n in ("index.html", "style.css", "app.js"))).hexdigest()[:12]
UI_URI = f"ui://storage/treemap-{_FINGERPRINT}.html"
UI_MIME = "text/html;profile=mcp-app"
UI_PROTOCOL = "2026-01-26"
UI_META = {"ui": {"prefersBorder": True, "csp": {
    "connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []}}}


def resource_list():
    return {"resources": [{"uri": UI_URI, "name": "storage-treemap-v1",
                           "description": "Read-only alias treemap and table; no local names or network access.",
                           "mimeType": UI_MIME, "_meta": UI_META}]}


def resource_read(params):
    if (not isinstance(params, dict) or set(params)-{"uri","_meta"} or params.get("uri") != UI_URI or
            "_meta" in params and not isinstance(params["_meta"],dict)):
        raise StorageError("NOT_FOUND", "UI resource is unavailable; refresh the connection metadata.")
    metadata=params.get('_meta',{})
    if 'progressToken' in metadata:
        token=metadata['progressToken']
        if not (isinstance(token,str) or type(token) is int or type(token) is float and math.isfinite(token)):
            raise StorageError("NOT_FOUND", "UI resource is unavailable; refresh the connection metadata.")
    # MCP request metadata is permitted, never forwarded, echoed or treated as
    # a path, caller/session authority, view selection or resource override.
    html = (ASSETS / "index.html").read_text()
    html = html.replace("/* BUNDLED_STYLE */", (ASSETS / "style.css").read_text())
    html = html.replace("/* BUNDLED_SCRIPT */", (ASSETS / "app.js").read_text())
    return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": html, "_meta": UI_META}]}


def view(engine, args):
    validate(args, SCHEMAS["storage_render_view"])
    with engine.lock:
        scan = engine.require_scan(args["scanId"], args["scanVersion"])
        if scan["state"] == "running":
            raise StorageError("CONFLICT", "Wait for completion or cancel before rendering a stable snapshot.")
        ids = args.get("selectionIds", [])
        if any(i not in scan["nodes"] for i in ids):
            raise StorageError("NOT_FOUND", "Selection is not in the current snapshot.")
        default_within = scan.get("rootNodeId")
        if len(ids) == 1:
            chosen = scan["nodes"][ids[0]]
            default_within = chosen["nodeId"] if chosen["kind"] == "directory" else chosen["parentId"]
        within = args.get("withinNodeId", default_within)
        node = scan["nodes"].get(within)
        if not node or node["kind"] != "directory":
            raise StorageError("NOT_FOUND", "Choose an observed directory in the current snapshot.")
        query = {k: args[k] for k in ("scanId", "scanVersion", "basis", "cursor", "pageSize", "aggregate") if k in args}
        query["withinNodeId"] = within
        result = engine.query(query)
        engine.view["selectionIds"] = ids
        engine.view["evidence"] = engine.evidence(scan, ids or [within], args["basis"])
        # All summaries use the existing non-local alias projection. Never use
        # local_state(), raw nodes, identity, names or relative paths here.
        breadcrumbs = []
        ancestor = node
        while ancestor:
            breadcrumbs.append(engine.summary(ancestor))
            ancestor = scan["nodes"].get(ancestor["parentId"])
        unknown = 0
        for candidate in scan["nodes"].values():
            if candidate["kind"] != "file" or candidate["allocatedEstimateBytes"] is not None:
                continue
            current = candidate
            while current:
                if current["nodeId"] == within:
                    unknown += 1
                    break
                current = scan["nodes"].get(current["parentId"])
        return {"view": {"scanId": scan["scanId"], "scanVersion": scan["scanVersion"],
                "viewRevision": engine.view_revision, "basis": args["basis"],
                "aggregate": args.get("aggregate", "children"), "within": engine.summary(node),
                "breadcrumbs": list(reversed(breadcrumbs)), "selectionIds": ids,
                "rows": result["rows"], "matchingRows": result["matchingRows"],
                "nextCursor": result["nextCursor"], "unknownAllocationCount": unknown,
                "evidence": engine.view["evidence"], "gapCounts": result["gapCounts"],
                "caveats": result["caveats"]},
                "privacy": "Synthetic-only ChatGPT route. Aliases, categories, sizes and coverage reach the host; no names, paths or contents. Host history may retain delivered data."}


def call_apps(engine, name, args):
    from .mcp import call
    if name != "storage_render_view":
        return call(engine, name, args, "")
    try:
        envelope = {"ok": True, "data": view(engine, args), "asOf": time.time()}
    except StorageError as exc:
        envelope = {"ok": False, "error": {"code": exc.code, "message": exc.message, "retryable": False}}
    except Exception:
        envelope = {"ok": False, "error": {"code": "UNAVAILABLE", "message": "Local executor unavailable.", "retryable": False}}
    return {"content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=True)}],
            "structuredContent": envelope, "isError": not envelope["ok"]}
