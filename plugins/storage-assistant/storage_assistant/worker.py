"""Isolated metadata traversal. stdout is private IPC to the local manager."""
import argparse
import errno
import json
import os
import stat
import time
import threading
from .policy import excluded, identity, opaque, placeholder, skipped_in_full


def category(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for label, extensions in {
        "video": {"mp4", "mov", "mkv", "webm"},
        "audio": {"mp3", "wav", "flac", "m4a"},
        "image": {"png", "jpg", "jpeg", "heic", "gif", "webp"},
        "archive": {"zip", "gz", "tar", "7z", "dmg"},
        "document": {"pdf", "txt", "docx", "pptx", "xlsx", "md"},
        "source": {"py", "js", "ts", "rs", "go", "swift", "json", "toml"},
    }.items():
        if ext in extensions:
            return label
    return "other"


def walk(root_fd, limits, emit, metadata=os.stat, enumerate_dir=os.scandir,
         now=time.monotonic, full=False):
    """Injectable metadata APIs are used only by unit tests, never by MCP.

    full: measure developer folders and iCloud-only files (by their metadata
    size, with no local allocation); credential stores stay skipped.
    """
    skip = skipped_in_full if full else excluded
    start = now()
    root_st = os.fstat(root_fd)
    device = root_st.st_dev
    root_id = opaque()
    count = 1
    stopped = False
    directories = set()

    def gap(node_id, reason):
        emit({"event": "gap", "nodeId": node_id, "reason": reason})

    def node(node_id, parent_id, name, st, kind, relative):
        emit({"event": "node", "nodeId": node_id, "parentId": parent_id,
              "name": name, "relative": relative, "kind": kind,
              "category": category(name) if kind == "file" else "other",
              "placeholder": placeholder(st, name),
              "rawLogicalBytes": str(st.st_size) if kind == "file" else "0",
              "rawAllocatedBytes": str(st.st_blocks * 512) if kind == "file" and hasattr(st, "st_blocks") else ("0" if kind != "file" else None),
              "identity": list(identity(st)), "modifiedNs": st.st_mtime_ns,
              "measuredAt": time.time(), "hardlinkCount": st.st_nlink if kind == "file" else 0,
              "protection": "protected" if excluded(name) or category(name) == "source" else "review_required"})

    node(root_id, None, "Selected folder", root_st, "directory", "")

    def visit(fd, node_id, depth, relative, ancestry):
        nonlocal count, stopped
        initial = os.fstat(fd)
        if not initial.st_nlink or identity(initial) in directories:
            gap(node_id, "changed")
            return
        directories.add(identity(initial))
        if depth >= limits["maxDepth"]:
            gap(node_id, "depth_limit")
            return
        try:
            with enumerate_dir(fd) as entries:
                for entry in entries:
                    if stopped:
                        break
                    if now() - start >= limits["maxDurationMs"] / 1000:
                        gap(node_id, "time_limit")
                        stopped = True
                        break
                    if count >= limits["maxEntries"]:
                        gap(node_id, "entry_limit")
                        stopped = True
                        break
                    # Every ancestor edge is checked through anchored descriptors.
                    if any(identity(metadata(n, dir_fd=p, follow_symlinks=False)) != expected
                           for p, n, expected in ancestry):
                        gap(node_id, "changed")
                        stopped = True
                        break
                    name = entry.name
                    count += 1
                    child_id = opaque()
                    try:
                        st = metadata(name, dir_fd=fd, follow_symlinks=False)
                    except OSError as exc:
                        gap(node_id, "denied" if exc.errno in (errno.EACCES, errno.EPERM) else "changed")
                        continue
                    kind = "directory" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "symlink" if stat.S_ISLNK(st.st_mode) else "other"
                    child_relative = relative + "/" + name
                    node(child_id, node_id, name, st, kind, child_relative)
                    if st.st_dev != device:
                        gap(child_id, "other_volume")
                    elif skip(name):
                        gap(child_id, "excluded")
                    elif placeholder(st, name) == "yes" and not (full and kind == "file" and not name.endswith(".icloud")):
                        # Dataless directories are never listed (listing can download them).
                        # In full mode, dataless files are counted from stat alone.
                        gap(child_id, "cloud_placeholder")
                    elif kind == "symlink":
                        gap(child_id, "symlink")
                    elif kind == "other":
                        gap(child_id, "unsupported")
                    elif kind == "directory":
                        child_fd = None
                        try:
                            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                            if identity(os.fstat(child_fd)) != identity(st):
                                gap(child_id, "changed")
                            else:
                                visit(child_fd, child_id, depth + 1, child_relative,
                                      ancestry + [(fd, name, identity(st))])
                        except OSError as exc:
                            gap(child_id, "denied" if exc.errno in (errno.EACCES, errno.EPERM) else "changed")
                        finally:
                            if child_fd is not None:
                                os.close(child_fd)
                    else:
                        try:
                            fresh = metadata(name, dir_fd=fd, follow_symlinks=False)
                        except OSError as exc:
                            # The first measurement is unvalidated. Attach the
                            # gap to this child; aggregation excludes its bytes.
                            gap(child_id, "denied" if exc.errno in (errno.EACCES, errno.EPERM) else "changed")
                            continue
                        if identity(fresh) != identity(st) or (fresh.st_size, fresh.st_mtime_ns, getattr(fresh, "st_blocks", None)) != (st.st_size, st.st_mtime_ns, getattr(st, "st_blocks", None)):
                            gap(child_id, "changed")
                        elif placeholder(st, name) == "unknown":
                            gap(child_id, "provider_unknown")
                    if kind == "file" and not hasattr(st, "st_blocks"):
                        gap(child_id, "allocation_unknown")
        except OSError as exc:
            gap(node_id, "denied" if exc.errno in (errno.EACCES, errno.EPERM) else "changed")
        final = os.fstat(fd)
        if identity(initial) != identity(final) or initial.st_mtime_ns != final.st_mtime_ns or not final.st_nlink:
            gap(node_id, "changed")

    visit(root_fd, root_id, 0, "", [])
    emit({"event": "done", "elapsedMs": round((now() - start) * 1000)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--limits", required=True)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    limits = json.loads(args.limits)
    parent_pid = os.getppid()
    def lifetime():
        deadline = time.monotonic() + limits["maxDurationMs"] / 1000 + .35
        while time.monotonic() < deadline:
            if os.getppid() != parent_pid:
                os._exit(125)
            time.sleep(.05)
        os._exit(124)
    threading.Thread(target=lifetime, daemon=True).start()
    def emit(event):
        print(json.dumps(event, ensure_ascii=True, separators=(",", ":")), flush=True)
    try:
        walk(args.fd, limits, emit, full=args.full)
    except Exception:
        emit({"event": "failure"})  # No path, traceback or OS error text.

if __name__ == "__main__":
    main()
