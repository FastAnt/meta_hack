"""Scenario tests: startup, turn lifecycle, every square state.

Covers the situations a user actually hits:
- attaching to agent history (startup) vs live events,
- every mode of the agent square (with a reachability audit so no
  caption exists for a mode no state can produce),
- observer-driven Codex-style turns (busy -> done window, and the
  interrupted window when an agent vanishes mid-turn),
- the scoring guard (never score a turn we did not watch).
"""
import importlib.util
import os
import pathlib
import sys
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


FRESH = {"at": time.time(), "tool": "bash", "command": "make all",
         "reason": "x"}


class ModeMatrixTests(unittest.TestCase):
    CASES = [
        ("idle", {"state": "idle"}, "blinky"),
        ("starting", {"state": "starting"}, "flash"),
        ("planning", {"state": "planning"}, "flash"),
        ("web search tool", {"state": "tool_running",
                             "tool": "web_search"}, "trail"),
        ("web fetch tool", {"state": "tool_running",
                            "tool": "web_fetch"}, "trail"),
        ("grep tool", {"state": "tool_running", "tool": "grep"},
         "glitch"),
        ("read tool", {"state": "tool_running", "tool": "read"},
         "glitch"),
        ("edit tool", {"state": "tool_running", "tool": "file_edit"},
         "build"),
        ("generic working", {"state": "working"}, "build"),
        ("completed", {"state": "completed"}, "done"),
        ("turn done window", {"state": "idle", "turn_done": True},
         "done"),
        ("approval", {"state": "awaiting_user", "needs": "approval"},
         "shuffle"),
        ("question", {"state": "awaiting_user", "needs": "answer"},
         "question"),
        ("interrupted", {"state": "idle", "interrupted": True}, "fail"),
        ("compile cmd", {"state": "tool_running", "tool": "bash",
                         "command": "g++ -std=c++20 -c main.cpp -o m.o"},
         "compile"),
        ("link cmd", {"state": "tool_running", "tool": "bash",
                      "command": "g++ main.o -o app"}, "link"),
        ("copy cmd", {"state": "tool_running", "tool": "bash",
                      "command": "cp a.txt b.txt"}, "copy"),
        ("install cmd", {"state": "tool_running", "tool": "bash",
                         "command": "sudo apt update"}, "install"),
        ("delete cmd", {"state": "tool_running", "tool": "bash",
                        "command": "rm -rf build/"}, "delete"),
        ("download cmd", {"state": "tool_running", "tool": "bash",
                          "command": "curl -O https://x.test/f.tgz"},
         "download"),
        ("test cmd", {"state": "tool_running", "tool": "bash",
                      "command": "python -m pytest -q"}, "test"),
        ("git cmd", {"state": "tool_running", "tool": "bash",
                     "command": "git push origin main"}, "git"),
        ("extract cmd", {"state": "tool_running", "tool": "bash",
                         "command": "tar -xf bundle.tar.gz"}, "extract"),
        ("serve cmd", {"state": "tool_running", "tool": "bash",
                       "command": "uvicorn main:app"}, "serve"),
    ]

    def test_matrix(self):
        native = _native()
        for name, agent, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(native.mode_for_agent(agent), expected)

    def test_every_caption_mode_is_reachable(self):
        native = _native()
        reached = {expected for _, _, expected in self.CASES}
        for cmd, mode in (("g++ -c a.cpp -o a.o", "fail_compile"),
                          ("g++ a.o -o app", "fail_link"),
                          ("pytest -q", "fail_test"),
                          ("frobnicate --all", "fail")):
            failure = {"at": time.time(), "tool": "bash",
                       "command": cmd, "reason": "boom"}
            got = native.mode_for_agent(
                {"state": "tool_running", "tool": "bash", "command": cmd,
                 "last_failure": failure})
            reached.add(got)
            self.assertEqual(got, mode)
        self.assertEqual(reached, set(native.MODE_CAPTIONS.keys()))

    def test_stale_failure_does_not_show(self):
        native = _native()
        stale = dict(FRESH, at=time.time() - 60)
        self.assertEqual(
            native.mode_for_agent({"state": "tool_running",
                                   "tool": "bash", "command": "make x",
                                   "last_failure": stale}),
            "compile")


def _adapter():
    from backend.app.muse_adapter import MuseAdapter
    evs = []
    return MuseAdapter(evs.append, lambda: ""), evs


def _msg(text):
    return {"payload_type": "runtime.session",
            "payload": {"event": {"kind": "assistant_message_committed",
                                  "text": text}}}


