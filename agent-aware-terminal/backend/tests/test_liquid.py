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


class ModeClassificationTests(unittest.TestCase):
    def test_bash_command_modes(self):
        native = _load_native()
        m = native.mode_for_agent
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "cargo build --release"}), "compile")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "pip install tenacity"}), "install")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "cp -r assets/ dist/"}), "copy")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "ld -o app main.o"}), "link")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "python -m pytest -q"}), "test")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "agent": "codex",
                            "command": "ls -la"}), "build")
        self.assertEqual(m({"state": "tool_running", "tool": "web_fetch",
                            "command": None}), "trail")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "rm -rf cpp_app"}), "delete")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "sudo apt update"}), "install")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "curl -O https://x.dev/a.tgz"}),
                         "download")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "git push origin main"}), "git")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "tar -xzf a.tgz"}), "extract")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "uvicorn app:main"}), "serve")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "npm test"}), "test")


class FailureAndQuestionTests(unittest.TestCase):
    def test_adapter_records_failure(self):
        from backend.app.muse_adapter import MuseAdapter
        evs = []
        ad = MuseAdapter(evs.append, lambda: "")
        ad._handle_record({"payload_type": "tool_batch.effect.started",
                           "payload": {"record": {"tool_name": "bash"}}})
        ad.state["command"] = "cargo build"
        ad._handle_record({"payload_type": "tool_batch.effect.terminal",
                           "payload": {"record": {"outcome": {
                               "task_completion": {"terminal": {
                                   "kind": "failed",
                                   "reason": "process exited with status 101"}}}}}})
        self.assertIsNotNone(ad.state["last_failure"])
        self.assertEqual(ad.state["last_failure"]["command"], "cargo build")

    def test_adapter_question_turn(self):
        import time as _time
        from backend.app.muse_adapter import MuseAdapter
        evs = []
        ad = MuseAdapter(evs.append, lambda: "")
        ad._handle_record({"payload_type": "runtime.session",
                           "payload": {"event": {
                               "kind": "assistant_message_committed",
                               "text": "Should I also update the README?"}}})
        ad.state["state"] = "working"
        ad._last_record_ts = _time.time() - 10
        ad._check_quiet()
        self.assertEqual(ad.state["state"], "awaiting_user")
        self.assertEqual(ad.state["needs"], "answer")

    def test_modes(self):
        import time as _time
        native = _load_native()
        m = native.mode_for_agent
        self.assertEqual(m({"state": "awaiting_user", "needs": "answer"}),
                         "question")
        self.assertEqual(m({"state": "awaiting_user", "needs": "approval"}),
                         "shuffle")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "ls",
                            "last_failure": {"tool": "bash",
                                             "command": "cargo build",
                                             "at": _time.time()}}),
                         "fail_compile")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "g++ -std=c++20 -O2 -c main.cpp -o main.o"}),
                         "compile")
        self.assertEqual(m({"state": "tool_running", "tool": "bash",
                            "command": "g++ main.o -o boardreport"}), "link")


class ScreenClassificationTests(unittest.TestCase):
    def test_classify_screen_parses_choice(self):
        import backend.app.liquid_client as lc
        client = lc.LiquidClient.__new__(lc.LiquidClient)
        client.decide = lambda state, q: {
            "screen_kind": {"type": "choice", "choice": "installing",
                            "confidence": 0.9}}
        out = client.classify_screen("==> Installing standalone package",
                                     "codex")
        self.assertEqual(out["mode"], "install")
        client.decide = lambda state, q: {
            "screen_kind": {"type": "choice", "choice": "idle_at_prompt",
                            "confidence": 0.8}}
        self.assertEqual(client.classify_screen("menu>", "codex")["mode"],
                         "blinky")


class HealthCheckTests(unittest.TestCase):
    def test_health_check_outcomes(self):
        import backend.app.liquid_client as lc
        client = lc.LiquidClient.__new__(lc.LiquidClient)
        client.api_key = ""
        ok, why = client.health_check()
        self.assertFalse(ok)
        self.assertIn("no LIQUID_API_KEY", why)
        client.api_key = "liquid_test"
        client.model = "d1.1"
        client.decide = lambda s, q: {"ok": {"type": "noul", "noul": 0.9}}
        ok, _ = client.health_check()
        self.assertTrue(ok)
        client.decide = lambda s, q: {}
        ok, _ = client.health_check()
        self.assertFalse(ok)

    def test_turn_done_mode(self):
        native = _load_native()
        self.assertEqual(
            native.mode_for_agent({"state": "idle", "turn_done": True}),
            "done")


class ReplayHistoryTests(unittest.TestCase):
    def test_backlog_never_completes(self):
        import json as _json
        import tempfile
        from backend.app.muse_adapter import MuseAdapter
        events = []
        adapter = MuseAdapter(events.append, lambda: "/tmp", "/tmp/nope")
        lines = [
            {"record_type": "event",
             "payload_type": "runtime.session.metadata",
             "stream": {"id": "s1"},
             "payload": {"record": {"model_id": "muse-spark-1.3"}}},
            {"record_type": "event", "payload_type": "session.end",
             "stream": {"id": "s1"}, "payload": {}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            for rec in lines:
                fh.write(_json.dumps(rec) + "\n")
            path = fh.name
        adapter.current_file = path
        adapter.offset = 0
        adapter._replay_pending = True
        adapter._read_new()
        states = [e.get("state") for e in events]
        self.assertNotIn("completed", states)
        self.assertEqual(adapter.state["state"], "idle")
        # live session.end still completes
        adapter._emit(state="working")
        adapter._handle_record(lines[1])
        self.assertEqual(adapter.state["state"], "completed")
