"""Cleanup candidates: read-only rules that tier a finished scan's items with plain reasons.

Everything here is metadata: snapshot nodes, no-follow stats of marker files and
installed apps, and (bounded, batched) Spotlight attributes. Git status and
content hashing run only when the person switched the matching opt-in on.
"""
from datetime import datetime
import os
import re
import stat
import subprocess
import time
from urllib.parse import urlsplit
from .policy import StorageError

DAY = 86400
RECENT, ACTIVE, UNTOUCHED = 7 * DAY, 30 * DAY, 365 * DAY
SPOTLIGHT_MAX = 300
MIN_UNTOUCHED = MIN_ICLOUD = 10_000_000  # smaller old files are noise in a review list
MIN_DUPLICATE = 1_000_000  # matches the scanner's grouping threshold
DUPLICATE_MAX_IDS = 5000
GIT_MAX_REPOS = 50  # nested repos checked per folder; more and the folder is not offered
MARKERS = ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml", "Podfile",
           "build.gradle", "Gemfile", "go.mod", ".git")
REBUILDABLE = {"node_modules": "npm install", ".venv": "pip install", "venv": "pip install", "target": "cargo build",
               ".next": "rebuilding the project", ".nuxt": "rebuilding the project", "dist": "rebuilding the project",
               "build": "rebuilding the project", ".turbo": "rebuilding the project",
               ".parcel-cache": "rebuilding the project", "Pods": "pod install", "DerivedData": "Xcode",
               "__pycache__": "Python, automatically", ".gradle": "Gradle"}
PARTIAL = (".crdownload", ".part", ".download")
INSTALLERS = (".dmg", ".pkg", ".zip")
ARCHIVES = (".zip", ".tar.gz", ".tgz", ".tar", ".7z", ".rar")
REDOWNLOADABLE = INSTALLERS + ARCHIVES + (".iso", ".xip", ".gguf", ".safetensors", ".ckpt", ".onnx", ".pt", ".pth")
# Strong signals may override the scanner's "protected" label for that item only (spec Clarification 1).
STRONG = {"rebuildable", "installed_app", "app_copy", "partial_download", "redownloadable", "repo_clean"}
TIER_ORDER = {"safe": 0, "glance": 1, "your_call": 2}
VERSION_TAIL = re.compile(r"[-_ .](v?\d|darwin|macos|mac|osx|universal|arm64|aarch64|x64|x86_64|intel|apple|installer|setup)", re.I)
# A .zip named like an installed app is an installer only with a version or platform tail
# (a bare name or a year such as "-2023" may be a backup or an export).
ZIP_TAIL = re.compile(r"[-_ .](v?\d+(\.\d+)+|v\d+|darwin|macos|mac|osx|universal|arm64|aarch64|x64|x86_64|intel)(?=$|[-_ .])", re.I)


def _date(text):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


