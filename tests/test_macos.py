"""macOS primitives: free space always; Trash/evict only with STORAGE_REAL_MACOS_TESTS=1."""
import os
import tempfile
import unittest

from storage_assistant import macos

REAL = os.environ.get("STORAGE_REAL_MACOS_TESTS") == "1"


class MacosTests(unittest.TestCase):
    def test_free_bytes_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertGreater(macos.free_bytes(directory), 0)

    @unittest.skipUnless(REAL, "set STORAGE_REAL_MACOS_TESTS=1 to touch the real Trash")
    def test_trash_item_real(self):
        handle, path = tempfile.mkstemp(prefix="storage-assistant-test-")
        os.close(handle)
        result = macos.trash_item(path)
        try:
            self.assertFalse(os.path.exists(path))
            self.assertIsNotNone(result)
            self.assertTrue(os.path.exists(result))
            os.rename(result, path)  # Put back
            self.assertTrue(os.path.exists(path))
        finally:
            for leftover in (path, result):
                if leftover and os.path.exists(leftover):
                    os.remove(leftover)

    @unittest.skipUnless(REAL, "set STORAGE_REAL_MACOS_TESTS=1 to call Foundation")
    def test_evict_requires_icloud_item(self):
        handle, path = tempfile.mkstemp(prefix="storage-assistant-test-")
        os.close(handle)
        try:
            with self.assertRaises(OSError):
                macos.evict_download(path)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
