"""Unified System-1 screen perception: one state machine for
every agent (Muse included), no per-agent log parsing.

Regression target (decisions.log 09:43-09:47): Codex worked for
minutes while CPU/screen-quiet heuristics closed a fake 5 s
turn every ~20 s and dropped the square back to blinky."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.screen_perception import ScreenPerception, Verdict  # noqa: E402


def v(choice, conf=0.6, **kw):
    return Verdict(choice=choice, confidence=conf, **kw)


class PerceptionTests(unittest.TestCase):
    def setUp(self):
        self.t = 1000.0
        self.p = ScreenPerception()

    def feed(self, verdict):
        self.t += 4.0
        return self.p.update(verdict, now=self.t)

    def work(self):
        ev = self.feed(v("thinking", 0.62))
        ev = self.feed(v("thinking", 0.57)) or ev
        self.assertEqual(self.p.state, "working")
        return ev

    def test_network_stall_never_closes_turn(self):
        ev = self.work()
        self.assertEqual(ev["kind"], "turn_started")
        # Minutes of low/mid-confidence thinking over a frozen
        # text dump (network wait): never a finish, never idle.
        for conf in (0.4, 0.65, 0.49, 0.55, 0.38, 0.6) * 10:
            ev = self.feed(v("thinking", conf))
            self.assertIsNone(ev)
            self.assertEqual(self.p.state, "working")

    def test_single_prompt_flash_does_not_close(self):
        self.work()
        self.feed(v("idle_at_prompt", 0.95))
        self.assertEqual(self.p.state, "working")
        self.feed(v("thinking", 0.8))
        self.assertEqual(self.p.state, "working")

    def test_persistent_finished_closes_once_and_sticks(self):
        self.work()
        self.assertIsNone(self.feed(v("finished", 0.8)))
        ev = self.feed(v("finished", 0.85))
        self.assertEqual(ev["kind"], "turn_finished")
        self.assertEqual(self.p.state, "finished")
        # Sticky: more rest verdicts do not re-fire the event.
        self.assertIsNone(self.feed(v("idle_at_prompt", 0.9)))
        self.assertEqual(self.p.state, "finished")

    def test_reopen_after_finished_is_a_new_turn(self):
        self.work()
        self.feed(v("finished", 0.8))
        first = self.feed(v("finished", 0.8))["turn_id"]
        ev = self.feed(v("writing_code", 0.9))  # strong: opens at once
        self.assertGreater(ev["turn_id"], first)
        self.assertEqual(self.p.state, "working")

    def test_waiting_for_user_needs_high_confidence(self):
        self.work()
        self.feed(v("asking_question", 0.4))
        self.assertEqual(self.p.state, "working")
        self.feed(Verdict("asking_question", 0.9, needs_user=0.9))
        self.assertEqual(self.p.state, "waiting")

    def test_failed_needs_confirmation(self):
        self.work()
        self.feed(v("failed", 0.7))
        self.assertEqual(self.p.state, "working")
        ev = self.feed(v("failed", 0.75))
        self.assertEqual(ev["kind"], "turn_failed")
        self.assertEqual(self.p.state, "failed")

    def test_noul_work_probability_opens_without_strong_choice(self):
        self.feed(Verdict("thinking", 0.3, is_working=0.9))
        self.assertEqual(self.p.state, "working")

    def test_agent_gone_finishes_open_turn_once(self):
        self.work()
        ev = self.p.note_agent_gone(now=self.t + 5)
        self.assertEqual(ev["kind"], "turn_finished")
        self.assertIsNone(self.p.note_agent_gone(now=self.t + 6))

    def test_idle_prompt_needs_user_is_not_waiting(self):
        # Live incident: an empty prompt scored needs_user=0.8
        # (it does need the next task) and opened a fake
        # awaiting turn out of idle.
        ev = self.feed(Verdict("idle_at_prompt", 0.7,
                               is_working=0.24, needs_user=0.8,
                               is_finished=0.64))
        self.assertIsNone(ev)
        self.assertEqual(self.p.state, "idle")

    def test_finish_probability_closes_despite_noisy_is_working(self):
        # Live incident, exact verdicts: a finished Codex screen
        # still scored is_working 0.38-0.59 (history on screen),
        # which kept resetting the rest streak and wedged the
        # square on Thinking after the turn was over.
        self.work()
        self.feed(Verdict("idle_at_prompt", 0.23, is_working=0.59,
                          is_finished=0.46))
        self.assertEqual(self.p.state, "working")
        self.feed(Verdict("finished", 0.62, is_working=0.29,
                          is_finished=0.88))
        ev = self.feed(Verdict("idle_at_prompt", 0.33,
                               is_working=0.38, is_finished=0.87))
        self.assertEqual(ev["kind"], "turn_finished")
        self.assertEqual(self.p.state, "finished")

    def test_mode_choice_is_smoothed(self):
        self.work()
        self.assertEqual(self.p.mode_choice, "thinking")
        self.feed(v("searching_web", 0.5))      # single sample
        self.assertEqual(self.p.mode_choice, "thinking")
        self.feed(v("searching_web", 0.5))      # confirmed
        self.assertEqual(self.p.mode_choice, "searching_web")


class UnifiedRefreshTests(unittest.TestCase):
    """Muse goes through the same screen perception as Codex:
    agent_state is produced by the state machine, never by a
    per-agent log adapter."""

    def _core(self):
        import importlib.util
        from collections import deque
        spec = importlib.util.spec_from_file_location(
            "native_terminal", ROOT / "desktop" / "native_terminal.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        core = object.__new__(mod.Core)
        core.perception = ScreenPerception()
        core.decisions = deque(maxlen=10)
        core._prev_sig = None
        core._last_outcome = None
        core.mod = mod
        return core

    def test_muse_and_codex_share_the_perception_path(self):
        for cli in ("muse", "codex"):
            core = self._core()
            core.perception.set_agent(cli)
            core.perception.update(Verdict("thinking", 0.9),
                                   now=100.0)
            core._refresh_unified(cli, {
                "foreground": {"cmdline": cli}, "agent_pid": 42})
            st = core.agent_state
            self.assertEqual(st["agent"], cli)
            self.assertEqual(st["state"], "working")
            self.assertEqual(st["source"], "system1-screen")
            self.assertEqual(core.mod.mode_for_agent(st), "flash")


if __name__ == "__main__":
    unittest.main()
