"""Screen-driven turn endings must survive mid-turn prompt
flashes: one 'idle_at_prompt' verdict between steps of a long
Codex turn may not fire DONE (regression: the agent kept
working for five more minutes under a check mark)."""
import importlib.util
import pathlib
import sys
import tempfile
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


class ScreenTurnTests(unittest.TestCase):
    def setUp(self):
        self.core = _native().Core(tempfile.mkdtemp())

    def _work(self, n=2):
        for _ in range(n):
            self.core._screen_turn_tick("codex", "thinking", 0.9)
        # the turn has been running for a minute
        self.core._screen_turn_since = time.time() - 60

    def test_single_prompt_flash_does_not_end_turn(self):
        self._work()
        self.core._screen_turn_tick("codex", "idle_at_prompt", 0.9)
        self.assertIsNone(self.core._last_outcome)
        self.assertTrue(self.core._screen_turn_active)
        # work resumes; the flash is forgotten
        self._work(1)
        self.assertIsNone(self.core._last_outcome)

    def _quiet_close(self, secs=30):
        screen = "static prompt screen"
        self.core._screen_hash = hash(screen)
        self.core._screen_quiet_since = time.time() - secs
        self.core._screen_quiet_tick("codex", screen)

    def test_rest_verdicts_never_end_turn(self):
        # Even persistent confident "idle/finished" verdicts are
        # flavor now: they cannot end a turn or arm DONE.
        self._work()
        for _ in range(5):
            self.core._screen_turn_tick(
                "codex", "idle_at_prompt", 0.95)
            self.core._screen_turn_tick("codex", "finished", 0.95)
        self.assertIsNone(self.core._last_outcome)
        self.assertTrue(self.core._screen_turn_active)

    def test_screen_quiet_ends_turn(self):
        self._work()
        self._quiet_close()
        self.assertIsNotNone(self.core._last_outcome)
        self.assertEqual(self.core._last_outcome["mode"], "done")
        self.assertFalse(self.core._screen_turn_active)

    def test_quiet_too_short_keeps_turn(self):
        self._work()
        self._quiet_close(secs=1)  # under TURN_END_CONFIRM_S
        self.assertTrue(self.core._screen_turn_active)
        self.assertIsNone(self.core._last_outcome)

    def test_confirmed_end_computes_results(self):
        # The DONE window must carry FILES/DONE/SAFE: the quiet
        # turn end finalizes the observed turn.
        self._work()
        self._quiet_close()
        self.assertIsNotNone(self.core.results)
        self.assertTrue(self.core.results["observed"])
        self.assertEqual(self.core.results["final_state"],
                         "completed")

    def test_work_resumption_clears_stale_done(self):
        self.core._last_outcome = {
            "key": "k", "agent": "codex", "mode": "done"}
        self._work(1)
        self.assertIsNone(self.core._last_outcome)


if __name__ == "__main__":
    unittest.main()



class ClassifyScheduleTests(unittest.TestCase):
    """Perception fires on change, not on a timer: settle 4 s,
    one call; exactly one trailing call 4 s later; a static
    screen costs nothing; endless output caps at MAX_WAIT."""

    def setUp(self):
        self.core = object.__new__(_native().Core)

    def test_settle_then_fire_once(self):
        c = self.core
        self.assertFalse(c._classify_due(100.0, "banner"))
        self.assertFalse(c._classify_due(103.9, "banner"))
        self.assertTrue(c._classify_due(104.0, "banner"))

    def test_one_trailing_call_then_silence(self):
        c = self.core
        c._classify_due(100.0, "banner")
        c._screen_at = 104.0
        self.assertTrue(c._classify_due(104.0, "banner"))
        c._screen_at = 104.0
        self.assertFalse(c._classify_due(107.9, "banner"))
        self.assertTrue(c._classify_due(108.0, "banner"))
        c._screen_at = 108.0
        self.assertFalse(c._classify_due(120.0, "banner"))
        self.assertFalse(c._classify_due(200.0, "banner"))

    def test_changes_rearm_the_debounce(self):
        c = self.core
        self.assertFalse(c._classify_due(100.0, "a"))
        self.assertFalse(c._classify_due(103.0, "ab"))
        self.assertFalse(c._classify_due(106.9, "ab"))
        self.assertTrue(c._classify_due(107.0, "ab"))

    def test_endless_output_caps_at_max_wait(self):
        c = self.core
        t = 100.0
        fired = False
        for i in range(40):
            t += 2.0
            if c._classify_due(t, f"line {i}"):
                fired = True
                break
        self.assertTrue(fired)
        self.assertLessEqual(t, 100.0 + 16.0)
