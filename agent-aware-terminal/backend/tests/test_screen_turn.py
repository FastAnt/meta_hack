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
    """While the screen keeps changing, calls fire every 4 s
    (no skipped steps); when it stops, one trailing call, then
    silence — an idle terminal costs zero calls."""

    def setUp(self):
        self.core = object.__new__(_native().Core)

    def test_streaming_fires_every_4s(self):
        c = self.core
        self.assertTrue(c._classify_due(100.0, "a"))
        c._screen_at = 100.0
        self.assertFalse(c._classify_due(101.0, "ab"))
        self.assertTrue(c._classify_due(104.0, "ab"))
        c._screen_at = 104.0
        self.assertTrue(c._classify_due(108.0, "abc"))
        c._screen_at = 108.0
        self.assertTrue(c._classify_due(112.0, "abcd"))

    def test_one_trailing_call_then_silence(self):
        c = self.core
        self.assertTrue(c._classify_due(100.0, "a"))
        c._screen_at = 100.0
        # unchanged screen: one trailing call at +4, then none
        self.assertTrue(c._classify_due(104.0, "a"))
        c._screen_at = 104.0
        self.assertFalse(c._classify_due(108.0, "a"))
        self.assertFalse(c._classify_due(200.0, "a"))

    def test_final_frame_seen_then_trailing(self):
        c = self.core
        self.assertTrue(c._classify_due(100.0, "work"))
        c._screen_at = 100.0
        self.assertTrue(c._classify_due(104.0, "final"))
        c._screen_at = 104.0
        self.assertTrue(c._classify_due(108.0, "final"))
        c._screen_at = 108.0
        self.assertFalse(c._classify_due(112.0, "final"))


class InferredShortTurnTests(unittest.TestCase):
    """A turn that fits between polls: idle_at_prompt, screen
    changes, finished — the cycle itself closes the turn."""

    def setUp(self):
        import tempfile
        self.core = object.__new__(_native().Core)
        self.core._tmp = tempfile.mkdtemp()
        self.core.project_path = self.core._tmp
        self.core._baseline = None
        self.core.results = None
        self.core._screen_hash = 111
        self.core._screen_turn_active = False
        self.core._busy_started_at = None
        self.core._agent_key_now = "codex:1"
        self.core.decisions = []
        self.core.liquid = type("L", (), {
            "configured": False,
            "evaluate_session": lambda s, x: None})()

    def test_cycle_closes_the_unseen_turn(self):
        c = self.core
        c._screen_turn_tick("codex", "idle_at_prompt", 0.8,
                            "banner\n> Ask Codex")
        # the screen moved: Q + A appeared between polls
        c._screen_turn_tick("codex", "finished", 0.85,
                            "banner\n> 2+2?\n4.\n> Ask Codex")
        self.assertEqual(c._last_outcome["mode"], "done")
        self.assertIsNotNone(c.results)
        self.assertFalse(c.results["files_measured"])
        self.assertEqual(c.results["files"], [])

    def test_finished_without_cycle_does_nothing(self):
        c = self.core
        c._screen_turn_tick("codex", "finished", 0.9)
        self.assertFalse(getattr(c, "_last_outcome", None))
        self.assertIsNone(c.results)

    def test_unchanged_screen_does_nothing(self):
        c = self.core
        c._screen_turn_tick("codex", "idle_at_prompt", 0.8,
                            "same screen")
        c._screen_turn_tick("codex", "finished", 0.9,
                            "same screen")
        self.assertIsNone(c.results)


class ReopenCooldownTests(unittest.TestCase):
    """Late work verdicts about the closing screen cannot
    reopen the turn; genuinely new on-screen work can."""

    def setUp(self):
        self.core = object.__new__(_native().Core)
        self.core._screen_hash = 555
        self.core._closed_screen_hash = 555
        self.core._turn_closed_at = time.time()
        self.core._screen_turn_active = False
        self.core._last_outcome = {"agent": "codex",
                                   "mode": "done"}
        self.core._start_baseline = lambda: None
        self.core.decisions = []

    def test_same_screen_work_verdict_ignored(self):
        self.core._screen_turn_tick("codex", "writing_code", 0.9)
        self.assertFalse(self.core._screen_turn_active)
        self.assertEqual(self.core._last_outcome["mode"], "done")

    def test_changed_screen_opens(self):
        self.core._screen_hash = 777
        self.core._screen_turn_tick("codex", "writing_code", 0.9)
        self.assertTrue(self.core._screen_turn_active)


