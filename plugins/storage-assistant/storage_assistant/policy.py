"""Trusted path opening. Paths enter here only from the local human UI/CLI."""
import errno
import ctypes
import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass

POLICY_VERSION = "metadata-v2"
DATALESS = 0x40000000  # Darwin UF_DATALESS, sys/stat.h
EXCLUDED = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".codex", ".agents", "etc", "bin", "sbin", "dev", "system",
            "library", "applications", "cloudstorage", "mobile documents",
            "docker", "docker.raw", "docker.qcow2", "node_modules", ".venv"}
SYSTEM = {"system", "library", "applications", "bin", "sbin", "usr", "dev", "etc"}
DEFAULT_LIMITS = {"maxEntries": 40000, "maxDepth": 8, "maxDurationMs": 3000}


class StorageError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def opaque():
    return uuid.uuid4().hex


def identity(st):
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode),
            getattr(st, "st_birthtime", None))


def placeholder(st, name=""):
    if name.endswith(".icloud") or getattr(st, "st_flags", 0) & DATALESS:
        return "yes"
    return "no" if hasattr(st, "st_flags") else "unknown"


def excluded(name):
    # Conservative even on case-sensitive volumes: protection never depends on
    # which spelling a caller used. Filesystem opens retain the original name.
    name = name.casefold()
    return name in EXCLUDED or name == ".env" or name.startswith(".env.") or name.endswith((".key", ".pem", ".p12", ".keychain-db", ".sqlite", ".sqlite3", ".db"))


CREDENTIALS = {".ssh", ".aws", ".azure", ".gnupg", "cloudstorage", "mobile documents"}


def skipped_in_full(name):
    """Full-coverage scans still skip credential stores and cloud-provider sync roots.

    Developer folders (.git, node_modules, .venv), Docker disk images and app
    databases are measured: their size matters for cleanup and only metadata
    is read. They keep the "protected" label so no future action targets them.
    """
    name = name.casefold()
    return (name in CREDENTIALS or name == ".env" or name.startswith(".env.")
            or name.endswith((".key", ".pem", ".p12", ".keychain-db")))


def validate_limits(requested, ceiling=None):
    ceiling = ceiling or DEFAULT_LIMITS
    if not isinstance(requested, dict) or set(requested) - set(ceiling):
        raise StorageError("INVALID_INPUT", "Unknown scan limit.")
    result = dict(ceiling)
    for key, value in requested.items():
        if type(value) is not int or not 1 <= value <= ceiling[key]:
            raise StorageError("INVALID_INPUT", "Limits may only narrow the grant.")
        result[key] = value
    return result


def local_filesystem(fd):
    """Darwin fstatfs64 layout from the installed SDK's public sys/mount.h.

    Names returned by the OS are discarded. No volume inventory is enumerated.
    Other operating systems are deliberately unsupported for live scanning.
    """
    if sys.platform != "darwin":
        raise StorageError("UNSUPPORTED", "Live scanning currently supports macOS only.")
    class StatFS(ctypes.Structure):
        _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
                    *[(n, ctypes.c_uint64) for n in ("blocks", "bfree", "bavail", "files", "ffree")],
                    ("fsid", ctypes.c_int32 * 2), ("owner", ctypes.c_uint32),
                    ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
                    ("fstypename", ctypes.c_char * 16), ("mntonname", ctypes.c_char * 1024),
                    ("mntfromname", ctypes.c_char * 1024), ("flags_ext", ctypes.c_uint32),
                    ("reserved", ctypes.c_uint32 * 7)]
    result = StatFS()
    function = ctypes.CDLL(None, use_errno=True).fstatfs64
    function.argtypes = [ctypes.c_int, ctypes.POINTER(StatFS)]
    function.restype = ctypes.c_int
    if function(fd, ctypes.byref(result)) != 0 or not result.flags & 0x1000 or result.flags & 0x00400000:
        raise StorageError("PROTECTED", "Network, automounted or unknown filesystems are excluded.")


def open_root(path):
    """Open each component without following links, including root ancestors.

    Only directory descriptors are opened. A cloud flag is checked before open.
    No canonicalization/following fallback and no permission retry.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise StorageError("INVALID_INPUT", "Choose one absolute folder.")
    path = os.path.expanduser(path)
    if not os.path.isabs(path) or any(c in (".", "..") for c in path.split("/")):
        raise StorageError("NOT_AUTHORIZED", "Choose an absolute folder without traversal.")
    path = os.path.normpath(path)
    policy_path = path.casefold()
    private_system = ("/private/etc", "/private/var/db", "/private/var/root", "/private/var/vm", "/private/var/audit", "/private/var/log", "/private/var/run", "/private/var/spool", "/private/var/backups")
    if policy_path == "/private/var" or any(policy_path == prefix or policy_path.startswith(prefix + "/") for prefix in private_system):
        raise StorageError("PROTECTED", "This system storage is excluded.")
    components = [c for c in path.split("/") if c]
    folded = [c.casefold() for c in components]
    if policy_path in ("/", "/private", "/users", os.path.normpath(os.path.expanduser("~")).casefold()) or not components or folded[0] == "users" and len(components) <= 2:
        raise StorageError("PROTECTED", "Choose a narrower folder than home or a volume root.")
    if folded[0] in SYSTEM | {"volumes", "network", "net"} or any(excluded(c) for c in components):
        raise StorageError("PROTECTED", "This protected or provider-managed folder is excluded.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for component in components:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise StorageError("NOT_AUTHORIZED", "Folder links are not followed.")
            if placeholder(before, component) == "yes":
                raise StorageError("PROTECTED", "Cloud-only folders are not opened.")
            next_fd = os.open(component, flags, dir_fd=fd)
            after = os.fstat(next_fd)
            if identity(before) != identity(after):
                os.close(next_fd)
                raise StorageError("IDENTITY_CHANGED", "Folder identity changed during selection.")
            os.close(fd)
            fd = next_fd
        st = os.fstat(fd)
        local_filesystem(fd)
        # Root of a mounted volume is too broad, even when explicitly entered.
        parent_st = os.stat("..", dir_fd=fd, follow_symlinks=False)
        if parent_st.st_dev != st.st_dev or identity(st) == identity(parent_st):
            raise StorageError("PROTECTED", "Choose a folder within the volume.")
        if not st.st_nlink:
            raise StorageError("IDENTITY_CHANGED", "Selected folder was removed.")
        return path, fd, identity(st)
    except StorageError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        code = "NOT_AUTHORIZED" if exc.errno in (errno.EACCES, errno.EPERM, errno.ELOOP) else "UNAVAILABLE"
        raise StorageError(code, "Folder selection failed; no permissions were changed.") from None


@dataclass
class Grant:
    path: str
    fd: int
    root_identity: tuple
    grant_id: str
    expires: float
    limits: dict

    @classmethod
    def select(cls, path, limits=None):
        path, fd, root_identity = open_root(path)
        return cls(path, fd, root_identity, opaque(), time.monotonic() + 1800,
                   dict(limits or DEFAULT_LIMITS))

    def validate(self):
        if time.monotonic() >= self.expires:
            raise StorageError("SCOPE_EXPIRED", "Select the folder again locally.")
        _, fresh, current = open_root(self.path)
        try:
            if current != self.root_identity or identity(os.fstat(self.fd)) != current:
                raise StorageError("IDENTITY_CHANGED", "Selected folder identity changed; select again.")
        finally:
            os.close(fresh)

    def close(self):
        os.close(self.fd)
