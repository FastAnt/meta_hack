import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.app.simulator import evaluate, load_script, state_for_line  # noqa: E402


def _native():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SimulatorTests(unittest.TestCase):
    def test_demo_script_matches_classifier(self):
        rows = evaluate(ROOT / "sims" / "demo_run.jsonl",
                        _native().mode_for_agent)
        bad = [r for r in rows if not r["ok"]]
        self.assertEqual(bad, [], f"mismatches: {bad}")
        self.assertGreaterEqual(len(rows), 15)

    def test_state_for_line_strips_meta(self):
        st = state_for_line({"sleep": 3, "expect": "build",
                             "state": "working", "tool": "bash"})
        self.assertNotIn("sleep", st)
        self.assertNotIn("expect", st)
        self.assertEqual(st["agent"], "simulator")

    def test_load_script_skips_comments(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write('# comment\n\n{"state": "idle"}\n')
            path = fh.name
        self.assertEqual(len(load_script(path)), 1)


if __name__ == "__main__":
    unittest.main()


class AgentPidTests(unittest.TestCase):
    def test_agent_pid_matches_shim_launch(self):
        import sys as _sys
        _sys.path.insert(0, str(ROOT))
        from backend.app.session_observer import _agent_pid
        tree = [
            {"pid": 10, "ppid": 1, "comm": "bash", "cmdline": "-bash"},
            {"pid": 11, "ppid": 10, "comm": "node",
             "cmdline": "node /home/u/.local/bin/codex --yolo"},
            {"pid": 12, "ppid": 11, "comm": "git", "cmdline": "git status"},
        ]
        self.assertEqual(_agent_pid(tree, "codex"), 11)
        self.assertIsNone(_agent_pid(tree, "muse"))
        self.assertIsNone(_agent_pid(tree, None))


class ScreenTextWiringTests(unittest.TestCase):
    def test_screen_classifier_reachable(self):
        """The GUI must hand a non-empty screen to classify_screen:
        guard the S['vte'] wiring that once silently disabled it."""
        native = _native()
        src = pathlib.Path(native.__file__).read_text()
        self.assertIn('S.get("term") or S.get("vte")', src)
        self.assertIn('S["vte"] = vte', src)

    def test_vte_text_coercion(self):
        native = _native()
        self.assertEqual(native._coerce_vte_text((None, [])), "")
        self.assertEqual(native._coerce_vte_text(("abc", [])), "abc")
        self.assertEqual(native._coerce_vte_text(None), "")
        self.assertEqual(native._coerce_vte_text("x"), "x")
