"""Issue #15 regressions: generated targets and injected metadata only."""
import collections
import errno
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

from storage_assistant.engine import Engine
from storage_assistant.policy import Grant, StorageError, open_root
from storage_assistant.worker import walk
from test_storage import wait


def metadata(directory=False, inode=2, size=123, blocks=1):
    values = dict(st_dev=1, st_ino=inode,
                  st_mode=(stat.S_IFDIR if directory else stat.S_IFREG) | 0o700,
                  st_birthtime=1, st_nlink=1, st_size=0 if directory else size,
                  st_mtime_ns=1, st_flags=0)
    if blocks is not None:
        values["st_blocks"] = 0 if directory else blocks
    return types.SimpleNamespace(**values)


class Entries:
    def __init__(self, names):
        self.names = names

    def __enter__(self):
        return iter(types.SimpleNamespace(name=name) for name in self.names)

    def __exit__(self, *args):
        pass


def virtual_scan(files, failure=None):
    """Keep walk/index real; replace only filesystem metadata/enumeration.

    No real target or descriptor is accessed. Failure applies to the second
    metadata read of the first file; other siblings remain readable.
    """
    directory = metadata(directory=True, inode=1)
    calls = collections.Counter()
    events = []

    def measure(name, *, dir_fd, follow_symlinks):
        assert dir_fd == 123 and follow_symlinks is False
        calls[name] += 1
        if name == next(iter(files)) and calls[name] == 2 and failure:
            raise OSError(failure, "synthetic revalidation failure")
        return files[name]

    with patch("storage_assistant.worker.os.fstat", return_value=directory), \
            patch("storage_assistant.worker.os.open", side_effect=AssertionError("No content opens")):
        walk(123, {"maxEntries": 20, "maxDepth": 2, "maxDurationMs": 1000},
             events.append, metadata=measure, enumerate_dir=lambda fd: Entries(files))
    nodes, gaps = {}, collections.Counter()
    root_id = None
    for event in events:
        if event["event"] == "node":
            nodes[event["nodeId"]] = event | {"gaps": []}
            if event["parentId"] is None:
                root_id = event["nodeId"]
        elif event["event"] == "gap":
            nodes[event["nodeId"]]["gaps"].append(event["reason"])
            gaps[event["reason"]] += 1
    engine = Engine()
    engine.authorize = lambda *args: None  # Pure index tests have no live grant.
    engine.scan = dict(scanId="c" * 32, scanVersion=1,
                       state="partial" if gaps else "complete", asOf=0,
                       nodes=nodes, gapCounts=gaps, rootNodeId=root_id)
    engine._aggregate(engine.scan)
    return engine, events, calls


def query(engine, aggregate, basis="allocated_estimate"):
    return engine.query(dict(scanId="c" * 32, scanVersion=1,
                             basis=basis, aggregate=aggregate))["rows"]


