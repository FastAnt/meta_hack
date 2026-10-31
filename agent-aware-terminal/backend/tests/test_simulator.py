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