import time  # noqa: E402  (used by ReopenCooldownTests)


class ReceiptInferenceTests(unittest.TestCase):
    """A new 'Worked for Ns' receipt after a resting snapshot
    closes a turn nobody saw — measured from the text itself."""

    def setUp(self):
        import tempfile
        self.core = object.__new__(_native().Core)
        self.core.decisions = []
        self.core.liquid = type("L", (), {
            "configured": False,
            "evaluate_session": lambda s, x: None})()
        self.core.project_path = tempfile.mkdtemp()
        self.core._baseline = None
        self.core.results = None
        self.core._busy_started_at = None
        self.core._screen_turn_active = False
        self.core._agent_key_now = "codex:1"
        self.core._screen_hash = 1
        self.core._screen_quiet_since = time.time()

    def test_new_receipt_closes_turn(self):
        c = self.core
        c._screen_tail = "old answer\nWorked for 9s\n> prompt"
        c._screen_turn_tick("codex", "idle_at_prompt", 0.8)
        scr = ("old answer\nWorked for 9s\n> question\n"
               "new answer\nWorked for 4s\n> prompt")
        c._screen_quiet_tick("codex", scr)   # registers the change
        c._screen_quiet_tick("codex", scr)   # now the receipt reads
        self.assertIsNotNone(c.results)
        self.assertEqual(c._last_outcome["mode"], "done")
        self.assertFalse(c.results["files_measured"])

    def test_no_new_receipt_no_close(self):
        c = self.core
        c._screen_tail = "old answer\nWorked for 9s\n> prompt"
        c._screen_turn_tick("codex", "idle_at_prompt", 0.8)
        c._screen_quiet_tick(
            "codex", "old answer\nWorked for 9s\n> prompt")
        self.assertIsNone(c.results)


class MidTurnStallTests(unittest.TestCase):
    """A network wait freezes the screen mid-turn: quiet alone
    must not close the turn while the last verdict says work."""

    def setUp(self):
        self.core = object.__new__(_native().Core)
        self.core.decisions = []
        self.core.liquid = type("L", (), {
            "configured": False,
            "evaluate_session": lambda s, x: None})()
        self.core._screen_turn_active = True
        self.core._screen_turn_agent = "codex"
        self.core._screen_turn_since = time.time() - 60
        self.core._screen_turn_failed = False
        self.core._busy_started_at = None
        self.core._last_outcome = None

    def _tick_quiet(self, secs_ago=10):
        scr = "frozen output"
        self.core._screen_hash = hash(scr[-900:])
        self.core._screen_quiet_since = time.time() - secs_ago
        self.core._screen_quiet_tick("codex", scr)

    def test_search_stall_does_not_close(self):
        c = self.core
        c._last_screen_choice = "searching_web"
        c._last_screen_choice_at = time.time()
        self._tick_quiet()
        self.assertTrue(c._screen_turn_active)
        self.assertIsNone(c._last_outcome)

    def test_rest_verdict_lets_quiet_close(self):
        c = self.core
        c._last_screen_choice = "idle_at_prompt"
        c._last_screen_choice_at = time.time()
        c.project_path = "/tmp"
        c._baseline = None
        c.results = None
        self._tick_quiet()
        self.assertFalse(c._screen_turn_active)
        self.assertEqual(c._last_outcome["mode"], "done")


class PendingDoneTests(unittest.TestCase):
    """Process-idle endings wait for the screen to agree."""

    def setUp(self):
        import tempfile
        self.core = object.__new__(_native().Core)
        self.core.decisions = []
        self.core.liquid = type("L", (), {
            "configured": False,
            "evaluate_session": lambda s, x: None})()
        self.core.project_path = tempfile.mkdtemp()
        self.core._baseline = None
        self.core.results = None
        self.core._agent_key_now = "codex:1"
        self.core._screen_quiet_since = time.time()

    def test_defers_until_rest(self):
        c = self.core
        c._pending_done = {"agent": "codex", "secs": 40}
        c._last_screen_choice = "thinking"
        c._last_screen_choice_at = time.time()
        c._maybe_arm_pending_done()
        self.assertIsNone(c.results)
        self.assertIsNotNone(c._pending_done)
        c._screen_turn_tick("codex", "finished", 0.8, "screen")
        self.assertIsNone(c._pending_done)
        self.assertIsNotNone(c.results)