class Spotlight:
    """Batched `mdls` on the local executor: last-used date and download source only."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def metadata(self, paths):
        paths = [p for p in paths if isinstance(p, str) and p.startswith("/") and "\0" not in p][:SPOTLIGHT_MAX]
        if not paths:
            return {}
        try:
            out = self.run(["/usr/bin/mdls", "-raw", "-name", "kMDItemLastUsedDate", "-name", "kMDItemWhereFroms"]
                           + paths, capture_output=True, timeout=20, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        values = out.decode("utf-8", "replace").split("\0")
        if len(values) != 2 * len(paths):  # a vanished path shifts the output: trust nothing
            return {}
        # mdls prints attributes in name order: kMDItemLastUsedDate, then kMDItemWhereFroms.
        return {p: {"lastUsed": _date(values[2 * i]),
                    "whereFroms": re.findall(r'"((?:[^"\\]|\\.)*)"', values[2 * i + 1])}
                for i, p in enumerate(paths)}


class Checks:
    """Default environment checks; tests inject fakes with the same methods."""

    def __init__(self, home=None):
        self.home = (home or os.path.expanduser("~")).rstrip("/")
        self._synced_folders = {}

    def is_icloud_synced(self, abs_path):
        path = abs_path.casefold()
        under = lambda base: path == base.casefold() or path.startswith(base.casefold() + "/")
        if under(self.home + "/Library/Mobile Documents"):
            return True
        return any(under(self.home + "/" + folder) and self._desktop_documents(folder) for folder in ("Desktop", "Documents"))

    def _desktop_documents(self, folder):
        # iCloud Desktop & Documents keeps a same-named folder in iCloud Drive (stat only).
        if folder not in self._synced_folders:
            try:
                os.lstat(self.home + "/Library/Mobile Documents/com~apple~CloudDocs/" + folder)
                self._synced_folders[folder] = True
            except OSError:
                self._synced_folders[folder] = False
        return self._synced_folders[folder]

    def git_health(self, abs_path):
        try:
            from . import optin
        except ImportError:
            return {"clean": False, "pushed": False, "remoteHost": None}
        return optin.git_health(abs_path)

    def exact_duplicates(self, engine, node_ids):
        try:
            from . import optin
        except ImportError:
            return {}
        return optin.exact_duplicates(engine, node_ids)


def _exists(path):
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def _host(urls):
    for url in urls:
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and host:
            return host
    return None


def _strip(name, suffixes):
    lower = name.lower()
    return next((name[:-len(s)] for s in suffixes if lower.endswith(s) and len(name) > len(s)), None)


class _Scan:
    """Read-only view of a finished snapshot plus cached stat checks."""

    def __init__(self, nodes, root_id, base, now, applications):
        self.nodes, self.root_id, self.base, self.now = nodes, root_id, base, now
        self.applications = [os.path.expanduser(a) for a in applications]
        self.children, self.rel, self.shelter = {}, {root_id: "."}, {root_id: False}
        self.newest, self.blocked, self.linked, self.repos = {}, {}, {}, {}
        for nid, node in nodes.items():  # parents precede children
            parent = node["parentId"]
            self.children.setdefault(parent, []).append(nid)
            if parent is None:
                continue
            up = nodes.get(parent)
            self.rel[nid] = node["name"] if parent == root_id else self.rel.get(parent, "?") + "/" + node["name"]
            self.shelter[nid] = self.shelter.get(parent, False) or parent != root_id and (
                up["protection"] == "protected" or up["name"].lower().endswith(".app") or up["name"] in REBUILDABLE)
        for nid, node in reversed(list(nodes.items())):
            own_gaps = [g for g in node["gaps"] if g != "symlink"]
            self.newest[nid] = max(self.newest.get(nid, 0), node.get("modifiedNs") or 0 if node["kind"] == "file" else 0)
            self.blocked[nid] = self.blocked.get(nid, False) or bool(own_gaps) or node["placeholder"] == "yes" or bool(node.get("cloudOnlyFiles"))
            self.linked[nid] = self.linked.get(nid, False) or "symlink" in node["gaps"]
            parent = node["parentId"]
            if parent in nodes:
                self.newest[parent] = max(self.newest.get(parent, 0), self.newest[nid])
                self.blocked[parent] = self.blocked.get(parent, False) or self.blocked[nid]
                self.linked[parent] = self.linked.get(parent, False) or self.linked[nid]
        for nid, node in nodes.items():
            if node["name"] == ".git" and node["parentId"] in nodes:  # .git folders and gitlink files
                up = node["parentId"]
                repo = up
                while up is not None:
                    self.repos.setdefault(up, set()).add(repo)
                    up = nodes[up]["parentId"]
            if not self.newest[nid]:
                self.newest[nid] = node.get("modifiedNs") or 0  # empty folder: its own time
        self._project, self._active = {}, {}

    def abs(self, nid):
        return self.base if nid == self.root_id else os.path.join(self.base, self.rel[nid])

    def is_project(self, nid):
        if nid not in self._project:
            folder = self.abs(nid)
            self._project[nid] = self.nodes[nid]["kind"] == "directory" and any(_exists(os.path.join(folder, m)) for m in MARKERS)
        return self._project[nid]

    def in_project(self, nid):
        parent = self.nodes[nid]["parentId"]
        while parent is not None:
            if self.is_project(parent):
                return True
            parent = self.nodes[parent]["parentId"]
        return False

    def active(self, project):
        """Any file outside the project's rebuildable folders changed in the last 30 days."""
        if project not in self._active:
            limit, stack, found = (self.now - ACTIVE) * 1e9, list(self.children.get(project, [])), False
            while stack and not found:
                node = self.nodes[stack.pop()]
                if node["kind"] == "file":
                    found = (node.get("modifiedNs") or 0) >= limit
                elif node["kind"] == "directory" and node["name"] not in REBUILDABLE:
                    stack.extend(self.children.get(node["nodeId"], []))
            self._active[project] = found
        return self._active[project]

    def installed(self, app_name):
        """Same-named app in an Applications folder (stat only), or None."""
        for folder in self.applications:
            try:
                st = os.lstat(os.path.join(folder, app_name + ".app"))
            except (OSError, ValueError):
                continue
            if stat.S_ISDIR(st.st_mode):  # a link named like an app does not count
                return st
        return None

    def installer_app(self, name):
        stem = _strip(name, INSTALLERS)
        if not stem:
            return None
        if stem.lower().endswith(".app"):
            stem = stem[:-4]
        tail = VERSION_TAIL.search(stem)
        options = [stem] + ([stem[:tail.start()]] if tail and tail.start() > 0 else [])
        options += [o.replace("-", " ").replace("_", " ") for o in options]
        return next((o for o in dict.fromkeys(options) if o.strip() and self.installed(o)), None)


