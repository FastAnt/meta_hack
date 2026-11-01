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

    def test_persistent_idle_ends_turn(self):
        self._work()
        self.core._screen_turn_tick("codex", "idle_at_prompt", 0.9)
        self.core._screen_end_since -= 20  # stretch the window
        self.core._screen_turn_tick("codex", "idle_at_prompt", 0.9)
        self.core._screen_turn_tick("codex", "finished", 0.9)
        self.assertIsNotNone(self.core._last_outcome)
        self.assertEqual(self.core._last_outcome["mode"], "done")

    def test_weak_verdicts_do_not_confirm(self):
        self._work()
        for _ in range(5):
            self.core._screen_turn_tick(
                "codex", "idle_at_prompt", 0.2)
        self.assertIsNone(self.core._last_outcome)
        self.assertTrue(self.core._screen_turn_active)

    def test_confirmed_end_computes_results(self):
        # The DONE window must carry FILES/DONE/SAFE: a confirmed
        # screen-turn end finalizes the observed turn.
        self._work()
        self.core._screen_turn_tick("codex", "idle_at_prompt", 0.9)
        self.core._screen_end_since -= 20
        self.core._screen_turn_tick("codex", "idle_at_prompt", 0.9)
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


class ScreenRuleTests(unittest.TestCase):
    def test_edited_line_means_writing_code(self):
        native = _native()
        screen = ("Ran python3 build.py\n"
                  "Edited 2 files (+8 -0)\n"
                  "docs/x.md (+4 -0)\nWorking (3m - esc)")
        self.assertEqual(native._screen_rule_choice(screen),
                         "writing_code")

    def test_old_edited_line_out_of_window_is_ignored(self):
        native = _native()
        lines = ["Edited 9 files (+1 -1)"] + \
            [f"output line {i}" for i in range(30)]
        self.assertIsNone(
            native._screen_rule_choice("\n".join(lines)))

    def test_work_root_follows_terminal_cwd(self):
        import pathlib as _pl
        core = _native().Core(tempfile.mkdtemp())
        with tempfile.TemporaryDirectory() as tmp:
            sub = _pl.Path(tmp) / "proj" / "src"
            sub.mkdir(parents=True)
            (_pl.Path(tmp) / "proj" / ".git").mkdir()
            core.snapshot = {"cwd": str(sub)}
            self.assertEqual(core._work_root(),
                             str(_pl.Path(tmp) / "proj"))
