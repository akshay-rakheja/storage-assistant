"""Cleanup approval: preview mints a one-time token bound to exact items; execute re-checks and acts.

This is the only path to Trash, permanent deletion or Remove Download. Everything fails closed:
items are reopened from the granted folder's descriptor one name at a time without following
links, compared with the scan and with the identity read at preview, and acted on through the
executor by parent descriptor and leaf name. A failure is reported per item and never escalates.
"""
import errno
import os
import posixpath
import secrets
import stat
import threading
import time

from . import macos, signals
from .executor import CleanupRefused, Executor
from .policy import DATALESS, StorageError, identity, opaque, skipped_in_full

ACTIONS = ("trash", "delete", "evict")
OPT_INS = ("gitHealth", "exactDuplicates")
MAX_ITEMS = 500
MAX_BYTES = 2 * 10 ** 12  # per action; a larger plan is refused as a whole
MAX_ENTRIES = 250000  # descendants walked before permanently deleting a folder
MAX_DEPTH = 256
TOKEN_TTL = 300
MAX_TOKENS = 16
_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)

CHANGED = "Changed since you reviewed it"
INCLUDED = "Included in its folder"
NOT_OFFERED = "Not offered for this action"
RECENT = "Changed in the last 7 days"
ICLOUD_ONLY = "Only in iCloud: deleting it frees nothing here"
DENIED = "macOS denied access"
TOO_MANY = "Too many files inside to check safely"
OUTSIDE = "Outside the selected folder"
RESCANNED = "This folder was rescanned. Review again."
MOVED = "This item changed since the scan. Scan again to see it."
TRASH_NOTE = "Space is freed when you empty the Trash."
# Not in the plan's list: Remove Download returned but macOS still reports the file as downloaded.
NOT_EVICTED = "macOS did not confirm the download was removed"
PARTLY = "Partly deleted: "
UNEXPECTED = "Something went wrong"
# Scan-level stop reasons: the worker may have stopped (or been stopped) before seeing every folder's contents.
_SCAN_STOPPED = ("time_limit", "entry_limit", "unavailable", "metadata_delayed", "cancelled")


class _Skip(Exception):
    def __init__(self, reason, failed=False):
        super().__init__(reason)
        self.reason, self.failed = reason, failed  # failed: the executor was entered


def _os_reason(error):
    return DENIED if getattr(error, "errno", None) in (errno.EACCES, errno.EPERM) else CHANGED


