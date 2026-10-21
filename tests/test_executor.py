"""Executor: descriptor-based deletion that never follows links."""
import os
import tempfile
import unittest

from storage_assistant.executor import CleanupRefused, Executor, FakeExecutor


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


class ExecutorTests(unittest.TestCase):
    def test_delete_removes_tree_without_following_links(self):
        with tempfile.TemporaryDirectory() as base:
            outside = os.path.join(base, "outside")
            os.mkdir(outside)
            open(os.path.join(outside, "keep.txt"), "w").close()
            parent = os.path.join(base, "parent")
            tree = os.path.join(parent, "tree")
            os.makedirs(os.path.join(tree, "a", "b"))
            open(os.path.join(tree, "a", "b", "f.txt"), "w").close()
            os.symlink(outside, os.path.join(tree, "a", "link"))
            os.symlink(os.path.join(outside, "keep.txt"), os.path.join(tree, "flink"))
            fd = _open_dir(parent)
            try:
                removed = Executor().delete(fd, "tree")
            finally:
                os.close(fd)
            self.assertFalse(os.path.lexists(tree))
            self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))
            self.assertEqual(removed, 6)  # tree, a, b, f.txt, link, flink

    def test_delete_symlink_and_file(self):
        with tempfile.TemporaryDirectory() as base:
            os.mkdir(os.path.join(base, "target"))
            os.symlink(os.path.join(base, "target"), os.path.join(base, "ln"))
            open(os.path.join(base, "f"), "w").close()
            fd = _open_dir(base)
            try:
                self.assertEqual(Executor().delete(fd, "ln"), 1)
                self.assertEqual(Executor().delete(fd, "f"), 1)
            finally:
                os.close(fd)
            self.assertTrue(os.path.isdir(os.path.join(base, "target")))
            self.assertFalse(os.path.lexists(os.path.join(base, "ln")))

    def test_delete_refuses_over_cap_before_removing(self):
        with tempfile.TemporaryDirectory() as base:
            tree = os.path.join(base, "tree")
            os.mkdir(tree)
            for i in range(20):
                open(os.path.join(tree, "f%d" % i), "w").close()
            fd = _open_dir(base)
            try:
                with self.assertRaises(CleanupRefused) as ctx:
                    Executor().delete(fd, "tree", max_entries=10)
            finally:
                os.close(fd)
            self.assertEqual(ctx.exception.reason, "too_many")
            self.assertEqual(len(os.listdir(tree)), 20)

    def test_fake_trash_moves_item(self):
        with tempfile.TemporaryDirectory() as base:
            trash = os.path.join(base, "trash")
            os.mkdir(trash)
            parent = os.path.join(base, "parent")
            os.mkdir(parent)
            open(os.path.join(parent, "x.txt"), "w").close()
            fake = FakeExecutor(trash)
            fd = _open_dir(parent)
            try:
                result = fake.trash(fd, "x.txt", os.path.join(parent, "x.txt"))
            finally:
                os.close(fd)
            self.assertEqual(result, os.path.join(trash, "x.txt"))
            self.assertTrue(os.path.exists(result))
            self.assertFalse(os.path.exists(os.path.join(parent, "x.txt")))
            self.assertEqual(fake.calls, [("trash", os.path.join(parent, "x.txt"))])

    def test_fake_fail_raises_for_named_item_and_records_nothing(self):
        with tempfile.TemporaryDirectory() as base:
            parent = os.path.join(base, "parent")
            os.mkdir(parent)
            open(os.path.join(parent, "bad"), "w").close()
            fake = FakeExecutor(base, fail={"bad": PermissionError("no")})
            fd = _open_dir(parent)
            try:
                with self.assertRaises(PermissionError):
                    fake.trash(fd, "bad", os.path.join(parent, "bad"))
                with self.assertRaises(PermissionError):
                    fake.delete(fd, "bad")
                with self.assertRaises(PermissionError):
                    fake.evict(os.path.join(parent, "bad"))
            finally:
                os.close(fd)
            self.assertEqual(fake.calls, [])
            self.assertTrue(os.path.exists(os.path.join(parent, "bad")))


if __name__ == "__main__":
    unittest.main()
