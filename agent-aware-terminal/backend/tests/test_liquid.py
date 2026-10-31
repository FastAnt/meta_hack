"""Liquid System-1 client parsing + git change extraction."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from backend.app.liquid_client import LiquidClient  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class LiquidClientTests(unittest.TestCase):
    def setUp(self):
        self._orig_post = requests.post

    def tearDown(self):
        requests.post = self._orig_post

    def test_predict_long_task_reads_noul(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(json)
            return FakeResponse({"answers": {
                "long_task": {"type": "noul", "noul": 0.87}}})

        requests.post = fake_post
        client = LiquidClient(api_key="liquid_test")
        p = client.predict_long_task("some state")
        self.assertAlmostEqual(p, 0.87)
        self.assertEqual(seen["model"], "d1.1")
        self.assertEqual(seen["questions"]["long_task"]["type"], "noul")

    def test_evaluate_session_reads_scores(self):
        def fake_post(url, headers=None, json=None, timeout=None):
            self.assertEqual(
                json["questions"]["completion"]["type"], "score")
            self.assertEqual(len(json["questions"]["safety"]["criteria"]), 5)
            return FakeResponse({"answers": {
                "completion": {"type": "score", "score": 3.4,
                               "confidence": 0.9},
                "safety": {"type": "score", "score": 2.1,
                           "confidence": 0.8}}})

        requests.post = fake_post
        out = LiquidClient(api_key="liquid_test").evaluate_session("s")
        self.assertEqual(out["completion"]["score"], 3.4)
        self.assertEqual(out["safety"]["score"], 2.1)

    def test_unconfigured_client_returns_none(self):
        client = LiquidClient(api_key="")
        self.assertIsNone(client.predict_long_task("s"))
        self.assertIsNone(client.evaluate_session("s"))


def _load_native():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GitChangesTests(unittest.TestCase):
    def test_numstat_and_untracked(self):
        native = _load_native()
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-C", tmp, *args], check=True,
                               capture_output=True, text=True)

            git("init", "-q")
            git("config", "user.email", "t@t.t")
            git("config", "user.name", "t")
            Path(tmp, "a.py").write_text("one\ntwo\n")
            git("add", "-A")
            git("commit", "-qm", "init")
            Path(tmp, "a.py").write_text("one\ntwo\nthree\n")
            Path(tmp, "b.py").write_text("new\n")
            files = {f["file"]: f for f in native.git_changes(tmp)}
            self.assertEqual(files["a.py"]["add"], 1)
            self.assertEqual(files["a.py"]["del"], 0)
            self.assertIn("b.py", files)  # untracked counts as added

    def test_non_repo_returns_none(self):
        native = _load_native()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(native.git_changes(tmp))


if __name__ == "__main__":
    unittest.main()


class AdapterStateTests(unittest.TestCase):
    def _adapter(self):
        from backend.app.muse_adapter import MuseAdapter
        evs = []
        return MuseAdapter(evs.append, lambda: ""), evs

    def test_tool_survives_status_events(self):
        ad, evs = self._adapter()
        ad._handle_record({"payload_type": "tool_batch.effect.started",
                           "payload": {"record": {"tool_name": "web_search"}}})
        ad._handle_record({"payload_type": "runtime.session",
                           "payload": {"event": {
                               "kind": "status",
                               "message": "tool action serialized: parallel eligibility not proven"}}})
        self.assertEqual(ad.state["tool"], "web_search")
        self.assertEqual(ad.state["state"], "tool_running")

    def test_tool_cleared_on_terminal(self):
        ad, evs = self._adapter()
        ad._handle_record({"payload_type": "tool_batch.effect.started",
                           "payload": {"record": {"tool_name": "web_search"}}})
        ad._handle_record({"payload_type": "tool_batch.effect.terminal",
                           "payload": {}})
        self.assertIsNone(ad.state["tool"])

    def test_quiet_turn_completion(self):
        import time as _time
        from backend.app import muse_adapter as ma
        ad, evs = self._adapter()
        ad._handle_record({"payload_type": "runtime.session",
                           "payload": {"event": {
                               "kind": "assistant_message_committed",
                               "text": "All done, here is the summary."}}})
        ad.state["state"] = "planning"
        ad._last_record_ts = _time.time() - 10
        ad._check_quiet()
        self.assertEqual(ad.state["state"], "completed")


class SessionDiffTests(unittest.TestCase):
    def test_only_session_changes_are_reported(self):
        native = _load_native()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "keep.py").write_text("old\n")
            Path(tmp, "dirty.py").write_text("pre-existing dirt\n")
            base = native.snapshot_files(tmp)
            Path(tmp, "keep.py").write_text("old\nnew line\n")
            Path(tmp, "created.py").write_text("a\nb\nc\n")
            files = {f["file"]: f
                     for f in native.session_diff(tmp, base)}
            self.assertIn("keep.py", files)
            self.assertIn("created.py", files)
            self.assertEqual(files["created.py"]["add"], 3)
            self.assertNotIn("dirty.py", files)  # untouched this session