def _disposable(scan, nid):
    """Inside a rebuildable folder, app or download folder: may be removed as a whole."""
    up = scan.nodes[nid]["parentId"]
    while up is not None:
        name = scan.nodes[up]["name"]
        if name in REBUILDABLE or name.lower().endswith((".app", ".download")):
            return True
        up = scan.nodes[up]["parentId"]
    return False


def _duplicates(scan, flaggable):
    """Same name and size across the whole scan: duplicate nodeId -> kept nodeId.

    The kept copy is preferably outside rebuildable folders, apps and downloads, then
    one that cannot be flagged (for example inside a project), then the shortest path.
    """
    groups = {}
    for nid, node in scan.nodes.items():
        if (node["kind"] == "file" and "groupedFiles" not in node and not node["gaps"] and not node.get("hardlinkAliasOf")
                and int(node["rawLogicalBytes"]) >= MIN_DUPLICATE):
            groups.setdefault((node["name"], node["rawLogicalBytes"]), []).append(nid)
    pairs = {}
    for ids in groups.values():
        ids.sort(key=lambda i: (_disposable(scan, i), i in flaggable, len(scan.rel[i]), scan.rel[i]))
        pairs.update({extra: ids[0] for extra in ids[1:] if extra in flaggable})
    return pairs


def suggest(engine, scan_id, scan_version, within_node_id=None, *, now, spotlight,
            applications=("/Applications", "~/Applications"), opt_in=frozenset(), checks=None):
    checks = checks or Checks()
    with engine.lock:
        snapshot = engine.require_scan(scan_id, scan_version)
        engine._aggregate(snapshot)
        if snapshot["state"] == "running":
            raise StorageError("CONFLICT", "Wait for the scan to finish before asking for cleanup suggestions.")
        if not engine.share_names:
            raise StorageError("UNSUPPORTED", "Cleanup suggestions are available only when names are shared.")
        root_id = snapshot["rootNodeId"]
        within = within_node_id or root_id
        if within not in snapshot["nodes"]:
            raise StorageError("NOT_FOUND", "Unknown node in this snapshot.")
        # A finished snapshot is not mutated again (a rescan creates a new one), so the
        # stat and Spotlight checks below run without holding the engine lock.
        scan = _Scan(snapshot["nodes"], root_id, engine.grant.path, now, applications)
    nodes, recent = scan.nodes, (now - RECENT) * 1e9

    def basic(nid):
        node = nodes[nid]
        return (nid != root_id and "groupedFiles" not in node and not node["gaps"] and node["placeholder"] != "yes"
                and not node.get("hardlinkAliasOf") and node.get("hardlinkCount", 1) <= 1
                and not scan.shelter[nid] and scan.newest[nid] < recent)

    eligible = {nid for nid in nodes if basic(nid) and nodes[nid]["kind"] in ("file", "directory")}
    scope, stack = set(), [within]
    while stack:
        nid = stack.pop()
        scope.add(nid)
        stack.extend(scan.children.get(nid, []))

    def free_file(nid):  # files outside any project, open to every file rule
        return nid in eligible and nid in scope and nodes[nid]["kind"] == "file" and not scan.in_project(nid)

    looked_up = sorted((nid for nid in scope if free_file(nid) and (
        nodes[nid]["name"].lower().endswith(REDOWNLOADABLE) or int(nodes[nid]["rawLogicalBytes"]) >= MIN_UNTOUCHED)),
        key=lambda i: -int(nodes[i]["allocatedEstimateBytes"] or 0))[:SPOTLIGHT_MAX]
    meta = spotlight.metadata([scan.abs(nid) for nid in looked_up]) if looked_up else {}
    possible = _duplicates(scan, {nid for nid in scope if free_file(nid)})
    exact = {}
    if "exactDuplicates" in opt_in:
        sizes = {}
        for nid in eligible:
            if nodes[nid]["kind"] == "file" and int(nodes[nid]["rawLogicalBytes"]) >= MIN_DUPLICATE:
                sizes.setdefault(nodes[nid]["rawLogicalBytes"], []).append(nid)
        same_size = [i for ids in sizes.values() if len(ids) > 1 for i in ids][:DUPLICATE_MAX_IDS]
        exact = checks.exact_duplicates(engine, same_size) if same_size else {}

    health_cache = {}

    def healthy(repo):
        if repo not in health_cache:
            health = checks.git_health(scan.abs(repo)) or {}
            health_cache[repo] = health.get("clean") is True and health.get("pushed") is True
            health_cache[repo, "host"] = health.get("remoteHost")
        return health_cache[repo]

    def git_ok(nid):
        """No repo below (or at) this folder, or every one is pushed with nothing unsaved (opt-in)."""
        repos = scan.repos.get(nid, ())
        if not repos:
            return True
        if "gitHealth" not in opt_in or len(repos) > GIT_MAX_REPOS:
            return False
        return all(healthy(r) for r in sorted(repos, key=lambda r: scan.rel[r]))

    hits, duplicate_of = {}, {}
    for nid in scope:
        if nid not in eligible:
            continue
        node, name, found = nodes[nid], nodes[nid]["name"], []
        lower, size = name.lower(), int(node["rawLogicalBytes"])
        if node["kind"] == "directory":
            parent = node["parentId"]
            if name in REBUILDABLE and scan.is_project(parent) and not scan.blocked[nid] and git_ok(nid):
                found.append(("glance" if scan.active(parent) else "safe", "rebuildable", "Rebuilds with " + REBUILDABLE[name]))
            elif lower.endswith(".app") and not scan.blocked[nid] and not scan.in_project(nid) and git_ok(nid):
                there = scan.installed(name[:-4])
                try:
                    here = os.lstat(scan.abs(nid))
                except OSError:
                    here = None
                if there and here and (there.st_dev, there.st_ino) != (here.st_dev, here.st_ino):
                    found.append(("safe", "app_copy", "Copy in Applications"))
            elif ("gitHealth" in opt_in and nid in scan.repos.get(nid, ())
                  and not scan.blocked[nid] and not scan.linked[nid]):
                if git_ok(nid):  # the repo itself and every nested repo or submodule
                    host = health_cache.get((nid, "host"))
                    found.append(("safe", "repo_clean", f"Pushed to {host}, nothing unsaved" if host else "Pushed, nothing unsaved"))
            elif (lower.endswith(".download") and not scan.blocked[nid] and not scan.linked[nid]
                  and not scan.in_project(nid) and git_ok(nid)):
                found.append(("safe", "partial_download", "Unfinished download"))
        elif free_file(nid):
            info = meta.get(scan.abs(nid)) or {}
            if lower.endswith(PARTIAL):
                found.append(("safe", "partial_download", "Unfinished download"))
            host = _host(info.get("whereFroms") or []) if lower.endswith(REDOWNLOADABLE) else None
            app = scan.installer_app(name)
            if app:
                sure = not lower.endswith(".zip") or host or lower.endswith(".app.zip") or ZIP_TAIL.search(name[:-4])
                found.append(("safe" if sure else "glance", "installed_app", f"{app} is already installed"))
            if host:
                found.append(("safe", "redownloadable", f"Downloaded from {host}, can be fetched again"))
            if nid in exact or nid in possible:
                duplicate_of[nid] = True
            last = max(info.get("lastUsed") or 0, (node.get("modifiedNs") or 0) / 1e9)
            if size >= MIN_UNTOUCHED and last and now - last >= UNTOUCHED:
                found.append(("glance", "untouched", "Untouched since " + str(time.localtime(last).tm_year)))
            stem = _strip(name, ARCHIVES)
            if stem and any(nodes[s]["kind"] == "directory" and nodes[s]["name"] == stem for s in scan.children.get(node["parentId"], [])):
                found.append(("glance", "unzipped", "Already unzipped"))
            if size >= MIN_ICLOUD and node["placeholder"] == "no" and checks.is_icloud_synced(scan.abs(nid)):
                found.append((None, "icloud_copy", "Also in iCloud"))
        if node["protection"] == "protected":
            found = [f for f in found if f[1] in STRONG]
            duplicate_of.pop(nid, None)
        if found:
            hits[nid] = found

    # Duplicate reasons last: the kept copy must survive, so it may not be the item itself,
    # flagged as a duplicate in turn (this also breaks cycles), a candidate, inside one, or
    # inside a rebuildable folder, app or download folder (which a wider view may offer).
    flagged = set(exact) | set(possible)

    def keeps(nid, kept):
        if kept == nid or kept not in nodes or kept in flagged or _disposable(scan, kept):
            return False
        up = kept
        while up is not None:
            if up in hits:
                return False
            up = nodes[up]["parentId"]
        return True

    for nid in duplicate_of:
        if keeps(nid, exact.get(nid)):
            reason = ("safe", "exact_duplicate", "Exact duplicate of " + scan.rel[exact[nid]])
        elif keeps(nid, possible.get(nid)):
            reason = ("glance", "possible_duplicate", "Possible duplicate of " + scan.rel[possible[nid]])
        else:
            continue
        found = hits.setdefault(nid, [])
        found.insert(next((i for i, f in enumerate(found) if f[1] == "icloud_copy"), len(found)), reason)

    candidates = []
    with engine.lock:
        engine.require_scan(scan_id, scan_version)
        for nid, found in hits.items():
            parent = nodes[nid]["parentId"]
            while parent is not None and parent not in hits:
                parent = nodes[parent]["parentId"]
            if parent is not None:
                continue  # the ancestor candidate covers this item
            tiers = [t for t, _, _ in found if t]
            tier = min(tiers, key=TIER_ORDER.get) if tiers else "safe"
            actions = (["trash", "delete"] if tier == "safe" else ["trash"]) if tiers else []
            if any(code == "icloud_copy" for _, code, _ in found):
                actions.append("evict")
            summary = engine.summary(nodes[nid])
            candidates.append({
                "nodeId": nid, "displayLabel": summary["displayLabel"], "path": summary.get("path", scan.rel[nid]),
                "kind": nodes[nid]["kind"], "tier": tier, "preTicked": tier == "safe",
                "reasons": [{"code": code, "text": text} for _, code, text in found], "actions": actions,
                "freesBytes": nodes[nid]["allocatedEstimateBytes"] or "0", "logicalBytes": nodes[nid]["logicalBytes"],
                "modifiedAt": scan.newest[nid] / 1e9})
    candidates.sort(key=lambda c: (TIER_ORDER[c["tier"]], -int(c["freesBytes"]), c["path"]))
    total = lambda tier: str(sum(int(c["freesBytes"]) for c in candidates if c["tier"] == tier))
    return {"candidates": candidates, "totals": {"safeBytes": total("safe"), "glanceBytes": total("glance")}}