class Cleanup:
    def __init__(self, engine, executor=None, clock=time.time, spotlight=None, checks=None,
                 applications=("/Applications", "~/Applications")):
        self.engine = engine
        self.executor = executor or Executor()
        self.clock = clock
        self.spotlight = spotlight or signals.Spotlight()
        self.checks = checks or signals.Checks()
        self.applications = applications
        self.opt_in = set()
        self.lock = threading.Lock()
        self.candidates = {}  # (scanId, scanVersion) -> {nodeId: candidate}; latest scan only
        self.tokens = {}
        self.stale = set()  # scans this service changed on disk

    # Suggestions and opt-ins

    def suggest(self, scan_id, scan_version, within_node_id=None):
        with self.lock:
            opt_in = frozenset(self.opt_in)
        result = signals.suggest(self.engine, scan_id, scan_version, within_node_id, now=self.clock(),
                                 spotlight=self.spotlight, applications=self.applications,
                                 opt_in=opt_in, checks=self.checks)
        with self.lock:
            if frozenset(self.opt_in) == opt_in:  # an opt-in switched meanwhile: do not remember
                key = (scan_id, scan_version)
                remembered = self.candidates.get(key, {})
                remembered.update({c["nodeId"]: c for c in result["candidates"]})
                self.candidates = {key: remembered}
        return result

    def set_opt_in(self, name, enabled):
        if name not in OPT_INS or not isinstance(enabled, bool):
            raise StorageError("INVALID_INPUT", "Unknown opt-in or value.")
        with self.lock:
            (self.opt_in.add if enabled else self.opt_in.discard)(name)
            self.candidates = {}  # suggestions made under other opt-ins no longer count
            return {"optIn": {n: n in self.opt_in for n in OPT_INS}}

    def open_trash(self):
        try:
            macos.open_trash()
        except OSError:
            raise StorageError("UNAVAILABLE", "Could not open the Trash.") from None
        return {"opened": True}

    def locate(self, scan_id, scan_version, node_id):
        """Absolute path of a scanned file or folder, after the same no-follow re-check preview uses.

        For Show in Finder and Quick Look only; raises StorageError when the item no longer matches the scan."""
        if not isinstance(scan_id, str) or type(scan_version) is not int or not isinstance(node_id, str):
            raise StorageError("INVALID_INPUT", "Give the current scan and an item.")
        engine = self.engine
        with engine.lock:
            scan = self._current(scan_id, scan_version)
            node = scan["nodes"].get(node_id)
            if (node is None or node["parentId"] is None or node["kind"] not in ("file", "directory")
                    or "groupedFiles" in node):
                raise StorageError("INVALID_INPUT", "This item can't be shown.")
            relative = engine.relative_path(node)
            expected = {"relative": relative, "kind": node["kind"], "modifiedNs": node.get("modifiedNs"),
                        "size": int(node["rawLogicalBytes"]) if node["kind"] == "file" else None}
            try:
                self._with_item(expected, self._times(scan, self.clock()), None, lambda parent, name, st: None,
                                check_walk=False)
            except _Skip as skip:
                raise StorageError("CONFLICT", MOVED if skip.reason == CHANGED else skip.reason) from None
            return os.path.join(engine.grant.path, relative)

    # Preview

    def preview(self, scan_id, scan_version, node_ids, action):
        if action not in ACTIONS:
            raise StorageError("INVALID_INPUT", "Unknown cleanup action.")
        if (not isinstance(node_ids, list) or not 1 <= len(node_ids) <= MAX_ITEMS
                or not all(isinstance(n, str) and 0 < len(n) <= 64 for n in node_ids)):
            raise StorageError("INVALID_INPUT", f"Choose between 1 and {MAX_ITEMS} items.")
        if not isinstance(scan_id, str) or type(scan_version) is not int:
            raise StorageError("INVALID_INPUT", "Give the current scan.")
        engine, now = self.engine, self.clock()
        with engine.lock:
            scan = self._current(scan_id, scan_version)
            with self.lock:
                offered = dict(self.candidates.get((scan_id, scan_version), {}))
            nodes, newest, children = scan["nodes"], self._newest(scan), {}
            for n in nodes.values():
                children.setdefault(n["parentId"], []).append(n["nodeId"])
            depth = lambda nid: len(engine.relative_path(nodes[nid]).split("/")) if nid in nodes else 0
            ids = sorted(dict.fromkeys(node_ids), key=depth)
            accepted, items, skipped = set(), [], []
            for nid in ids:
                node = nodes.get(nid)
                label = engine.summary(node)["displayLabel"] if node else "Unknown item"
                try:
                    if node is None:
                        raise _Skip(OUTSIDE)
                    up = node["parentId"]
                    while up is not None and up not in accepted:
                        up = nodes[up]["parentId"]
                    if up is not None:
                        raise _Skip(INCLUDED)
                    candidate, warnings = self._eligible(scan, node, newest, children, offered, action, now)
                    item = self._bind(scan, node, action)
                    if action == "delete" and candidate and candidate.get("keptNodeId"):
                        item["kept"] = self._bind_kept(scan, candidate["keptNodeId"])
                except _Skip as skip:
                    skipped.append({"nodeId": nid, "displayLabel": label, "reason": skip.reason})
                    continue
                if action == "delete":
                    warnings.append("Deleted permanently: this can't be undone.")
                accepted.add(nid)
                summary = engine.summary(node)
                item.update({"nodeId": nid, "displayLabel": summary["displayLabel"],
                             "path": summary.get("path", item["relative"]),
                             "freesBytes": node["allocatedEstimateBytes"] or "0",
                             "reasons": candidate["reasons"] if candidate else [], "warnings": warnings})
                items.append(item)
            total = sum(int(i["freesBytes"]) for i in items)
            if total > MAX_BYTES:
                raise StorageError("INVALID_INPUT", "Too much at once; choose fewer items.")
            token, expires = None, now + TOKEN_TTL
            if items:
                token = secrets.token_hex(16)
                with self.lock:
                    self.tokens = {k: v for k, v in self.tokens.items() if v["expiresAt"] >= now}
                    while len(self.tokens) >= MAX_TOKENS:
                        self.tokens.pop(next(iter(self.tokens)))
                    self.tokens[token] = {"grantId": engine.grant.grant_id, "scanId": scan_id,
                                          "scanVersion": scan_version, "asOf": scan["asOf"],
                                          "startedAt": scan.get("startedAt", scan["asOf"]), "action": action,
                                          "items": items, "count": len(items), "bytes": total, "expiresAt": expires}
        public = ("nodeId", "displayLabel", "path", "freesBytes", "reasons", "warnings")
        return {"token": token, "expiresAt": expires if token else None, "action": action,
                "items": [{k: i[k] for k in public} for i in items], "skipped": skipped,
                "freesBytes": str(total), "count": len(items)}

    def _current(self, scan_id, scan_version):
        """The engine's current, finished scan, which this service has not changed since."""
        engine = self.engine
        engine.authorize()
        scan = engine.scan
        if (not scan or scan["scanId"] != scan_id or scan["scanVersion"] != scan_version
                or (scan_id, scan_version) in self.stale or scan.get("stale")):
            raise StorageError("STALE_SCAN", RESCANNED)
        engine._aggregate(scan)
        if scan["state"] == "running":
            raise StorageError("CONFLICT", "Wait for the scan to finish first.")
        return scan

    @staticmethod
    def _newest(scan):
        """Newest file modification below each node (an empty folder: its own), as signals does."""
        nodes, newest = scan["nodes"], {}
        for nid, node in reversed(list(nodes.items())):  # children follow their parents
            own = (node.get("modifiedNs") or 0) if node["kind"] == "file" else 0
            newest[nid] = max(newest.get(nid, 0), own)
            parent = node["parentId"]
            if parent in nodes:
                newest[parent] = max(newest.get(parent, 0), newest[nid])
        for nid, node in nodes.items():
            if not newest[nid]:
                newest[nid] = node.get("modifiedNs") or 0
        return newest

    @staticmethod
    def _eligible(scan, node, newest, children, offered, action, now):
        """(candidate or None, warnings) when the item may be planned for this action, else _Skip."""
        if (node["parentId"] is None or node["kind"] not in ("file", "directory") or "groupedFiles" in node
                or node.get("hardlinkAliasOf") or "identity" in node or (action == "evict" and node["kind"] != "file")):
            raise _Skip(NOT_OFFERED)  # the folder itself, grouped small files, hardlinks, links, special files
        nodes, subtree, stack = scan["nodes"], [], [node["nodeId"]]
        while stack:
            nid = stack.pop()
            subtree.append(nodes[nid])
            stack.extend(children.get(nid, []))
        if any(n["placeholder"] == "yes" or n.get("cloudOnlyFiles") for n in subtree):
            raise _Skip(ICLOUD_ONLY)
        if newest[node["nodeId"]] >= (now - signals.RECENT) * 1e9:
            raise _Skip(RECENT)
        # Incompletely seen items are never offered, whatever the action: any gap below other than links
        # (never followed; the delete walk checks they stay inside), or a folder in a scan that stopped early.
        if (any(g != "symlink" for n in subtree for g in n["gaps"])
                or node["kind"] == "directory" and any(scan["gapCounts"].get(r) for r in _SCAN_STOPPED)):
            raise _Skip(NOT_OFFERED)
        candidate = offered.get(node["nodeId"])
        if action == "delete" and (not candidate or candidate.get("tier") != "safe"):
            raise _Skip(NOT_OFFERED)  # permanent deletion is for Safe-tier suggestions only
        if candidate and action in candidate["actions"]:
            return candidate, []
        if action != "trash" or node["protection"] == "protected":
            raise _Skip(NOT_OFFERED)
        warnings = []
        if any(n["protection"] == "protected" for n in subtree[1:]):
            warnings.append("Contains code, a git repository or other protected items.")
        return None, warnings

    def _bind(self, scan, node, action):
        """Re-read the item now; return its token record (identity from this read)."""
        relative = self.engine.relative_path(node)
        expected = {"relative": relative, "kind": node["kind"], "modifiedNs": node.get("modifiedNs"),
                    "size": int(node["rawLogicalBytes"]) if node["kind"] == "file" else None}
        st = self._with_item(expected, self._times(scan, self.clock()), None, lambda parent, name, st: st, check_walk=action == "delete",
                             policy_reason=NOT_OFFERED)
        expected["identity"] = identity(st)
        return expected

    def _bind_kept(self, scan, kept_id):
        """A duplicate is deleted only while the copy it duplicates is still there, unchanged."""
        kept = scan["nodes"].get(kept_id)
        if kept is None or kept["kind"] != "file":
            raise _Skip(CHANGED)
        try:
            return self._bind(scan, kept, "trash")
        except _Skip:
            raise _Skip(CHANGED) from None

    def _check_kept(self, item, times):
        kept = item.get("kept")
        if kept is None:
            return
        try:
            self._with_item(kept, times, kept["identity"], lambda parent, name, st: None, check_walk=False)
        except _Skip:
            raise _Skip(CHANGED) from None

    # Revalidation

    @staticmethod
    def _times(scan, now):
        """Change limits in ns: leaf ctime after the scan; below a folder, anything since the scan began
        (the scanner may have passed that folder earlier) or modified in the last 7 days."""
        started = scan.get("startedAt", scan["asOf"])
        return {"asOf": scan["asOf"] * 1e9, "started": started * 1e9,
                "modified": min(started, now - signals.RECENT) * 1e9}

    def _with_item(self, item, times, bound, act, check_walk, policy_reason=CHANGED):
        """Open the item's parent from the grant without following links, check the leaf, then act."""
        parts = item["relative"].split("/")
        if not parts or any(p in ("", ".", "..") or "\0" in p for p in parts):
            raise _Skip(CHANGED)
        root = self.engine.grant.fd
        device, opened, parent = os.fstat(root).st_dev, [], root
        try:
            try:
                for part in parts[:-1]:
                    parent = os.open(part, _DIR_FLAGS, dir_fd=parent)
                    opened.append(parent)
                    if os.fstat(parent).st_dev != device:
                        raise _Skip(CHANGED)
                name = parts[-1]
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as error:
                raise _Skip(_os_reason(error)) from None
            kind_ok = stat.S_ISREG(st.st_mode) if item["kind"] == "file" else stat.S_ISDIR(st.st_mode)
            if (not kind_ok or st.st_dev != device or st.st_mtime_ns != item["modifiedNs"]
                    or item["size"] is not None and st.st_size != item["size"]
                    or st.st_ctime_ns > times["asOf"] or bound is not None and identity(st) != bound
                    or item["kind"] == "file" and st.st_nlink != 1):
                raise _Skip(CHANGED)
            if check_walk and stat.S_ISDIR(st.st_mode):
                self._walk_item(parent, name, st, times, policy_reason)
            return act(parent, name, st)
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _walk_item(self, parent, name, st, times, policy_reason):
        """Every entry below a folder to delete: not newer than the scan, same disk, no credentials,
        no iCloud-only files and no links leading out of the folder. Bounded."""
        count = [0]

        def visit(fd, rel, depth):
            if depth > MAX_DEPTH:
                raise _Skip(TOO_MANY)
            with os.scandir(fd) as entries:
                names = [e.name for e in entries]
            for child in names:
                count[0] += 1
                if count[0] > MAX_ENTRIES:
                    raise _Skip(TOO_MANY)
                info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if info.st_ctime_ns >= times["started"] or info.st_mtime_ns >= times["modified"]:
                    raise _Skip(CHANGED)
                if (skipped_in_full(child) or info.st_dev != st.st_dev or child.endswith(".icloud")
                        or getattr(info, "st_flags", 0) & DATALESS):
                    raise _Skip(policy_reason)
                child_rel = posixpath.join(rel, child)
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(child, dir_fd=fd)
                    resolved = posixpath.normpath(posixpath.join(rel, target))
                    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
                        raise _Skip(policy_reason)
                elif stat.S_ISDIR(info.st_mode):
                    sub = os.open(child, _DIR_FLAGS, dir_fd=fd)
                    try:
                        if identity(os.fstat(sub)) != identity(info):
                            raise _Skip(CHANGED)
                        visit(sub, child_rel, depth + 1)
                    finally:
                        os.close(sub)

        try:
            top = os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError as error:
            raise _Skip(_os_reason(error)) from None
        try:
            if identity(os.fstat(top)) != identity(st):
                raise _Skip(CHANGED)
            visit(top, ".", 0)
        except OSError as error:
            raise _Skip(_os_reason(error)) from None
        finally:
            os.close(top)

    # Execute

    def execute(self, token):
        if not isinstance(token, str) or len(token) != 32:
            raise StorageError("INVALID_INPUT", "This approval is not valid. Review again.")
        with self.lock:
            record = self.tokens.pop(token, None)
        if record is None:
            raise StorageError("INVALID_INPUT", "This approval was already used or is not valid. Review again.")
        if self.clock() > record["expiresAt"]:
            raise StorageError("INVALID_INPUT", "This approval expired. Review again.")
        engine = self.engine
        with engine.lock:  # holds the grant descriptor open and keeps scans from starting meanwhile
            if not engine.grant or engine.grant.grant_id != record["grantId"]:
                raise StorageError("NOT_AUTHORIZED", "The selected folder changed. Review again.")
            scan = self._current(record["scanId"], record["scanVersion"])
            action, path = record["action"], engine.grant.path
            before = self._free(path)
            done, skipped, failed, called, rescan = [], [], [], [], None
            times = self._times(record, self.clock())
            try:
                for item in record["items"]:
                    try:
                        try:
                            self._check_kept(item, times)
                            outcome = self._with_item(item, times, item["identity"],
                                                      self._act(action, item, called), check_walk=action == "delete")
                        except _Skip:
                            raise
                        except Exception:  # never lose the receipt: earlier items may already have been acted on
                            raise _Skip(UNEXPECTED, failed=item["nodeId"] in called) from None
                    except _Skip as skip:
                        reason = PARTLY + skip.reason if skip.failed and action == "delete" else skip.reason
                        (failed if skip.failed else skipped).append(
                            {"nodeId": item["nodeId"], "displayLabel": item["displayLabel"], "reason": reason})
                        continue
                    entry = {"nodeId": item["nodeId"], "displayLabel": item["displayLabel"], "path": item["path"],
                             "freesBytes": item["freesBytes"]}
                    if action == "trash":
                        entry.update({"trashPath": outcome, "putBack": bool(outcome)})
                    done.append(entry)
                after = self._free(path)
            finally:  # also after an unexpected error: whatever was acted on makes the snapshot stale
                if called:
                    rescan = self._rescan(scan)
        return {"action": action, "done": done, "skipped": skipped, "failed": failed,
                "freedEstimateBytes": str(sum(int(d["freesBytes"]) for d in done)),
                "freeSpaceBefore": before, "freeSpaceAfter": after,
                "putBack": action == "trash" and any(d["putBack"] for d in done),
                "trashNote": TRASH_NOTE if action == "trash" else None, "rescan": rescan}

    def _act(self, action, item, called):
        abs_path = os.path.join(self.engine.grant.path, item["relative"])

        def act(parent, name, st):
            if action != "delete":
                # Trash and Remove Download take a path: it must still lead to the item just checked
                # by descriptor (an ancestor may have been swapped meanwhile).
                try:
                    now = os.lstat(abs_path)
                except OSError:
                    raise _Skip(CHANGED) from None
                if ((now.st_dev, now.st_ino, stat.S_IFMT(now.st_mode))
                        != (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))):
                    raise _Skip(CHANGED)
            called.append(item["nodeId"])
            try:
                if action == "trash":
                    result = self.executor.trash(parent, name, abs_path)
                    try:
                        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except OSError:  # the name is gone (or no longer visible): macOS moved it
                        return result
                    if identity(after) == identity(st):
                        raise _Skip(CHANGED, failed=True)  # macOS reported success but the item is still here
                    return result
                if action == "delete":
                    return self.executor.delete(parent, name, MAX_ENTRIES)
                self.executor.evict(abs_path)
                after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if identity(after) != identity(st) or not getattr(after, "st_flags", 0) & DATALESS:
                    raise _Skip(NOT_EVICTED, failed=True)
                return None
            except CleanupRefused as refused:  # "too_many" is raised before anything is removed
                too_many = refused.reason == "too_many"
                raise _Skip(TOO_MANY if too_many else CHANGED, failed=not too_many) from None
            except _Skip:
                raise
            except OSError as error:
                raise _Skip(_os_reason(error), failed=True) from None
            except Exception:  # e.g. a ctypes/Foundation error: report it, never lose the receipt
                raise _Skip(UNEXPECTED, failed=True) from None
        return act

    @staticmethod
    def _free(path):
        try:
            return macos.free_bytes(path)
        except OSError:
            return None

    def _rescan(self, scan):
        """Mark the reviewed snapshot as changed and rescan the same grant."""
        engine = self.engine
        scan["stale"] = True
        with self.lock:
            self.stale.add((scan["scanId"], scan["scanVersion"]))
            self.candidates = {}
            self.tokens = {k: v for k, v in self.tokens.items() if v["scanId"] != scan["scanId"]}
        engine.view = {}
        try:
            status = engine.start(engine.grant.grant_id, opaque())
        except StorageError:
            return {"started": False}
        return {"started": True, "scanId": status["scanId"], "scanVersion": status["scanVersion"]}
