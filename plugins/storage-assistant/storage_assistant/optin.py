"""Opt-in checks that read a little file data: git health and exact duplicate content.

Both only run when the person switched the matching option on. Nothing here writes,
follows links, or reads iCloud-only (dataless) files.
"""
import hashlib
import os
import re
import stat
import subprocess
from urllib.parse import urlsplit

MIN_DUPLICATE = 1_000_000
HASH_BUDGET = 2_000_000_000
CHUNK = 1_000_000
GIT_TIMEOUT = 10
# No HOME, no system or global config and no system attributes: only the repo's own config is read,
# and that is checked against GIT_CONFIG_ALLOWED before git status runs.
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null"]
# Without the developer tools, /usr/bin/git is a shim that opens an install dialog.
XCODE_SELECT = ["/usr/bin/xcode-select", "-p"]
GIT_CONFIG_ALLOWED = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
                      "core.ignorecase", "core.precomposeunicode"}
GIT_CONFIG_SUBSECTION = {"remote": {"url", "fetch"}, "branch": {"remote", "merge"}}
ATTRIBUTES_MAX = 65536
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


def _developer_tools():
    try:
        return subprocess.run(XCODE_SELECT, capture_output=True, timeout=GIT_TIMEOUT, check=False,
                              env={"PATH": "/usr/bin:/bin"}).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _git(path, *args):
    env = dict(GIT_ENV, GIT_CEILING_DIRECTORIES=os.path.dirname(path.rstrip("/")) or "/")
    result = subprocess.run(["git", "-C", path] + GIT_SAFE + list(args), capture_output=True, timeout=GIT_TIMEOUT,
                            check=False, env=env)
    if result.returncode != 0:
        raise OSError("git failed")
    return result.stdout.decode("utf-8", "replace")


def _config_allowed(key):
    if key in GIT_CONFIG_ALLOWED:
        return True
    section, _, rest = key.partition(".")
    subsection, _, variable = rest.rpartition(".")
    return bool(subsection) and variable in GIT_CONFIG_SUBSECTION.get(section, ())


def _repo_config(path):
    """The repo's own config (read without running anything), or None if any key is outside the allowlist."""
    listed = _git(path, "config", "--local", "--no-includes", "--list", "-z")
    config = {}
    for entry in listed.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        if not _config_allowed(key):
            return None
        config.setdefault(key, value)
    return config


def _mentions_filter(dir_fd, name):
    """True when a small attributes file names a filter driver, or cannot be read safely; False when absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > ATTRIBUTES_MAX:
            return True
        return b"filter=" in os.read(fd, ATTRIBUTES_MAX + 1)
    except OSError:
        return True
    finally:
        os.close(fd)


def _attributes_name_filters(repo_abs_path):
    try:
        repo_fd = os.open(repo_abs_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        return True
    try:
        if _mentions_filter(repo_fd, ".gitattributes"):
            return True
        try:
            info_fd = os.open(".git/info", os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=repo_fd)
        except FileNotFoundError:
            return False
        except OSError:
            return os.path.isdir(os.path.join(repo_abs_path, ".git", "info"))
        try:
            return _mentions_filter(info_fd, "attributes")
        finally:
            os.close(info_fd)
    finally:
        os.close(repo_fd)


def _unquote(path):
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def git_health(repo_abs_path):
    """{"clean", "pushed", "remoteHost"}; any doubt means not clean, not pushed.

    git status honours repo config that can run commands (core.fsmonitor, filter drivers named in
    .gitattributes), so status only runs when the repo's config holds nothing but allowlisted keys and
    no attributes file names a filter.
    """
    from .signals import REBUILDABLE  # imported late: signals imports this module lazily
    bad = {"clean": False, "pushed": False, "remoteHost": None}
    if not _developer_tools():
        return bad
    try:
        config = _repo_config(repo_abs_path)
        if config is None or _attributes_name_filters(repo_abs_path):
            return bad
        status = _git(repo_abs_path, "status", "--porcelain=v2", "--branch", "--ignored", "--ignore-submodules=all")
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
    host = remote_host(config.get("remote.origin.url", ""))
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