class AdapterLifecycleTests(unittest.TestCase):
    def test_user_intent_plans_and_stores_task(self):
        ad, _ = _adapter()
        ad._handle_record({
            "payload_type": "runtime.user_intent.accepted",
            "payload": {"model_messages": [
                {"content": [{"kind": "text",
                              "text": "Fix the flaky test"}]}]}})
        self.assertEqual(ad.state["state"], "planning")
        self.assertEqual(ad.state["task"], "Fix the flaky test")

    def test_bash_command_is_captured(self):
        ad, _ = _adapter()
        ad._handle_record({
            "payload_type": "runtime.session",
            "payload": {"event": {
                "kind": "assistant_tool_calls_committed",
                "tool_calls": [{"name": "bash",
                                "args": '{"command": "make all", '
                                        '"description": "Build"}'}]}}})
        self.assertEqual(ad.state["command"], "make all")
        self.assertEqual(ad.state["state"], "tool_running")

    def test_terminal_failure_becomes_last_failure(self):
        ad, _ = _adapter()
        ad._handle_record({
            "payload_type": "tool_batch.effect.terminal",
            "payload": {"record": {"outcome": {"task_completion": {
                "terminal": {"kind": "failed",
                             "reason": "process exited with status 2"}}}}}})
        self.assertIsNotNone(ad.state["last_failure"])
        self.assertIn("status 2", ad.state["last_failure"]["reason"])

    def test_approval_wait(self):
        ad, _ = _adapter()
        ad._handle_record({
            "payload_type": "approval_wait.effect.started",
            "payload": {"record": {"tool_name": "bash"}}})
        self.assertEqual(ad.state["state"], "awaiting_user")
        self.assertEqual(ad.state["needs"], "approval")

    def test_question_turn_via_quiet(self):
        ad, _ = _adapter()
        ad._handle_record(_msg("Which database should I use?"))
        ad.state["state"] = "working"
        ad._last_record_ts = time.time() - 10
        ad._check_quiet()
        self.assertEqual(ad.state["state"], "awaiting_user")
        self.assertEqual(ad.state["needs"], "answer")

    def test_model_failure_is_failed(self):
        ad, _ = _adapter()
        ad._handle_record({
            "payload_type": "runtime.session",
            "payload": {"event": {
                "kind": "model_failure_terminal_intent",
                "reason": "model unavailable"}}})
        self.assertEqual(ad.state["state"], "failed")

    def test_replayed_reply_cannot_complete_a_turn(self):
        ad, _ = _adapter()
        ad._in_replay = True
        ad._handle_record(_msg("Old summary from history."))
        ad._in_replay = False
        ad.state["state"] = "idle"
        ad._last_record_ts = time.time() - 10
        ad._check_quiet()
        self.assertEqual(ad.state["state"], "idle")


class CoreScenarioTests(unittest.TestCase):
    """Core driven with synthetic events/snapshots (no GUI)."""

    def setUp(self):
        import tempfile
        self._home = os.environ.get("HOME")
        self._tmp = tempfile.mkdtemp()
        os.environ["HOME"] = self._tmp
        self.native = _native()
        self.core = self.native.Core(self._tmp)
        self.core.liquid.api_key = ""  # deterministic heuristic scoring
        self.core.adapter.stop()

    def tearDown(self):
        try:
            self.core.observer_stop = True
        except Exception:
            pass
        if self._home:
            os.environ["HOME"] = self._home

    def _snap(self, tree, agent_pid, cli="codex", fg_cmd="codex"):
        return {
            "cwd": self._tmp,
            "foreground": {"pid": 999, "process": "codex",
                           "cmdline": fg_cmd},
            "shell_pid": 1, "command_running": True,
            "command_duration_s": 3,
            "agent_cli_detected": cli,
            "agent_pid": agent_pid,
            "process_tree": tree,
            "inferred": {"state": "working", "confidence": 0.5},
        }

    def test_codex_busy_then_done_window(self):
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "rg",
                 "cmdline": "rg --files"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["state"], "working")
        self.core._busy_started_at = time.time() - 10
        self.core.snapshot = self._snap([codex], 10)
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["state"], "idle")
        self.assertTrue(self.core.agent_state.get("turn_done"))
        self.assertEqual(
            self.native.mode_for_agent(self.core.agent_state), "done")

    def test_codex_vanishing_mid_turn_is_interrupted(self):
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "make",
                 "cmdline": "make all"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["state"], "working")
        self.core.snapshot = {"cwd": self._tmp,
                              "foreground": {"process": "bash"},
                              "agent_cli_detected": None,
                              "process_tree": [],
                              "inferred": {"state": "idle"}}
        self.core.refresh_agent_state()
        # No instant verdict: the ending is resolved from the
        # screen within seconds; unresolved, it times out.
        self.assertTrue(getattr(self.core, "_vanish", None))
        self.assertFalse(self.core.agent_state.get("interrupted"))
        self.core._vanish["at"] = time.time() - 20
        self.core.refresh_agent_state()
        self.assertTrue(self.core.agent_state.get("interrupted"))
        self.assertEqual(
            self.native.mode_for_agent(self.core.agent_state), "fail")

    def test_quick_phantom_completion_is_not_scored(self):
        self.core._on_agent_event({"state": "working",
                                   "session_id": "s1"})
        self.core._on_agent_event({"state": "completed",
                                   "session_id": "s1"})
        self.assertIsNone(self.core.results)
        self.assertTrue(any("completion skipped" in d
                            for d in self.core.decisions))

    def test_watched_completion_is_scored(self):
        self.core._on_agent_event({"state": "working",
                                   "session_id": "s2", "task": "Do it"})
        self.core.working_since = time.time() - 30
        self.core._on_agent_event({"state": "completed",
                                   "session_id": "s2"})
        self.assertIsNotNone(self.core.results)
        for _ in range(50):
            if self.core.results.get("source"):
                break
            time.sleep(0.1)
        self.assertEqual(self.core.results["source"],
                         "heuristic (no LIQUID_API_KEY)")


