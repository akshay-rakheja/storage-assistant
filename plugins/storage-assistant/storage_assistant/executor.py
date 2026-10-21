"""Performs approved cleanup actions. Deletion is descriptor-based and never follows links."""
import os
import stat
from typing import Dict, List, Optional, Tuple

from . import macos

_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)


class CleanupRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Executor:
    def trash(self, parent_fd: int, name: str, abs_path: str) -> Optional[str]:
        return macos.trash_item(abs_path)

    def evict(self, abs_path: str) -> None:
        macos.evict_download(abs_path)

    def delete(self, parent_fd: int, name: str, max_entries: int = 250000) -> int:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return 1
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            total = 1 + self._count(fd, max_entries - 1)
            if total > max_entries:
                raise CleanupRefused("too_many")
            removed = 1 + self._remove_children(fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)
        return removed

    def _count(self, fd: int, budget: int) -> int:
        """Count entries below directory fd; stop early once the budget is exceeded."""
        count = 0
        with os.scandir(fd) as it:
            for entry in it:
                count += 1
                if count > budget:
                    return count
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
                    try:
                        count += self._count(child, budget - count)
                    finally:
                        os.close(child)
                    if count > budget:
                        return count
        return count

    def _remove_children(self, fd: int) -> int:
        removed = 0
        with os.scandir(fd) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        for child_name, is_dir in entries:
            if is_dir:
                child = os.open(child_name, _DIR_FLAGS, dir_fd=fd)
                try:
                    removed += self._remove_children(child)
                finally:
                    os.close(child)
                os.rmdir(child_name, dir_fd=fd)
            else:
                os.unlink(child_name, dir_fd=fd)
            removed += 1
        return removed


class FakeExecutor(Executor):
    """Records calls; trash moves into a temp dir; delete is real."""

    def __init__(self, trash_dir: str, fail: Optional[Dict[str, Exception]] = None):
        self.trash_dir = trash_dir
        self.fail = fail or {}
        self.calls = []  # type: List[Tuple[str, str]]

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def trash(self, parent_fd: int, name: str, abs_path: str) -> Optional[str]:
        self._maybe_fail(name)
        self.calls.append(("trash", abs_path))
        target = os.path.join(self.trash_dir, name)
        os.rename(name, target, src_dir_fd=parent_fd)
        return target

    def evict(self, abs_path: str) -> None:
        self._maybe_fail(os.path.basename(abs_path))
        self.calls.append(("evict", abs_path))

    def delete(self, parent_fd: int, name: str, max_entries: int = 250000) -> int:
        self._maybe_fail(name)
        self.calls.append(("delete", name))
        return super().delete(parent_fd, name, max_entries)