class ProtectedCaseTests(unittest.TestCase):
    def test_case_alias_selection_rejects_same_protected_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            protected = Path(temporary).resolve() / ".ssh"
            protected.mkdir()
            (protected / "synthetic.txt").write_bytes(b"x" * 123)
            alias = protected.with_name(".SSH")
            if not alias.is_dir() or alias.stat().st_ino != protected.stat().st_ino:
                self.skipTest("Case-sensitive fixture volume: same-identity alias integration unavailable; lexical policy tested separately")
            for path in (protected, alias):
                with self.subTest(spelling=path.name):
                    with self.assertRaises(StorageError) as caught:
                        grant = Grant.select(str(path))
                        grant.close()
                    self.assertEqual(caught.exception.code, "PROTECTED")

    def test_virtual_protected_roots_are_rejected_before_any_open(self):
        paths = ["/uSeRs", "/uSeRs/Synthetic", "/pRiVaTe", "/pRiVaTe/vAr",
                 "/sYsTeM/folder", "/lIbRaRy/folder", "/aPpLiCaTiOnS/folder",
                 "/uSr/folder", "/vOlUmEs/synthetic/folder", "/nEtWoRk/folder",
                 "/NeT/folder", "/pRiVaTe/eTc/folder", "/pRiVaTe/vAr/dB/folder",
                 "/pRiVaTe/vAr/rOoT/folder", "/pRiVaTe/vAr/vM/folder",
                 "/pRiVaTe/vAr/aUdIt/folder", "/pRiVaTe/vAr/lOg/folder",
                 "/pRiVaTe/vAr/rUn/folder", "/pRiVaTe/vAr/sPoOl/folder",
                 "/pRiVaTe/vAr/bAcKuPs/folder", "/synthetic/hOmE"]
        # Home identity is represented lexically; no real home/system is opened.
        expand = os.path.expanduser
        with patch("storage_assistant.policy.os.path.expanduser", side_effect=lambda value: "/synthetic/Home" if value == "~" else expand(value)), \
                patch("storage_assistant.policy.os.open", side_effect=AssertionError("Protected root opened")):
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaises(StorageError) as caught:
                        open_root(path)
                    self.assertEqual(caught.exception.code, "PROTECTED")

    def test_protected_component_variants_reject_selection(self):
        names = [".SSH", ".AWS", ".AZURE", ".GNUPG", ".CODEX", ".AGENTS", ".GIT",
                 "lIbRaRy", "cLoUdStOrAgE", "mObIlE dOcUmEnTs", "dOcKeR",
                 "DOCKER.RAW", "DOCKER.QCOW2", "NODE_MODULES", ".VENV", ".ENV", ".ENV.local"]
        with patch("storage_assistant.policy.os.open", side_effect=AssertionError("Protected component opened")):
            for name in names:
                with self.subTest(name=name):
                    with self.assertRaises(StorageError) as caught:
                        open_root("/synthetic/selected/" + name + "/folder")
                    self.assertEqual(caught.exception.code, "PROTECTED")

    def test_protected_descendant_variants_are_not_entered_or_counted(self):
        names = [".SSH", ".GIT", ".AWS", ".CODEX", ".AGENTS", "lIbRaRy",
                 "cLoUdStOrAgE", "mObIlE dOcUmEnTs", "dOcKeR", "NODE_MODULES", ".VENV"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "selected"
            root.mkdir()
            for name in names:
                (root / name).mkdir()
                (root / name / "synthetic.txt").write_bytes(b"x" * 123)
            for name in ("DOCKER.RAW", "DOCKER.QCOW2", ".ENV", ".ENV.local"):
                (root / name).write_bytes(b"x" * 123)
            engine = Engine()
            try:
                engine.select(str(root))
                engine.start(engine.grant.grant_id, "a" * 32)
                status = wait(engine)
                self.assertEqual(status["logicalBytes"], "0")
                self.assertEqual(status["allocatedEstimateBytes"], "0")
                self.assertEqual(status["gapCounts"]["excluded"], 15)
                self.assertEqual(status["coverage"], "partial")
                self.assertNotIn("synthetic.txt", [n["name"] for n in engine.scan["nodes"].values()])
            finally:
                engine.close()


class UnknownGroupTests(unittest.TestCase):
    def assert_groups(self, files, logical_total, unknown_count):
        engine, _, _ = virtual_scan(files)
        self.addCleanup(engine.close)
        root = engine.scan["nodes"][engine.scan["rootNodeId"]]
        self.assertIsNone(root["allocatedEstimateBytes"])
        for operation in ("by_category", "by_kind"):
            with self.subTest(operation=operation):
                allocated = query(engine, operation)
                self.assertEqual(len(allocated), 1)
                self.assertIsNone(allocated[0]["bytes"])
                self.assertEqual(allocated[0]["unknownAllocationCount"], unknown_count)
                logical = query(engine, operation, "logical")
                self.assertEqual(logical[0]["bytes"], logical_total)
                self.assertEqual(logical[0]["unknownAllocationCount"], 0)

    def test_all_unknown_groups_keep_unknown_total(self):
        self.assert_groups({"unknown.pdf": metadata(blocks=None)}, "123", 1)

    def test_mixed_groups_and_hardlinks_do_not_report_known_subtotal_as_total(self):
        self.assert_groups({"a.pdf": metadata(inode=2, size=200, blocks=2),
                            "alias.pdf": metadata(inode=2, size=200, blocks=2),
                            "unknown.pdf": metadata(inode=3, size=123, blocks=None)}, "323", 1)

    def test_separate_known_and_unknown_groups_preserve_known_bytes(self):
        engine, _, _ = virtual_scan({"known.mov": metadata(inode=2, size=200, blocks=2),
                                     "unknown.pdf": metadata(inode=3, blocks=None)})
        self.addCleanup(engine.close)
        allocated = {r["displayLabel"]: r for r in query(engine, "by_category")}
        self.assertEqual(allocated["video"]["bytes"], "1024")
        self.assertEqual(allocated["video"]["unknownAllocationCount"], 0)
        self.assertIsNone(allocated["document"]["bytes"])
        self.assertEqual(allocated["document"]["unknownAllocationCount"], 1)


class FreshStatTests(unittest.TestCase):
    def check_failure(self, failure, reason):
        engine, events, calls = virtual_scan({"failed.pdf": metadata(),
                                             "sibling.pdf": metadata(inode=3, size=200, blocks=2)}, failure)
        self.addCleanup(engine.close)
        failed = next(n for n in engine.scan["nodes"].values() if n["name"] == "failed.pdf")
        self.assertIn({"event": "gap", "nodeId": failed["nodeId"], "reason": reason}, events)
        self.assertIn("sibling.pdf", [n["name"] for n in engine.scan["nodes"].values()])
        sibling = next(n for n in engine.scan["nodes"].values() if n["name"] == "sibling.pdf")
        self.assertEqual(calls["failed.pdf"], 2, "Failed fresh stat must not be retried")
        self.assertEqual(calls["sibling.pdf"], 2, "Safe siblings must continue")
        self.assertEqual(failed["coverage"], "partial")
        self.assertEqual(failed["gaps"], [reason])
        self.assertEqual((failed["logicalBytes"], failed["allocatedEstimateBytes"]), ("0", "0"))
        exposed = engine.get_node("c" * 32, 1, failed["nodeId"])["node"]
        self.assertEqual(exposed["coverage"], "partial")
        self.assertEqual(exposed["gaps"], [reason])
        self.assertEqual((exposed["logicalBytes"], exposed["allocatedEstimateBytes"]), ("0", "0"))
        self.assertEqual(sibling["coverage"], "complete")
        self.assertEqual((sibling["logicalBytes"], sibling["allocatedEstimateBytes"]), ("200", "1024"))
        root = engine.scan["nodes"][engine.scan["rootNodeId"]]
        self.assertEqual(root["coverage"], "partial")
        self.assertEqual((root["logicalBytes"], root["allocatedEstimateBytes"]), ("200", "1024"))
        self.assertEqual(engine.scan["coverage"], "partial")
        self.assertEqual(engine.scan["gapCounts"][reason], 1)
        for operation in ("children", "largest_files", "by_category", "by_kind"):
            for basis, total in (("logical", 200), ("allocated_estimate", 1024)):
                with self.subTest(operation=operation, basis=basis):
                    self.assertEqual(sum(int(r["bytes"]) for r in query(engine, operation, basis)), total)

    def test_second_stat_enoent_excludes_child_and_keeps_sibling(self):
        self.check_failure(errno.ENOENT, "changed")

    def test_second_stat_eacces_excludes_child_and_keeps_sibling(self):
        self.check_failure(errno.EACCES, "denied")


if __name__ == "__main__":
    unittest.main()