class ScreenMappingTests(unittest.TestCase):
    def test_screen_modes_are_real_modes(self):
        native = _native()
        from backend.app.liquid_client import SCREEN_MODES
        self.assertLessEqual(set(SCREEN_MODES.values()),
                             set(native.MODE_CAPTIONS.keys()))

    def test_classify_screen_choices(self):
        import backend.app.liquid_client as lc
        client = lc.LiquidClient.__new__(lc.LiquidClient)
        for choice, mode in (("finished", "done"),
                             ("asking_question", "question"),
                             ("waiting_for_user", "shuffle"),
                             ("running_tests", "test")):
            client.decide = lambda s, q, c=choice: {
                "screen_kind": {"type": "choice", "choice": c,
                                "confidence": 0.9}}
            out = client.classify_screen("screen text", "codex")
            self.assertEqual(out["mode"], mode)


class SimulatorScenarioTests(unittest.TestCase):
    def test_startup_and_end_script(self):
        from backend.app.simulator import evaluate
        native = _native()
        rows = evaluate(ROOT / "sims" / "lifecycle.jsonl",
                        native.mode_for_agent)
        self.assertTrue(rows)
        self.assertTrue(all(r["ok"] for r in rows), rows)


if __name__ == "__main__":
    unittest.main()


class SourcePriorityTests(CoreScenarioTests):
    def test_working_codex_beats_idle_muse_and_back(self):
        self.core.adapter.current_file = "/tmp/fake-session.jsonl"
        with self.core._lock:
            self.core.adapter_state = {"state": "idle", "agent": "muse"}
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "rg",
                 "cmdline": "rg --files"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["agent"], "codex")
        self.assertEqual(self.core.agent_state["state"], "working")
        with self.core._lock:
            self.core.adapter_state = {"state": "working",
                                       "agent": "muse"}
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["agent"], "muse")


class ScreenTurnTests(CoreScenarioTests):
    def _codex_idle_snapshot(self):
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        self.core.snapshot = self._snap([codex], 10)
        self.core.refresh_agent_state()

    def test_screen_turn_arms_done_window(self):
        core = self.core
        core._screen_turn_tick("codex", "thinking", 0.9)
        core._screen_turn_since = time.time() - 30
        core._screen_turn_tick("codex", "idle_at_prompt", 0.8)
        self._codex_idle_snapshot()
        self.assertTrue(core.agent_state.get("turn_done"))
        self.assertEqual(
            self.native.mode_for_agent(core.agent_state), "done")

    def test_short_screen_turn_does_not_arm(self):
        core = self.core
        core._screen_turn_tick("codex", "thinking", 0.9)
        core._screen_turn_tick("codex", "finished", 0.9)
        self._codex_idle_snapshot()
        self.assertFalse(core.agent_state.get("turn_done"))

    def test_failed_screen_turn_does_not_arm(self):
        core = self.core
        core._screen_turn_tick("codex", "writing_code", 0.9)
        core._screen_turn_tick("codex", "failed", 0.9)
        core._screen_turn_since = time.time() - 30
        core._screen_turn_tick("codex", "idle_at_prompt", 0.8)
        self._codex_idle_snapshot()
        self.assertFalse(core.agent_state.get("turn_done"))


