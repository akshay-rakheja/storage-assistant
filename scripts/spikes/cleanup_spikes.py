#!/usr/bin/env python3
"""Manual spikes for the cleanup feature. Run by a developer; results go in the spike doc.

  evict PATH   evict a DOWNLOADED iCloud file and confirm it became dataless
  putback      trash a temp file, then try to rename it back to where it came from
  mdls N       time batched mdls over N paths
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "storage-assistant"))
from storage_assistant import macos  # noqa: E402

DATALESS = 0x40000000


def evict(path):
    before = os.lstat(path)
    print("before: flags=%#x blocks=%d" % (before.st_flags, before.st_blocks))
    macos.evict_download(path)
    after = os.lstat(path)
    print("after:  flags=%#x blocks=%d dataless=%s" % (
        after.st_flags, after.st_blocks, bool(after.st_flags & DATALESS)))


def putback():
    handle, path = tempfile.mkstemp(prefix="storage-assistant-spike-")
    os.close(handle)
    trashed = macos._foundation_trash(path)  # Foundation reports the resulting Trash path
    print("trashed to:", trashed)
    if not trashed:
        print("RESULT: resulting Trash path unknown; Put back not possible -> Open Trash")
        return
    try:
        os.rename(trashed, path)
    except OSError as error:
        print("rename failed: %r" % error)
        print("RESULT: Put back not possible without Full Disk Access -> fall back to Open Trash")
        return
    print("RESULT: Put back works without Full Disk Access")
    os.remove(path)


def mdls(count):
    root = os.path.expanduser("~/Library")
    paths = []
    for directory, _, names in os.walk(root):
        paths.extend(os.path.join(directory, name) for name in names)
        if len(paths) >= count:
            break
    paths = paths[:count]
    started = time.time()
    done = subprocess.run(["/usr/bin/mdls", "-name", "kMDItemLastUsedDate",
                           "-name", "kMDItemWhereFroms"] + paths,
                          capture_output=True, text=True)
    print("paths=%d seconds=%.2f rc=%d output_lines=%d" % (
        len(paths), time.time() - started, done.returncode, len(done.stdout.splitlines())))


def main(argv):
    if len(argv) >= 3 and argv[1] == "evict":
        evict(argv[2])
    elif len(argv) == 2 and argv[1] == "putback":
        putback()
    elif len(argv) >= 2 and argv[1] == "mdls":
        mdls(int(argv[2]) if len(argv) > 2 else 300)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
