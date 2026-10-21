"""Opt-in checks that read a little file data: git health and exact duplicate content.

Both only run when the person switched the matching option on. Nothing here writes,
follows links, or reads iCloud-only (dataless) files.
"""
import hashlib
import os
import re
import subprocess
from urllib.parse import urlsplit

MIN_DUPLICATE = 1_000_000
HASH_BUDGET = 2_000_000_000
CHUNK = 1_000_000
GIT_TIMEOUT = 10
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "PATH": "/usr/bin:/bin"}
SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?([^:/@]+):(?!//)")


def remote_host(url):
    """Host name of a git remote URL with any credentials, port, or path dropped."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        try:
            return urlsplit(url).hostname
        except ValueError:
            return None
    match = SCP_REMOTE.match(url)
    return match.group(1) if match and "/" not in url.split(":", 1)[0] else None


def _git(path, *args):
    result = subprocess.run(["git", "-C", path] + list(args), capture_output=True, timeout=GIT_TIMEOUT,
                            check=False, env=GIT_ENV)
    if result.returncode != 0:
        raise OSError("git failed")
    return result.stdout.decode("utf-8", "replace")


def _unquote(path):
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def git_health(repo_abs_path):
    """{"clean", "pushed", "remoteHost"}; any doubt means not clean, not pushed."""
    from .signals import REBUILDABLE  # imported late: signals imports this module lazily
    bad = {"clean": False, "pushed": False, "remoteHost": None}
    try:
        status = _git(repo_abs_path, "status", "--porcelain=v2", "--branch", "--ignored")
    except (OSError, subprocess.SubprocessError, ValueError):
        return bad
    clean, upstream, ahead_behind = True, False, None
    for line in status.splitlines():
        if line.startswith("# branch.upstream"):
            upstream = True
        elif line.startswith("# branch.ab "):
            ahead_behind = line.split(" ", 2)[2].strip()
        elif line.startswith("#") or not line:
            continue
        elif line.startswith("! "):
            parts = [p for p in _unquote(line[2:]).split("/") if p]
            if not any(p in REBUILDABLE for p in parts):
                clean = False  # ignored local data (databases, config) would be lost
        else:
            clean = False
    host = None
    try:
        host = remote_host(_git(repo_abs_path, "config", "--get", "remote.origin.url").strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"clean": clean, "pushed": upstream and ahead_behind == "+0 -0", "remoteHost": host}


def _open_relative(engine, relative):
    """Open a file below the chosen folder one component at a time, never following links."""
    parts = [p for p in relative.split("/") if p]
    if not parts or not engine.grant:
        return None
    opened = []
    try:
        current = engine.grant.fd
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=current)
            opened.append(current)
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
    except OSError:
        return None
    finally:
        for fd in opened:
            os.close(fd)


def _digest(engine, relative, size):
    fd = _open_relative(engine, relative)
    if fd is None:
        return None
    try:
        if os.fstat(fd).st_size != size:
            return None
        h, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
        return h.hexdigest() if total == size else None
    except OSError:
        return None
    finally:
        os.close(fd)


def exact_duplicates(engine, node_ids):
    """Map each duplicate nodeId to the kept nodeId (first by path) of its identical-content group."""
    by_size = {}
    with engine.lock:
        nodes = engine.scan["nodes"] if engine.scan else {}
        for node_id in dict.fromkeys(node_ids):
            node = nodes.get(node_id)
            if not node or node.get("kind") != "file" or node.get("placeholder") == "yes" or node.get("groupedFiles"):
                continue
            try:
                size = int(node["logicalBytes"])
            except (KeyError, TypeError, ValueError):
                continue
            if size >= MIN_DUPLICATE:
                by_size.setdefault(size, []).append((engine.relative_path(node), node_id))
    result, spent = {}, 0
    for size in sorted(by_size, reverse=True):
        group = sorted(by_size[size])
        if len(group) < 2:
            continue
        if spent + size * len(group) > HASH_BUDGET:
            continue
        spent += size * len(group)
        by_hash = {}
        for relative, node_id in group:
            digest = _digest(engine, relative, size)
            if digest:
                by_hash.setdefault(digest, []).append(node_id)
        for members in by_hash.values():
            for duplicate in members[1:]:
                result[duplicate] = members[0]
    return result