class StickyOutcomeTests(CoreScenarioTests):
    def _drive_done(self):
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "rg",
                 "cmdline": "rg --files"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        self.core._busy_started_at = time.time() - 10
        self.core.snapshot = self._snap([codex], 10)
        self.core.refresh_agent_state()

    def test_done_sticks_across_refreshes(self):
        self._drive_done()
        self.assertTrue(self.core.agent_state.get("turn_done"))
        for _ in range(3):
            self.core.refresh_agent_state()
            self.assertTrue(self.core.agent_state.get("turn_done"))
            self.assertEqual(
                self.native.mode_for_agent(self.core.agent_state),
                "done")

    def test_new_turn_clears_outcome(self):
        self._drive_done()
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "rg",
                 "cmdline": "rg --files"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["state"], "working")
        self.assertFalse(self.core.agent_state.get("turn_done"))

    def test_new_agent_process_clears_outcome(self):
        self._drive_done()
        codex2 = {"pid": 20, "ppid": 1, "comm": "codex",
                  "cmdline": "codex --yolo"}
        self.core.snapshot = self._snap([codex2], 20)
        self.core.refresh_agent_state()
        self.assertFalse(self.core.agent_state.get("turn_done"))
        self.assertEqual(
            self.native.mode_for_agent(self.core.agent_state), "blinky")

    def test_sticky_modes_in_matrix(self):
        native = self.native
        self.assertEqual(native.mode_for_agent(
            {"state": "idle", "sticky_outcome": "done"}), "done")
        self.assertEqual(native.mode_for_agent(
            {"state": "idle", "sticky_outcome": "fail"}), "fail")


class VanishResolutionTests(CoreScenarioTests):
    def _drive_vanish(self):
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "rg",
                 "cmdline": "rg --files"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        self.assertEqual(self.core.agent_state["state"], "working")
        self.core.snapshot = {"cwd": self._tmp,
                              "foreground": {"process": "bash"},
                              "agent_cli_detected": None,
                              "process_tree": [],
                              "inferred": {"state": "idle"}}
        self.core.refresh_agent_state()
        self.assertTrue(getattr(self.core, "_vanish", None))
        self.assertFalse(self.core.agent_state.get("interrupted"))

    def test_finished_screen_resolves_to_sticky_done(self):
        self._drive_vanish()
        self.core._screen_turn_tick("codex", "thinking", 0.9)
        self.core._screen_turn_since = time.time() - 30
        self.core._screen_turn_tick("codex", "finished", 0.8)
        self.core.refresh_agent_state()
        self.assertEqual(
            self.native.mode_for_agent(self.core.agent_state), "done")

    def test_bare_prompt_resolves_to_interrupted(self):
        self._drive_vanish()
        self.core._screen_turn_tick("codex", "thinking", 0.9)
        self.core._screen_turn_since = time.time() - 30
        self.core._screen_turn_tick("codex", "idle_at_prompt", 0.8)
        self.core._vanish["at"] = time.time() - 7
        self.core.refresh_agent_state()
        self.assertTrue(self.core.agent_state.get("interrupted"))
        self.assertEqual(
            self.native.mode_for_agent(self.core.agent_state), "fail")

    def test_unresolved_vanish_times_out_to_interrupted(self):
        self._drive_vanish()
        self.core._vanish["at"] = time.time() - 20
        self.core.refresh_agent_state()
        self.assertTrue(self.core.agent_state.get("interrupted"))

    def test_observed_turn_gets_session_results(self):
        import pathlib as _pl
        codex = {"pid": 10, "ppid": 1, "comm": "codex",
                 "cmdline": "codex --yolo"}
        child = {"pid": 11, "ppid": 10, "comm": "rg",
                 "cmdline": "rg --files"}
        self.core.snapshot = self._snap([codex, child], 10)
        self.core.refresh_agent_state()
        time.sleep(0.4)  # baseline snapshot lands
        _pl.Path(self._tmp, "created_by_agent.py").write_text("x = 1\n")
        self.core._busy_started_at = time.time() - 12
        self.core.snapshot = self._snap([codex], 10)
        self.core.refresh_agent_state()
        self.assertIsNotNone(self.core.results)
        self.assertTrue(self.core.results.get("observed"))
        names = [f["file"] for f in self.core.results["files"]]
        self.assertIn("created_by_agent.py", names)
        for _ in range(50):
            if self.core.results.get("source") not in (None, "pending"):
                break
            time.sleep(0.1)
        self.assertEqual(self.core.results["source"],
                         "heuristic (no LIQUID_API_KEY)")
