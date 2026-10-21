"""Small strict JSON Schema subset; all exposed arguments are bounded data."""
import re
from .policy import StorageError

ID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
BYTES = {"type": "string", "pattern": "^(0|[1-9][0-9]{0,29})$"}
VERSION = {"type": "integer", "minimum": 1, "maximum": 1000000}
BASIS = {"type": "string", "enum": ["logical", "allocated_estimate"]}


def obj(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


SCAN = obj({"scanId": ID}, ["scanId"])
SNAPSHOT = {"scanId": ID, "scanVersion": VERSION}
LIMITS = obj({"maxEntries": {"type": "integer", "minimum": 1, "maximum": 40000},
              "maxDepth": {"type": "integer", "minimum": 1, "maximum": 8},
              "maxDurationMs": {"type": "integer", "minimum": 1, "maximum": 3000}})


def strings(enum, maximum):
    return {"type": "array", "items": {"type": "string", "enum": enum}, "minItems": 1, "maxItems": maximum}


QUERY = obj(SNAPSHOT | {
    "withinNodeId": ID, "basis": BASIS,
    "aggregate": {"type": "string", "enum": ["children", "largest_files", "by_category", "by_kind"]},
    "pageSize": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": ID,
    "predicate": obj({"minBytes": BYTES, "maxBytes": BYTES,
                      "kinds": strings(["file", "directory", "symlink", "other"], 4),
                      "categoryIn": strings(["video", "audio", "image", "archive", "document", "source", "other"], 7),
                      "placeholder": {"type": "string", "enum": ["yes", "no", "unknown"]}})
}, ["scanId", "scanVersion", "basis"])

TOOL_DEFINITIONS = [
    ("storage_list_scopes", "List the folder selected by the human locally; returns an alias, never its path.", obj()),
    ("storage_start_scan", "Start bounded metadata-only scan in an existing grant. Poll status; no content opens.",
     obj({"grantId": ID, "requestId": ID, "limits": LIMITS}, ["grantId", "requestId"])),
    ("storage_scan_status", "Read scan progress, measured totals and gaps; this does not rescan.", SCAN),
    ("storage_cancel_scan", "Stop worker and keep explicitly cancelled partial results.", SCAN),
    ("storage_query", "Query current immutable snapshot. Cite evidence, basis and coverage. No filename, path, script or deletion input.", QUERY),
    ("storage_get_node", "Inspect a node alias and measurements from the current snapshot; no file contents.",
     obj(SNAPSHOT | {"nodeId": ID}, ["scanId", "scanVersion", "nodeId"])),
    ("storage_render_view", "Highlight existing IDs in the local companion; URL contains no names or secrets. Browser pairing is local.",
     obj(SNAPSHOT | {"basis": BASIS, "selectionIds": {"type": "array", "items": ID, "maxItems": 100},
                    "withinNodeId": ID, "cursor": ID,
                    "pageSize": {"type": "integer", "minimum": 1, "maximum": 100},
                    "aggregate": {"type": "string", "enum": ["children", "by_category"]}},
         ["scanId", "scanVersion", "basis"])),
    ("storage_release_scope", "Revoke folder grant and purge transient snapshots; no target filesystem mutation.",
     obj({"grantId": ID}, ["grantId"])),
]
SCHEMAS = {name: schema for name, _, schema in TOOL_DEFINITIONS}
OUTPUT = {"type": "object", "properties": {
    "ok": {"type": "boolean"}, "data": {"type": "object"}, "asOf": {"type": "number"},
    "error": obj({"code": {"type": "string"}, "message": {"type": "string"}, "retryable": {"type": "boolean"}}, ["code", "message", "retryable"])},
    "required": ["ok"], "additionalProperties": False}


def validate(value, schema):
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "number": (int, float)}
    if expected and (not isinstance(value, types[expected]) or expected in ("integer", "number") and type(value) is bool):
        raise StorageError("INVALID_INPUT", "Argument type does not match the tool schema.")
    if "enum" in schema and value not in schema["enum"]:
        raise StorageError("INVALID_INPUT", "Unsupported argument choice.")
    if expected == "object":
        if set(schema.get("required", [])) - set(value) or not schema.get("additionalProperties", True) and set(value) - set(schema["properties"]):
            raise StorageError("INVALID_INPUT", "Missing or unknown argument.")
        for key, item in value.items():
            if key in schema.get("properties", {}):
                validate(item, schema["properties"][key])
    elif expected == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise StorageError("INVALID_INPUT", "Too many or too few selections.")
        for item in value:
            validate(item, schema["items"])
    elif expected == "string":
        if len(value) > 128 or "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise StorageError("INVALID_INPUT", "Invalid opaque identifier or byte value.")
    elif expected in ("integer", "number"):
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise StorageError("INVALID_INPUT", "Argument is outside the allowed bound.")


def tools_catalog():
    return [{"name": name, "description": description, "inputSchema": schema, "outputSchema": OUTPUT,
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}
            for name, description, schema in TOOL_DEFINITIONS]
