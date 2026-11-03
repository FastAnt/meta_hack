"""A turn that ends before its baseline snapshot lands must
report files as unmeasured — never as "every file in the project".

Regression (screenshot, prime-agent in ~/dev/music_compiler):
the agent only read files and answered a question, yet the
result pane showed FILES · 146 — exactly the project's file
count. The baseline snapshot runs on a thread and used to park
a placeholder {} in _baseline; a turn ending in that window
diffed the tree against the empty dict and counted every file
as newly created, marked measured. The late snapshot thread
then resurrected its stale result as the next turn's baseline.
"""
import importlib.util
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _native():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NT = _native()


class BaselineRaceTests(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        for name in ("a.txt", "b.txt"):
            pathlib.Path(self.proj, name).write_text("x\n")
        self.core = NT.Core(self.proj)
        # Isolate from the live session-log adapter: in legacy
        # mode it replays real Muse history into _on_agent_event
        # and would start baselines of its own mid-test.
        try:
            self.core.adapter.stop()
            if hasattr(self.core.adapter, "join"):
                # stop() is only a flag: wait out the thread so a
                # replayed event cannot land mid-test.
                self.core.adapter.join(timeout=3)
        except Exception:
            pass
        self.core._drop_baseline()
        self._real_snapshot = NT.snapshot_files

    def tearDown(self):
        NT.snapshot_files = self._real_snapshot

    def _slow_snapshot(self, delay=0.4):
        real = self._real_snapshot

        def slow(root):
            time.sleep(delay)
            return real(root)
        NT.snapshot_files = slow

    def test_finish_while_snapshot_pending_is_unmeasured(self):
        self._slow_snapshot()
        self.core._start_baseline()
        self.core._finish_observed_turn("prime", "completed", 0)
        self.assertEqual(self.core.results["files"], [])
        self.assertFalse(self.core.results["files_measured"])

    def test_late_snapshot_does_not_become_next_baseline(self):
        self._slow_snapshot(delay=0.2)
        self.core._start_baseline()
        self.core._finish_observed_turn("prime", "completed", 0)
        time.sleep(0.45)  # let the snapshot thread land
        self.assertIsNone(self.core._baseline)

    def test_ready_baseline_still_measures_real_changes(self):
        self.core._start_baseline()
        deadline = time.time() + 5
        while not self.core._baseline and time.time() < deadline:
            time.sleep(0.02)
        # An edit after the snapshot is real, measured truth.
        pathlib.Path(self.proj, "a.txt").write_text("x\ny\n")
        # mtime granularity: force a different signature.
        import os
        st = os.stat(pathlib.Path(self.proj, "a.txt"))
        os.utime(pathlib.Path(self.proj, "a.txt"),
                 (st.st_atime, st.st_mtime + 2))
        self.core._finish_observed_turn("prime", "completed", 5)
        files = [f["file"] for f in self.core.results["files"]]
        self.assertEqual(files, ["a.txt"])
        self.assertTrue(self.core.results["files_measured"])


if __name__ == "__main__":
    unittest.main()
