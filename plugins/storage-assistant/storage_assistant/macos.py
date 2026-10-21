"""macOS primitives for cleanup: Trash, iCloud eviction, Finder Trash, free space.

No shell is used; paths are only ever passed as argv items or Foundation strings.
"""
import ctypes
import ctypes.util
import os
import subprocess
from typing import Optional

TRASH_TOOL = "/usr/bin/trash"
OPEN_TOOL = "/usr/bin/open"


def free_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def open_trash() -> None:
    try:
        subprocess.run([OPEN_TOOL, os.path.expanduser("~/.Trash")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (subprocess.SubprocessError, OSError) as error:
        raise OSError("could not open the Trash: %s" % error)


def trash_item(path: str) -> Optional[str]:
    """Move one item to the Trash via Foundation and return its Trash path.

    Falls back to /usr/bin/trash (returning None) only if Foundation fails."""
    try:
        return _foundation_trash(path)
    except OSError as foundation_error:
        if not os.path.exists(TRASH_TOOL):
            raise
        if not os.path.lexists(path):
            raise foundation_error
    try:
        done = subprocess.run([TRASH_TOOL, path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=60)
    except (subprocess.SubprocessError, OSError) as error:
        raise OSError("trash failed: %s" % error)
    if done.returncode != 0:
        raise OSError("trash failed: %s" % done.stderr.decode("utf-8", "replace").strip())
    return None


def evict_download(path: str) -> None:
    """Evict a downloaded iCloud item back to a placeholder."""
    objc, manager, url = _manager_and_url(path)
    error = ctypes.c_void_p()
    ok = _send(objc, manager, "evictUbiquitousItemAtURL:error:", ctypes.c_bool,
               [ctypes.c_void_p, ctypes.c_void_p], [url, ctypes.byref(error)])
    if not ok:
        raise OSError("evict failed: %s" % _error_text(objc, error))


def _foundation_trash(path: str) -> Optional[str]:
    objc, manager, url = _manager_and_url(path)
    result = ctypes.c_void_p()
    error = ctypes.c_void_p()
    ok = _send(objc, manager, "trashItemAtURL:resultingItemURL:error:", ctypes.c_bool,
               [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
               [url, ctypes.byref(result), ctypes.byref(error)])
    if not ok:
        raise OSError("trash failed: %s" % _error_text(objc, error))
    if not result.value:
        return None
    return _ns_string(objc, _send(objc, result, "path", ctypes.c_void_p, [], []))


_objc_library = None


def _load():
    global _objc_library
    if _objc_library is None:
        ctypes.CDLL(ctypes.util.find_library("Foundation"))
        library = ctypes.CDLL(ctypes.util.find_library("objc"))
        library.objc_getClass.restype = ctypes.c_void_p
        library.objc_getClass.argtypes = [ctypes.c_char_p]
        library.sel_registerName.restype = ctypes.c_void_p
        library.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc_library = library
    return _objc_library


def _send(objc, target, selector, restype, argtypes, args):
    """Call objc_msgSend with a fresh prototype (required on Apple silicon)."""
    target = target.value if isinstance(target, ctypes.c_void_p) else target
    prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    function = prototype(("objc_msgSend", objc))
    return function(target, objc.sel_registerName(selector.encode()), *args)


def _manager_and_url(path: str):
    objc = _load()
    manager = _send(objc, objc.objc_getClass(b"NSFileManager"), "defaultManager",
                    ctypes.c_void_p, [], [])
    string = _send(objc, objc.objc_getClass(b"NSString"), "stringWithUTF8String:",
                   ctypes.c_void_p, [ctypes.c_char_p], [os.fsencode(path)])
    url = _send(objc, objc.objc_getClass(b"NSURL"), "fileURLWithPath:",
                ctypes.c_void_p, [ctypes.c_void_p], [string])
    if not manager or not url:
        raise OSError("Foundation is unavailable")
    return objc, manager, url


def _ns_string(objc, string) -> Optional[str]:
    if not string:
        return None
    raw = _send(objc, string, "UTF8String", ctypes.c_char_p, [], [])
    return os.fsdecode(raw) if raw else None


def _error_text(objc, error) -> str:
    if not error.value:
        return "unknown error"
    description = _send(objc, error, "localizedDescription", ctypes.c_void_p, [], [])
    return _ns_string(objc, description) or "unknown error"
