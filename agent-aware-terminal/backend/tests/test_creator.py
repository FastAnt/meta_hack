"""Creator-agent launch helpers: mission text and the
project-substance pre-gate for boot generation."""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _native():
    spec = importlib.util.spec_from_file_location(
        "native_terminal", ROOT / "desktop" / "native_terminal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CreatorPromptTests(unittest.TestCase):
    def test_prompt_bounds_the_agent(self):
        text = _native().creator_task_text(
            "/app", "/proj", "fact", task="fix the parser")
        for needle in ("/app/content-researcher/SKILL.md",
                       "/proj/.aat/content/", '"fact"',
                       "fix the parser", "test_game.py",
                       "write ONLY inside"):
            self.assertIn(needle, text)

    def test_game_prompt_points_at_the_sdk(self):
        text = _native().creator_task_text("/app", "/p", "game")
        self.assertIn("/app/games-base/AGENTS.md", text)


class SubstanceTests(unittest.TestCase):
    def test_small_project_is_not_substantial(self):
        ok, _ = _native().project_substance(
            {"domains": [{"score": 3}, {"score": 2}]})
        self.assertFalse(ok)

    def test_real_project_is_substantial(self):
        ok, _ = _native().project_substance(
            {"domains": [{"score": 18}, {"score": 7},
                         {"score": 4}]})
        self.assertTrue(ok)

    def test_no_domains(self):
        ok, _ = _native().project_substance({"domains": []})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
