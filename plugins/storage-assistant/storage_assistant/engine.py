"""One selected grant, one current snapshot, bounded typed index queries."""
import collections
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from .policy import Grant, StorageError, opaque, validate_limits, POLICY_VERSION

CAVEATS = [
    "Observed regular-file bytes only; directory overhead and omitted entries are not included.",
    "Hardlink bytes are counted once, assigned to the lexically first observed relative path.",
    "Allocated estimate uses st_blocks × 512; APFS clones, compression and snapshots prevent exclusive physical attribution.",
    "Neither apparent nor allocated bytes prove reclaimable space. No deletion capability exists.",
    "Cloud flags are metadata hints; real File Provider no-hydration behavior is unverified.",
    "A metadata snapshot is not atomic. Directory movement after validation has a residual read race.",
]


class Engine:
    def __init__(self, share_names=False, limits=None, full_coverage=False):
        # share_names: the human chose to let the assistant see names and
        # relative paths (plugin mode). limits: this engine's grant ceiling.
        self.share_names = share_names
        self.limits = limits
        self.full_coverage = full_coverage
        self.lock = threading.RLock()
        self.grant = None
        self.scan = None
        self.process = None
        self.version = 0
        self.requests = collections.OrderedDict()
        self.cursors = collections.OrderedDict()
        self.view = {}
        self.view_revision = 0
        self.closed = False

    def select(self, path):
        new_grant = Grant.select(path, self.limits)
        with self.lock:
            self.release()
            self.grant = new_grant
            return self.scopes()

    def authorize(self, grant_id=None):
        if self.closed or not self.grant or grant_id and self.grant.grant_id != grant_id:
            raise StorageError("NOT_AUTHORIZED", "Select one folder in the local companion first.")
        try:
            self.grant.validate()
        except StorageError:
            self.release()
            raise
        return self.grant

    def scopes(self):
        with self.lock:
            if not self.grant:
                return {"scopes": [], "policyVersion": POLICY_VERSION}
            grant = self.authorize()
            return {"scopes": [{"grantId": grant.grant_id, "displayLabel": "Selected folder",
                                "mode": "metadata_only", "expiresInSeconds": max(0, int(grant.expires - time.monotonic())),
                                "limits": grant.limits}], "policyVersion": POLICY_VERSION}

    def release(self, grant_id=None):
        with self.lock:
            if grant_id and (not self.grant or self.grant.grant_id != grant_id):
                raise StorageError("NOT_AUTHORIZED", "Unknown scope.")
            self._stop_process()
            if self.grant:
                self.grant.close()
            self.grant = self.scan = None
            self.requests.clear()
            self.cursors.clear()
            self.view = {}
            return {"released": True, "purged": True}

    def close(self):
        self.release()
        self.closed = True

    def _stop_process(self):
        proc = self.process
        self.process = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.15)

    def start(self, grant_id, request_id, limits=None):
        with self.lock:
            grant = self.authorize(grant_id)
            selected_limits = validate_limits(limits or {}, grant.limits)
            key = json.dumps(selected_limits, sort_keys=True)
            if request_id in self.requests:
                old_id, old_key = self.requests[request_id]
                if key != old_key:
                    raise StorageError("CONFLICT", "Request ID was used with different limits.")
                return self.status(old_id)
            if self.process and self.process.poll() is None:
                raise StorageError("CONFLICT", "A scan is already running; cancel it first.")
            self.version += 1
            scan = {"scanId": opaque(), "scanVersion": self.version, "state": "running",
                    "coverage": "partial", "nodes": {}, "gapCounts": collections.Counter(),
                    "started": time.monotonic(), "asOf": time.time(), "limits": selected_limits}
            self.scan = scan
            self.cursors.clear()
            self.view = {}
            self.requests[request_id] = (scan["scanId"], key)
            while len(self.requests) > 32:
                self.requests.popitem(last=False)
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR", "SYSTEMROOT")}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.Popen([sys.executable, "-m", "storage_assistant.worker", "--fd", str(grant.fd),
                                     "--limits", json.dumps(selected_limits)] + (["--full"] if self.full_coverage else []),
                                    cwd=Path(__file__).resolve().parent.parent, env=env,
                                    pass_fds=(grant.fd,), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8")
            self.process = proc
            threading.Thread(target=self._collect, args=(proc, scan), daemon=True).start()
            threading.Thread(target=self._deadline, args=(proc, scan), daemon=True).start()
            return self.status(scan["scanId"])

    def _collect(self, proc, scan):
        try:
            for line in proc.stdout:
                event = json.loads(line)
                with self.lock:
                    if self.scan is not scan or scan["state"] != "running":
                        break
                    if event["event"] == "node":
                        if len(scan["nodes"]) >= scan["limits"]["maxEntries"]:
                            scan["gapCounts"]["entry_limit"] += 1
                            scan["state"] = "partial"
                            self._stop_process()
                            break
                        event["gaps"] = []
                        # Keep only what queries use: identity matters only for hardlinked files.
                        del event["event"], event["modifiedNs"]
                        if event.pop("hardlinkCount", 0) <= 1:
                            del event["identity"]
                        for key in ("kind", "category", "placeholder", "protection"):
                            event[key] = sys.intern(event[key])
                        scan["nodes"][event["nodeId"]] = event
                        if event["parentId"] is None:
                            scan["rootNodeId"] = event["nodeId"]
                    elif event["event"] == "gap":
                        scan["gapCounts"][event["reason"]] += 1
                        node = scan["nodes"].get(event["nodeId"])
                        if node:
                            node["gaps"].append(event["reason"])
                    elif event["event"] == "done":
                        scan["workerDone"] = True
                        scan["elapsedMs"] = event["elapsedMs"]
                    else:
                        scan["gapCounts"]["unavailable"] += 1
        except (OSError, ValueError, KeyError):
            with self.lock:
                if self.scan is scan:
                    scan["gapCounts"]["unavailable"] += 1
                    scan["state"] = "partial"
        finally:
            proc.stdout.close()
            proc.wait()
            with self.lock:
                if self.scan is scan:
                    if scan["state"] == "running":
                        if not scan.get("workerDone"):
                            scan["gapCounts"]["unavailable"] += 1
                        scan["state"] = "partial" if scan["gapCounts"] else "complete"
                    scan.setdefault("elapsedMs", round((time.monotonic() - scan["started"]) * 1000))
                    scan["asOf"] = time.time()
                    self._aggregate(scan)
                    scan["collectionFinalized"] = True

    def _deadline(self, proc, scan):
        try:
            proc.wait(timeout=scan["limits"]["maxDurationMs"] / 1000 + 0.25)
        except subprocess.TimeoutExpired:
            with self.lock:
                if self.scan is scan and scan["state"] == "running":
                    scan["gapCounts"]["time_limit"] += 1
                    scan["gapCounts"]["metadata_delayed"] += 1
                    scan["state"] = "partial"
                    self._stop_process()
                    self._aggregate(scan)

    def _aggregate(self, scan):
        if scan.get("aggregatedFinal"):
            return
        seen = {}
        nodes = scan["nodes"]
        for node in sorted(nodes.values(), key=lambda n: n["relative"]):
            skipped = any(g in node["gaps"] for g in ("excluded", "cloud_placeholder", "other_volume", "changed", "denied"))
            node["logicalBytes"] = "0" if skipped else node["rawLogicalBytes"]
            node["allocatedEstimateBytes"] = "0" if skipped else node["rawAllocatedBytes"]
            node["hardlinkAliasOf"] = None
            if node["kind"] == "file" and not skipped and "identity" in node:
                ident = tuple(node["identity"])
                if ident in seen:
                    node["hardlinkAliasOf"] = seen[ident]
                    node["logicalBytes"] = node["allocatedEstimateBytes"] = "0"
                else:
                    seen[ident] = node["nodeId"]
            node["coverage"] = "partial" if node["gaps"] or scan["state"] == "running" else "complete"
        for node in reversed(list(nodes.values())):
            parent = nodes.get(node["parentId"])
            if parent:
                parent["logicalBytes"] = str(int(parent["logicalBytes"]) + int(node["logicalBytes"]))
                a, b = parent["allocatedEstimateBytes"], node["allocatedEstimateBytes"]
                parent["allocatedEstimateBytes"] = str(int(a) + int(b)) if a is not None and b is not None else None
                if node["coverage"] == "partial":
                    parent["coverage"] = "partial"
        scan["coverage"] = "cancelled" if scan["state"] == "cancelled" else "partial" if scan["state"] != "complete" or scan["gapCounts"] else "complete"
        if scan["state"] != "running":
            scan["aggregatedFinal"] = True

    def require_scan(self, scan_id, version=None):
        self.authorize()
        if not self.scan or self.scan["scanId"] != scan_id or version is not None and self.scan["scanVersion"] != version:
            raise StorageError("STALE_SCAN", "Snapshot is unavailable or superseded; use the current scan.")
        return self.scan

    def status(self, scan_id):
        with self.lock:
            scan = self.require_scan(scan_id)
            self._aggregate(scan)
            root = scan["nodes"].get(scan.get("rootNodeId"), {})
            return {k: scan[k] for k in ("scanId", "scanVersion", "state", "coverage", "asOf", "limits")} | {
                "rootNodeId": scan.get("rootNodeId"), "observedEntries": len(scan["nodes"]),
                "gapCounts": dict(scan["gapCounts"]), "unobservedEntries": "unknown" if scan["gapCounts"] or scan["state"] == "running" else 0,
                "logicalBytes": root.get("logicalBytes", "0"), "allocatedEstimateBytes": root.get("allocatedEstimateBytes", "0"),
                "elapsedMs": scan.get("elapsedMs", round((time.monotonic() - scan["started"]) * 1000)), "caveats": CAVEATS}

    def cancel(self, scan_id):
        with self.lock:
            scan = self.require_scan(scan_id)
            if scan["state"] == "running":
                scan["state"] = "cancelled"
                scan["gapCounts"]["cancelled"] += 1
                self._stop_process()
            return self.status(scan_id)

    def relative_path(self, node):
        nodes = self.scan["nodes"] if self.scan else {}
        parts = []
        while node and node["parentId"] is not None:
            parts.append(node["name"])
            node = nodes.get(node["parentId"])
        return "/".join(reversed(parts)) or "."

    def summary(self, node, local=False):
        named = local or self.share_names
        keys = ("nodeId", "parentId", "kind", "category", "logicalBytes", "allocatedEstimateBytes",
                "coverage", "placeholder", "protection", "hardlinkAliasOf", "gaps", "measuredAt")
        result = {k: node[k] for k in keys}
        result["displayLabel"] = node["name"] if named else ("Selected folder" if node["parentId"] is None else node["kind"].capitalize() + " " + node["nodeId"][:6])
        result["categoryEvidence"] = ["Extension hint v1; no content inspection and no disposal inference."]
        if local:
            result["itemLogicalBytes"] = node["rawLogicalBytes"]
        if self.share_names:
            result["path"] = self.relative_path(node)
            if node["parentId"] is None and self.grant:
                result["displayLabel"] = os.path.basename(self.grant.path.rstrip("/")) or "Selected folder"
        return result

    def get_node(self, scan_id, version, node_id, local=False):
        with self.lock:
            scan = self.require_scan(scan_id, version)
            self._aggregate(scan)
            node = scan["nodes"].get(node_id)
            if not node:
                raise StorageError("NOT_FOUND", "Unknown node in this snapshot.")
            return {"node": self.summary(node, local), "evidence": self.evidence(scan, [node_id], "logical")}

    @staticmethod
    def evidence(scan, ids, basis):
        return {"scanId": scan["scanId"], "scanVersion": scan["scanVersion"], "basis": basis,
                "coverage": scan["coverage"], "asOf": scan["asOf"], "nodeIds": ids}

    def query(self, args, local=False):
        with self.lock:
            scan = self.require_scan(args["scanId"], args["scanVersion"])
            self._aggregate(scan)
            if scan["state"] == "running":
                raise StorageError("CONFLICT", "Wait for completion or cancel before querying a stable snapshot.")
            nodes = scan["nodes"]
            within = args.get("withinNodeId", scan.get("rootNodeId"))
            if within not in nodes:
                raise StorageError("NOT_FOUND", "Unknown selection.")
            metric = "logicalBytes" if args["basis"] == "logical" else "allocatedEstimateBytes"
            operation = args.get("aggregate", "children")
            selected = set()
            for node in nodes.values():
                current = node
                while current:
                    if current["nodeId"] == within:
                        selected.add(node["nodeId"])
                        break
                    current = nodes.get(current["parentId"])
            candidates = [n for n in nodes.values() if n["nodeId"] in selected and n["nodeId"] != within]
            if operation == "children":
                candidates = [n for n in candidates if n["parentId"] == within]
            elif operation != "by_kind":
                candidates = [n for n in candidates if n["kind"] == "file"]
            predicate = args.get("predicate", {})
            for field, values in (("category", predicate.get("categoryIn")), ("kind", predicate.get("kinds"))):
                if values is not None:
                    candidates = [n for n in candidates if n[field] in values]
            if "placeholder" in predicate:
                candidates = [n for n in candidates if n["placeholder"] == predicate["placeholder"]]
            for field, sign in (("minBytes", 1), ("maxBytes", -1)):
                if field in predicate:
                    bound = int(predicate[field])
                    candidates = [n for n in candidates if n[metric] is not None and (int(n[metric]) - bound) * sign >= 0]
            if operation in ("by_category", "by_kind"):
                groups = {}
                grouping = "category" if operation == "by_category" else "kind"
                for n in candidates:
                    group = groups.setdefault(n[grouping], {"displayLabel": n[grouping], "bytes": "0", "count": 0, "unknownAllocationCount": 0})
                    group["count"] += 1
                    if n["kind"] != "file":
                        continue  # directory sums overlap regular-file contributions
                    if n[metric] is None:
                        group["unknownAllocationCount"] += 1
                    else:
                        group["bytes"] = str(int(group["bytes"]) + int(n[metric]))
                rows = list(groups.values())
                for group in rows:
                    if group["unknownAllocationCount"]:
                        group["bytes"] = None  # A known subtotal is not a total.
            else:
                rows = [self.summary(n, local) | {"bytes": n[metric]} for n in candidates]
            rows.sort(key=lambda r: (r["bytes"] is None, -int(r["bytes"] or 0), r.get("nodeId", r["displayLabel"])))
            fingerprint = json.dumps({k: v for k, v in args.items() if k != "cursor"}, sort_keys=True)
            offset = 0
            if "cursor" in args:
                saved = self.cursors.get(args["cursor"])
                if not saved or saved[0] != fingerprint or saved[2] < time.monotonic():
                    raise StorageError("INVALID_INPUT", "Cursor is expired or belongs to another query.")
                offset = saved[1]
            size = args.get("pageSize", 30)
            page = rows[offset:offset + size]
            cursor = None
            if offset + size < len(rows):
                cursor = opaque()
                self.cursors[cursor] = (fingerprint, offset + size, time.monotonic() + 120)
                while len(self.cursors) > 128:
                    self.cursors.popitem(last=False)
            ids = [r["nodeId"] for r in page if "nodeId" in r]
            evidence = self.evidence(scan, ids or [within], args["basis"])
            self.view_revision += 1
            self.view = {"scanId": scan["scanId"], "scanVersion": scan["scanVersion"], "withinNodeId": within,
                         "basis": args["basis"], "selectionIds": ids, "evidence": evidence, "revision": self.view_revision}
            return {"rows": page, "matchingRows": len(rows), "nextCursor": cursor,
                    "evidence": evidence, "gapCounts": dict(scan["gapCounts"]), "caveats": CAVEATS}

    def local_state(self):
        with self.lock:
            scopes = self.scopes()
            scopes["selectedFolderLabel"] = os.path.basename(self.grant.path) if self.grant else None
            if not self.scan:
                return scopes | {"scan": None, "nodes": [], "view": {}}
            status = self.status(self.scan["scanId"])
            return scopes | {"scan": status, "nodes": [self.summary(n, True) for n in self.scan["nodes"].values()], "view": self.view}
